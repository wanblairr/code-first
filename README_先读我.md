# Cafe AI：咖啡店销量预测与补货助手

基于你提供的 `cafe_ollama_stock.py` 重构。保留“输入昨日销量、天气、明天星期/节假日、周围活动、店主意见，由大模型估计，再运行十次去掉最高最低各两个取平均”的核心思路。

**当前交付状态：工程流程和接口替身测试已完成；没有完成真实 Llama 联调、权重训练或真实门店效果验证。** 本次连接的电脑未检测到正在运行的 Ollama 服务，常见安装位置也未发现 Ollama。测试数量及结果见 `reports/engineering_summary.json`。请勿把这些工程测试称为预测准确率。

## 1. 先运行起来

1. 把整个压缩包解压到一个文件夹。不要只拿主脚本，其他模块也要保留。
2. 电脑需要 Python 3.11 或更新版本，且包含 Tkinter。本次在 Windows / Python 3.13.15 检查。
3. 程序默认连接 `http://127.0.0.1:11434`，模型是 **`llama3.1:8b`**。这是 Llama 3.1 的 8B 参数版本，不是“Ollama 3.1.8”。
4. 在你已经安装 Ollama 的电脑打开 Ollama；终端执行 `ollama list`，确认包含该模型。若未下载，`ollama pull llama3.1:8b` 会下载数 GB 模型，请先确认空间和网络。
5. 双击 `start.cmd`。或者在项目文件夹终端执行：

```powershell
python cafe_ollama_stock.py doctor
python cafe_ollama_stock.py gui
```

本包附有 Excel 依赖 `vendor`，可以直接使用。若删掉了该目录，可执行 `python -m pip install -r requirements.txt`。核心预测、审计和 JSON 功能只用 Python 标准库。

如果 `doctor` 提示不能连接：先在运行 Ollama 的电脑执行 `ollama list`，确认服务和模型；若 Ollama 在另一台电脑，默认的 127.0.0.1 不会连接到那台电脑。不要为了连接把未授权的 Ollama 服务直接暴露到公网。

## 2. 第一遍操作

1. 打开“输入与预测”，导入 `examples/cafe_input.xlsx`，或直接编辑默认模拟数据。
2. 样例日期固定为 2026-10-21 填表、2026-10-20 昨日销量、2026-10-22 预测日。复现样例时保留这组日期；测试新情景时同步修改日期和历史记录。
3. 确认“检查模型”成功后，点击“开始 10 次 AI 估计”。CPU 推理可能较慢；每次请求超时 180 秒，最多重试一次。不能保证完成时长。
4. 在“结果与审批”查看预测杯数、十轮结果、分歧、依据、公斤换算和采购量。
5. 切换模拟角色 `owner`，勾选核对确认，才能批准模拟采购。店员 `operator` 无权批准。
6. 在“订单与记录”确认模拟收货。批准不增加库存；收货才记入收货账。它不会联系供应商、付款或真实下单。
7. 导出 Excel，保留输入快照、十轮估计、模型引用的证据、审批和操作记录。

角色切换是赛事允许的流程演示，不是真正的登录授权系统；SQLite 是本地记录，不是防篡改审计平台。

## 3. 最重要的输入含义

本版只处理**单店、一类咖啡原料、明天一个营业日**。不同饮品如果每杯用豆量不同，应先拆成独立配方，不能直接混用一个平均数而不说明。

| 输入 | 应该填写什么 |
|---|---|
| 昨日杯数、日期 | 已知的前一天销量；预测明天时，与目标日期相隔两天 |
| 明日天气、周围活动 | 已知或店主设定的模拟情景；未知就写未知 |
| 是否节假日 | 店主确认的布尔值；程序不联网推断公共假期 |
| 店主意见 | 经验、活动预期和不确定性；想少采购不代表顾客需求变少 |
| 最大产能 | 明天确实最多能制作的杯数，模型超过就判无效，不偷偷截断 |
| 每杯克数 | 咖啡豆/粉的实际配方用量；默认 18 g 只是样例 |
| 明日开店前预计可用存量 | 扣除今天消耗后、明天开店前仍可用的存量；不是当前库存 |
| 确认在途数量 | 确认在明天开店前到货，而且未计入上面存量的数量 |
| 安全库存 | 店主明确设定的采购偏好，默认 0.2 kg 不是行业推荐 |
| 每包 kg | 供应商实际包装大小；例如 0.5 kg |
| 单次采购上限、储存上限 | 店主设置的硬约束，超出时阻止审批 |
| 新采购到货日 | 必须能在目标日开店前到货；到货太晚不能用于解决当天缺货 |

