"""Tkinter desktop MVP: input -> model -> review -> simulated order -> receipt log."""
from __future__ import annotations
import queue
import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from cafe_core import AuditStore, CafeError, OllamaClient, fingerprint, json_text, run_forecast, validate_context, validate_history
from cafe_io import BOOL_KEYS, LABELS, demo_context, export_result, parse_field, read_input, write_template

BASE = Path(__file__).resolve().parents[1]


class CafeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Cafe AI · 销量预测与补货审批（比赛模拟版）")
        self.root.geometry("1100x870")
        self.store = AuditStore(BASE / "runtime" / "cafe.sqlite3")
        self.history = []
        self.result = None
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.inputs = {}
        self.role = tk.StringVar(value="operator")
        self.acknowledged = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="先导入模板或填写模拟数据；点击“检查模型”确认连接。")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TButton", padding=(10, 7))
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"), foreground="#174A50")
        top = ttk.Frame(root, padding=16)
        top.pack(fill="x")
        ttk.Label(top, text="Cafe AI  补货工作台", style="Title.TLabel").pack(anchor="w")
        ttk.Label(top, text="Llama 估计销量 · 十轮去极值 · 人工审批 · Excel 导入/导出").pack(anchor="w", pady=(4, 0))
        ttk.Label(top, text="仅限团队模拟数据。当前没有已验证的预测准确率；角色切换仅为比赛演示。", foreground="#9A5B00").pack(anchor="w", pady=4)
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=16)
        self.input_tab = ttk.Frame(self.tabs, padding=10)
        self.output_tab = ttk.Frame(self.tabs, padding=10)
        self.order_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.input_tab, text="1  输入与预测")
        self.tabs.add(self.output_tab, text="2  结果与审批")
        self.tabs.add(self.order_tab, text="3  订单与记录")
        self.build_input()
        self.build_output()
        self.build_orders()
        ttk.Label(root, textvariable=self.status, wraplength=1060, padding=12).pack(fill="x")
        self.fill(demo_context(datetime.now(timezone(timedelta(hours=8))).date().isoformat()))
        self.root.after(100, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def build_input(self):
        toolbar = ttk.Frame(self.input_tab)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="导入 Excel / JSON", command=self.import_file).pack(side="left", padx=3)
        ttk.Button(toolbar, text="保存 Excel 输入", command=self.save_input).pack(side="left", padx=3)
        ttk.Button(toolbar, text="检查模型", command=self.health).pack(side="left", padx=3)
        self.history_label = ttk.Label(toolbar, text="历史记录：0 天")
        self.history_label.pack(side="left", padx=10)
        canvas = tk.Canvas(self.input_tab, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.input_tab, orient="vertical", command=canvas.yview)
        self.form = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=self.form, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        self.form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        scrollbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        for i, (key, label) in enumerate(LABELS.items()):
            ttk.Label(self.form, text=label, wraplength=390).grid(row=i, column=0, sticky="w", pady=5, padx=4)
            var = tk.StringVar()
            self.inputs[key] = var
            if key in BOOL_KEYS:
                widget = ttk.Combobox(self.form, textvariable=var, values=["否", "是"], state="readonly", width=50)
            else:
                widget = ttk.Entry(self.form, textvariable=var, width=65)
                if key == "data_source":
                    widget.configure(state="readonly")
            widget.grid(row=i, column=1, sticky="ew", pady=5, padx=4)
        self.form.columnconfigure(1, weight=1)
        actions = ttk.Frame(self.input_tab)
        actions.pack(fill="x", pady=10)
        self.run_button = ttk.Button(actions, text="开始 10 次 AI 估计", command=self.start)
        self.run_button.pack(side="left")
        ttk.Button(actions, text="取消（当前请求结束后停止）", command=self.cancel.set).pack(side="left", padx=10)

    def build_output(self):
        self.output = tk.Text(self.output_tab, wrap="word", font=("Microsoft YaHei UI", 10), padx=12, pady=12)
        self.output.pack(fill="both", expand=True)
        self.output.configure(state="disabled")
        row = ttk.Frame(self.output_tab)
        row.pack(fill="x", pady=8)
        ttk.Label(row, text="模拟角色").pack(side="left")
        ttk.Combobox(row, textvariable=self.role, values=["operator", "owner"], state="readonly", width=12).pack(side="left", padx=8)
        ttk.Label(row, text="operator = 店员；owner = 店主").pack(side="left")
        ttk.Checkbutton(self.output_tab, variable=self.acknowledged,
                        text="我已核对库存时间、在途、交期、模型分歧与理由，理解预测未验证且不向供应商实际下单。 ").pack(anchor="w", pady=6)
        buttons = ttk.Frame(self.output_tab)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="批准模拟采购", command=self.approve).pack(side="left", padx=3)
        ttk.Button(buttons, text="导出 Excel / JSON", command=self.export).pack(side="left", padx=3)

    def build_orders(self):
        ttk.Label(self.order_tab, text="批准不会增加库存；只有确认收货才记入收货账。下一次填表仍需核对可用存量。", wraplength=1020).pack(anchor="w", pady=8)
        self.orders = ttk.Treeview(self.order_tab, columns=("id", "date", "kg", "status"), show="headings", height=8)
        for key, text, width in (("id", "订单编号", 390), ("date", "目标日期", 150), ("kg", "kg", 100), ("status", "状态", 120)):
            self.orders.heading(key, text=text)
            self.orders.column(key, width=width)
        self.orders.pack(fill="x")
        buttons = ttk.Frame(self.order_tab)
        buttons.pack(fill="x", pady=10)
        ttk.Button(buttons, text="确认模拟收货", command=lambda: self.transition("received")).pack(side="left", padx=4)
        ttk.Button(buttons, text="取消待收货订单", command=lambda: self.transition("cancelled")).pack(side="left", padx=4)
        ttk.Button(buttons, text="刷新记录", command=self.refresh_orders).pack(side="left", padx=4)
        self.log = tk.Text(self.order_tab, wrap="word", font=("Microsoft YaHei UI", 9), state="disabled")
        self.log.pack(fill="both", expand=True)
        self.refresh_orders()

    def fill(self, c):
        for key, var in self.inputs.items():
            value = c[key]
            var.set(("是" if value else "否") if key in BOOL_KEYS else str(value))
        self.acknowledged.set(False)

    def collect(self):
        c = validate_context({k: parse_field(k, v.get()) for k, v in self.inputs.items()})
        validate_history(self.history, c)
        return c

    def error(self, error):
        self.status.set("操作未完成：" + str(error))
        messagebox.showerror("请检查", str(error), parent=self.root)

    def import_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(filetypes=[("输入文件", "*.xlsx *.json")])
        if not path:
            return
        try:
            c, self.history = read_input(path)
            self.fill(c)
            self.history_label.configure(text=f"历史记录：{len(self.history)} 天")
            self.result = None
            self.set_output("已导入新输入，请运行模型。")
            self.status.set("输入已导入。星期由目标日期自动核对。")
        except Exception as e:
            self.error(e)

    def save_input(self):
        try:
            c = self.collect()
            path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
            if path:
                write_template(path, c, self.history)
                self.status.set("输入文件已保存。")
        except Exception as e:
            self.error(e)

    def background(self, fn, kind):
        self.busy = True
        self.run_button.configure(state="disabled")
        def worker():
            try:
                self.events.put((kind, fn()))
            except Exception as e:
                self.events.put(("error", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def health(self):
        if self.busy:
            return
        self.status.set("正在检查本地/配置的 Ollama 服务…")
        self.background(lambda: OllamaClient().health(), "health")

    def start(self):
        if self.busy:
            return
        try:
            c = self.collect()
            history = [dict(r) for r in self.history]
            self.cancel.clear()
            self.result = None
            self.acknowledged.set(False)
            self.set_output("正在运行模型。CPU 推理可能较慢；尚未生成采购建议。")
            self.status.set("连接模型…")
            self.background(lambda: run_forecast(c, history, OllamaClient(),
                            lambda message: self.events.put(("progress", message)), self.cancel), "result")
        except Exception as e:
            self.error(e)

    def set_output(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", text)
        self.output.configure(state="disabled")

    def show_result(self, r):
        status = {"needs_review": "待店主核对", "blocked": "已阻止审批", "cancelled": "已取消", "closed": "休业日，不采购"}.get(r["status"], r["status"])
        lines = [f"状态：{status}", f"预测日期：{r['context']['target_date']}  {r['context']['weekday']}",
                 f"模型：{r['model'].get('model')}  |  数据：团队自建模拟数据", ""]
        if "summary" in r:
            s = r["summary"]
            p = r["procurement"]
            lines += [f"明日估计销量：{s['forecast_cups']:.1f} 杯", f"预计咖啡豆/粉消耗：{p['forecast_consumption_kg']:.3f} kg",
                      f"建议采购：{p['suggested_order_kg']:.3f} kg（{p['pack_count']} 包）",
                      f"明日开店前可用存量＋确认在途：{p['opening_available_kg']:.3f} kg",
                      "库存计算：销量 × 每杯克数 ÷ 1000 ＋安全库存－可用存量－在途，然后按包装取整。", ""]
            if "sample_min" in s:
                lines += [f"十轮采样范围：{s['sample_min']:.1f}–{s['sample_max']:.1f} 杯（不是置信区间）",
                          "保留的六个估计：" + ", ".join(f"{n:.1f}" for n in s["retained_samples"]), ""]
        lines += ["需要核对："] + ["• " + w for w in r["warnings"] + r.get("procurement", {}).get("blockers", [])]
        lines += ["", "逐轮模型依据（模型生成，必须核对是否忠于输入）："]
        for i, s in enumerate(r["samples"], 1):
            lines += [f"{i}. {s['forecast_cups']:.1f} 杯：{s['reason']}", "   引用：" + ", ".join(s["evidence_ids"]), "   局限：" + "；".join(s["limitations"])]
        self.set_output("\n".join(lines))
        self.tabs.select(self.output_tab)

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.status.set(value)
                    continue
                self.busy = False
                self.run_button.configure(state="normal")
                if kind == "error":
                    self.error(value)
                elif kind == "health":
                    self.status.set(f"连接成功：{value['model']}；Ollama {value['ollama_version']}；{value['mode']}。")
                elif kind == "result":
                    self.result = value
                    try:
                        self.store.save_run(value)
                        self.show_result(value)
                        self.refresh_orders()
                        self.status.set("结果已记录；请先核对，再由店主决定是否批准。")
                    except Exception as e:
                        self.result = None
                        self.error(e)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def approve(self):
        try:
            if self.busy or not self.result:
                raise CafeError("请先完成模型预测。")
            current_hash = fingerprint({"context": self.collect(), "history": validate_history(self.history, self.collect())})
            if current_hash != self.result["input_hash"]:
                raise CafeError("输入已变化，请重新预测；不能批准旧输入对应的建议。")
            order_id = self.store.approve(self.result["run_id"], self.role.get(), self.acknowledged.get())
            self.refresh_orders()
            self.tabs.select(self.order_tab)
            self.status.set("已生成模拟订单 " + order_id + "；未向供应商发送，也未增加库存。")
        except Exception as e:
            self.error(e)

    def export(self):
        if not self.result:
            self.error("没有可导出的预测记录。")
            return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx"), ("JSON", "*.json")])
        if path:
            try:
                export_result(path, self.result, self.store.snapshot())
                self.status.set("结果与操作记录已导出。")
            except Exception as e:
                self.error(e)

    def transition(self, action):
        selected = self.orders.selection()
        if not selected:
            self.error("请先选择订单。")
            return
        try:
            self.store.transition(selected[0], self.role.get(), action)
            self.refresh_orders()
            self.status.set("已记录模拟收货。" if action == "received" else "订单已取消。")
        except Exception as e:
            self.error(e)

    def refresh_orders(self):
        snap = self.store.snapshot()
        self.orders.delete(*self.orders.get_children())
        for order in snap["orders"]:
            self.orders.insert("", "end", iid=order["id"], values=(order["id"], order["target_date"], order["quantity"], order["status"]))
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(f"{e['at']} | {e['actor']} | {e['action']} | {e['detail']}" for e in snap["events"][-100:]))
        self.log.configure(state="disabled")

    def close(self):
        self.cancel.set()
        self.root.destroy()


def launch():
    root = tk.Tk()
    CafeApp(root)
    root.mainloop()
