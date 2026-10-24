import requests
import json

# ================= 原有配置 =================
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL_NAME = "llama3.1:8b"
log_file = "cafe_decision_log.txt"
total_round = 10

# 业务常量
COFFEE_POWDER_PER_CUP = 0.018
# 新增：人工合理范围，根据你的场景设置，你可以自己改
MIN_STOCK = 5
MAX_STOCK = 80

print("====咖啡店AI咖啡粉进货预测====")
coffee_cups = int(input("1. 昨日销售杯数："))
weather = input("2. 明日天气：")
nearby_event = input("3. 周边活动：")
weekday = input("4. 明天是周几：")
is_holiday = input("5. 是否节假日（是/否）：")
owner_comment = input("6. 店主看法：")
owner_opinion = owner_comment.strip() if owner_comment.strip() else "店主希望不要囤太多货，优先减少浪费"

result_list = []

def ai_first_predict(coffee_cups, powder_per_cup, weather, nearby_event, weekday, is_holiday):
    prompt = f"""
你是咖啡店库存顾问，根据信息预测明天咖啡粉进货公斤数。
昨日销量：{coffee_cups}杯
每杯消耗咖啡粉：{powder_per_cup} kg
星期：{weekday}
是否节假日：{is_holiday}
明日天气：{weather}
周边活动：{nearby_event}
【硬性规则】预测进货量必须在 {MIN_STOCK} ~ {MAX_STOCK} kg 之间，不能超出这个范围，符合咖啡店真实进货逻辑，不要夸张。
只输出JSON：{{"suggest_kg":数字,"risk_comment":"风险描述"}}
不要多余文字
"""
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "temperature": 0.85,
        "stream": False
    }
    try:
        res = requests.post(OLLAMA_URL, json=payload, timeout=60)
        res.raise_for_status()
        raw = res.json()["response"].strip()
        s = raw.find("{")
        e = raw.rfind("}") + 1
        data = json.loads(raw[s:e])
        # Python侧二次截断兜底，防止AI无视规则输出离谱数字
        data["suggest_kg"] = max(MIN_STOCK, min(MAX_STOCK, float(data["suggest_kg"])))
        return data
    except Exception as e:
        print(f"第一轮预测报错：{e}")
        return None


def ai_final_decision(base_kg, powder_per_cup, coffee_cups, owner_opinion, weather, nearby_event, weekday, is_holiday):
    prompt = f"""
基于初次推荐{base_kg}kg，结合店主意见：{owner_opinion}
昨日销量{coffee_cups}杯，每杯耗粉{powder_per_cup}kg，星期{weekday}，节假日：{is_holiday}
天气：{weather}，周边活动：{nearby_event}
【硬性规则】最终进货量必须在 {MIN_STOCK} ~ {MAX_STOCK} kg 之间，符合咖啡店真实经营，禁止极端离谱数值。
重新权衡，输出最终进货量JSON格式：{{"final_kg":数字,"comment":"说明"}}
"""
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "temperature": 0.85,
        "stream": False
    }
    try:
        res = requests.post(OLLAMA_URL, json=payload, timeout=60)
        res.raise_for_status()
        raw = res.json()["response"].strip()
        s = raw.find("{")
        e = raw.rfind("}") + 1
        data = json.loads(raw[s:e])
        # 兜底截断
        data["final_kg"] = max(MIN_STOCK, min(MAX_STOCK, float(data["final_kg"])))
        return data
    except Exception as e:
        raise e


with open(log_file, "w", encoding="utf-8") as f:
    f.write("====咖啡店咖啡粉进货AI预测日志====\n")

for i in range(1, total_round + 1):
    print(f"\n----------【第 {i}/{total_round} 轮】----------")
    first_res = ai_first_predict(coffee_cups, COFFEE_POWDER_PER_CUP, weather, nearby_event, weekday, is_holiday)
    if first_res is None:
        print("❌第一轮预测失败，跳过本轮")
        continue
    ai_rec = float(first_res["suggest_kg"])
    print(f"✅【AI第一轮模拟推荐】推荐进货：{ai_rec:.2f} kg，风险提示：{first_res['risk_comment']}")
    try:
        final_out = ai_final_decision(ai_rec, COFFEE_POWDER_PER_CUP, coffee_cups, owner_opinion, weather, nearby_event, weekday, is_holiday)
        final_val = float(final_out["final_kg"])
        print(f"✅【AI正在结合海量模拟数据 + 店主经验，独立重新权衡，生成最终方案...】")
        print(f"✅本轮最终进货推荐：{final_val:.2f} kg，备注：{final_out['comment']}")
        result_list.append(final_val)
        log_line = f"第{i}轮 | 初次:{ai_rec:.2f}kg | 最终:{final_val:.2f}kg | {final_out['comment']}\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception as err:
        print(f"❌本轮二次决策失败（主+兜底模型全部挂掉）：{err}，跳过本轮")
        final_val = ai_rec
        result_list.append(final_val)
        print(f"⚠️  二次决策异常，暂用第一轮推荐值：{final_val:.2f} kg")
        log_line = f"第{i}轮 | 初次:{ai_rec:.2f}kg | 二次失败，复用初次值:{final_val:.2f}kg\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(log_line)


print("\n======== 全部轮次运行结束 ========")
print(f"全部有效结果：{result_list}")
if len(result_list) >= 5:
    sorted_res = sorted(result_list)
    trimmed = sorted_res[2:-2]
    avg_val = sum(trimmed) / len(trimmed)
    print(f"排序后全部有效结果: {sorted_res}")
    print(f"剔除最小2个、最大2个之后保留数据: {trimmed}")
    print(f"🎯 综合最终建议进货量（截尾均值）: {avg_val:.2f} kg")
else:
    print("⚠️ 有效轮次不足，无法剔除极值，可用结果：", result_list)

with open(log_file, "a", encoding="utf-8") as f:
    f.write("\n===== 汇总 =====\n")
    if len(result_list) >= 5:
        f.write(f"原始列表:{result_list}\n剔除极值后:{trimmed}\n最终推荐进货 {avg_val:.2f} kg\n")
    else:
        f.write(f"有效数据不足，原始列表:{result_list}\n")

print(f"\n📝 {total_round}轮模拟运行完毕！所有结果保存在 {log_file}")