过期、受损或不适用库存不能计入可用存量。程序不判定食品是否安全，也没有实现批次保质期优化。所有默认业务数字都是模拟情景，不是适合任何店的标准。

## 4. 保留了什么，修正了什么

**保留：** Llama 自行结合信息估计销量；没有设置“下雨减少 X%、节假日增加 Y%”的固定需求公式。每次正常预测调用模型 10 次，各用不同随机种子；剔除两个最大和两个最小值，平均剩余六个。

**修正：**

- 原脚本实际是 10 轮、每轮 2 次调用，最多 20 次。新版通常是 10 次，格式不合格的轮次最多重试一次；重试记录可查。
- `temperature` 放进 Ollama 的 `options`；默认 0.35 是待验证配置，不声称已经调到最优。
- 使用 JSON Schema 约束与二次验证；不从任意文本中截取大括号后就相信它。
- 不再固定要求采购 5–80 kg。100 杯、每杯 18 g 的需求只有 1.8 kg；还需要扣除已有和在途库存。
- 至少且恰好取得 10 个有效预测才汇总。失败就阻止建议，不用 5 个样本丢掉 4 个后冒充稳定平均。
- 将店主意见与其他信息一次性提供，避免第二次“复审”重复放大同一猜测。
- 展示全十轮的极差，避免截尾后掩盖严重分歧；分歧超过均值的 50%会提示。50% 是工程审查阈值，不是统计显著性。
- 保存模型名称、摘要、Ollama 版本、随机种子、输入哈希、实际参考证据和每次尝试。
- SQLite 保存每次运行，不再每次运行清空同一个日志文件。
- 模型不能执行代码或批准订单；输出负数、非有限数、超产能值、虚构证据编号会被拦截。
- 明天星期由日期计算；仍可核对原脚本手填的星期，出现不一致就报错。

**采购算术仍需公式，这是物料核算，不是用固定公式代替 AI 需求预测：**

预计用量 = AI 预测杯数 × 每杯克数 ÷ 1000。

采购缺口 = max(0，预计用量 + 安全库存 − 明日开店前可用存量 − 确认在途)。

按每包 kg 向上取整；如果超过储存/单次采购上限或交期不符，阻止审批，不静默改成一个看似合法的数。

## 5. 怎样“学习”和怎样验证

重复向同一个模型提问，不会改变模型权重；十个回答彼此相关，平均后仍可能共同偏高或偏低。新版提供三种不同层面的改进：

1. **历史证据：** 从最多 366 天模拟记录中选最近 7 天和更早最多 4 个同星期样例传给模型。这是提供上下文，不是权重训练。
2. **时间顺序回测：** 预测只见当时已知信息；`actual_cups` 留在评估器里，不传给模型。比较 AI、昨日销量基线和近期同星期均值基线。
3. **可选偏差校准：** 至少 30 条同店、同模型配置的预测/结果配对，前 60% 日期拟合平均误差，后 40% 日期独立检查。只有留出 MAE 比未校准模型与两个基线都改善超过 10%，才允许显式选择用于之后新日期。这只是一个可验证的实验校准，不是 Llama 微调。

```powershell
# 先检查模型，避免对离线端点反复请求
python cafe_ollama_stock.py doctor

# 真实模型回测；14 案例需要正常情况下 140 次推理，可能较慢
python cafe_ollama_stock.py evaluate examples/synthetic_backtest_cases.json --limit 14 --out runtime/live_evaluation.json

# 如果要评估偏差校准，需要至少 30 个完整案例；样例提供 40 个
python cafe_ollama_stock.py evaluate examples/synthetic_backtest_cases.json --limit 40 --out runtime/live_evaluation_40.json
python cafe_ollama_stock.py calibrate runtime/live_evaluation_40.json --out runtime/calibration.json

# 只有通过留出检查的校准文件才可用于其留出日期之后的输入
python cafe_ollama_stock.py predict examples/input.json --calibration runtime/calibration.json --out runtime/result.xlsx
```

**这些命令尚未对真实模型跑通，不存在已训练好的校准文件。** `examples/synthetic_backtest_cases.json` 是人为规则加随机扰动生成的合成测试集，其生成方式和固定种子公开在 `make_examples.py`；不是模型自己生成答案再拿来证明自己正确。合成回测表现不能证明真实经营效果。

指标解释：MAE 是平均差多少杯；WAPE 是总绝对误差除以实际总杯数，实际总杯数为零时不计算。低估/高估杯数统计不等同于实际缺货或浪费，因为真实后果还受库存和保质期影响。

没有先设置“预测准确率 90%”的承诺。是否值得微调 Llama，需要先有可靠标签、明确成本收益和受控回测。当前不应靠不断调整提示词直到某一组样例好看来冒充泛化能力。

## 6. 比赛与部署

- 2026-10-21 重新读取的 Domain 2 要求：核心任务从触发、连接数据、决策到人工批准、可见结果和记录；AI 只在有价值的环节使用。并非要求所有算术由 AI 猜。
- 当前版本只允许 `data_source=synthetic`。其他店铺验证只能了解流程或测试交互，不把真实个人/顾客/公司记录塞进比赛数据。
- Replit 必须作为方案的一部分。可以把本项目上传 Replit，在 Shell 安装依赖并执行 CLI 预测/回测，保留项目历史和运行证据。Tkinter 桌面界面不适合直接部署成 Replit 网站；本次没有声称已经部署 Replit。
- 若使用 GPU 推理、训练或微调，赛事要求 RunPod。默认本地模式在 Ollama 请求中设置 `num_gpu=0`，仅 CPU；运行时还应检查 `ollama ps` 的处理器信息。
- 配置 RunPod 模式时使用你自己的受保护 HTTPS Ollama 端点，保留 Pod ID、GPU 和运行时长截图。程序没有内置 RunPod 密钥、没有创建或付费启动实例。

```powershell
$env:CAFE_OLLAMA_URL = "https://你的Pod代理主机.proxy.runpod.net"
$env:CAFE_INFERENCE_MODE = "runpod_gpu"
$env:CAFE_MODEL = "llama3.1:8b"
# 若你的端点由认证网关保护，在环境变量中设置 CAFE_OLLAMA_TOKEN，勿写入代码或提交仓库
python cafe_ollama_stock.py doctor
```

该配置要求端点兼容 Ollama 的 `/api/version`、`/api/tags`、`/api/generate`；它不是直接把 RunPod Serverless URL 填入即可使用的通用适配器。

`ORBYTE_INTERFACE.md` 说明了可复用模块与未来共享平台的接口假设，没有使用真实 Orbyte API。提交材料应声明 Codex/ChatGPT 的代码与文档协助、Llama 模型、Ollama、openpyxl 和模拟数据生成方式。

## 7. 文件与测试

| 文件 | 用途 |
|---|---|
| `cafe_ollama_stock.py` | 主入口，兼容你的原文件名称 |
| `cafe_core.py` | 模型调用、十轮汇总、采购核算、审批日志 |
| `cafe_gui.py` | Windows 本地图形界面 |
| `cafe_io.py` | Excel/JSON 导入导出 |
| `cafe_eval.py` | 回测、偏差校准 |
| `examples/` | 模拟输入模板与独立保留标签的合成案例 |
| `original/` | 你上传的原脚本备份，未改动 |
| `reports/` | 已执行的工程测试与真实模型连接状态 |
| `run_tests.py` | 工程测试入口 |
| `vendor/` | 随包携带的 Excel 依赖及原许可文件 |

运行工程测试：`python run_tests.py`。测试使用明确的模拟模型和本地 HTTP 接口替身；测试通过只表示这些程序行为符合约定。

部署到真实经营场景前仍需：独立验证预测效果、真实身份认证、多店数据隔离、库存与采购系统同步、数据保护与备份，以及业务负责人确认适用范围。本版不是自动采购系统。

## 8. 参考来源

- [Domain 2 原题与评分](https://drive.google.com/file/d/11wqbeO-HYep-DJPQ-LPZnrZfQvCgr5BU/view)
- [参赛手册：Replit / RunPod / 提交规则](https://drive.google.com/file/d/1fGBS-jevDj_FrF8wsbYNPzyU5TTrfk1V/view)
- [Ollama Generate API](https://docs.ollama.com/api/generate)
- [Ollama Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs)
- [Ollama 参数：temperature / seed](https://docs.ollama.com/modelfile)
- [Llama 3.1 8B 模型](https://ollama.com/library/llama3.1:8b)
- [Forecasting: Principles and Practice：预测误差](https://otexts.com/fpp3/accuracy.html)
- [Forecasting: Principles and Practice：按时间回测](https://otexts.com/fpp3/tscv.html)

来源核对日期：2026-10-21。程序版本：2.1。模型权重没有随包提供，也没有改变模型的原有许可。
