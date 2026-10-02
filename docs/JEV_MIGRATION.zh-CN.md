# Jev 替换 RoBERTa：评分合约与待上线方案

2026-10-01。用户明确的 Jev 是 TypeSafe Jev，入口为
[awesome-jev-zh](https://github.com/yzfly/awesome-jev-zh)。该仓库是非官方精选资料清单，
不是 Jev 模型权重或可直接部署的推理服务。

**状态：真实 Jev 已接通并完成小样本对照，尚未切换云端流量。**
用户已提供并授权使用凭据；完成 80 次真实 API 推理，覆盖 38 条不同真实帖子、6 条构造样本，
包含两个 rubric 版本的重复对照。详见 [真实验收与样本审核](JEV_LIVE_VALIDATION.zh-CN.md)。
此前 26 条结构化标注来自 GPT-4.1 mini，1.43 秒延迟也是 GPT 试验值；不得将这些数字归给 Jev。

## 为什么可以替换

Jev 是面向软件的类型化决策模型，输入文本与明确问题，输出预先定义的选项或评分。
它不通过自由文本生成来返回情感解释。官方提供 Choice、Score、Noul 三类问题，
其中 Score 返回等级概率、概率加权分数和单独的 confidence。
这与本项目的情感分类、相关性过滤、对象态度判断直接对应。
参考：[System One](https://docs.typesafe.ai/concepts/system-one)、
[Score](https://docs.typesafe.ai/primitives/score)、[API](https://docs.typesafe.ai/api)。

实现固定 `jev-1.13.0`，请求只发送到 `https://api.typesafe.ai/v1/systemone`，
读取 `TYPESAFE_API_KEY` 或 `TYPESAFE_API_KEY_FILE`。OpenAI 密钥不能使用，也没有 LLM 回退。
当前官方价为每百万输入 tokens US$0.042、输出不计费；本轮按服务端 token 用量估算总费用
US$0.009604854，未核对账单或免费额度抵扣。
同一请求中加入问题仍会增加输入用量。模型版本与访问条件见
[官方模型文档](https://docs.typesafe.ai/models)。

## 正负面与数值的定义

主指标采用三档 Score，让它判断文本属于哪一种可描述的情况：

| Jev 档位 | 判断标准 | 项目中的符号 |
|---|---|---:|
| 0 | 表达不满、批评、反对或负面评价 | −1 |
| 1 | 客观描述，未表达正面或负面评价 | 0 |
| 2 | 表达满意、赞同、支持或正面评价 | +1 |

Jev 返回原生等级期望 `native_score = 0×P₀ + 1×P₁ + 2×P₂`，范围是 `[0,2]`。
项目在代码中做归一化：

```text
sentiment_value = P(positive) − P(negative) ∈ [−1, +1]
# 理想精确概率下等于 native_score − 1；实际返回值存在独立舍入。
```

在精确概率和为 1 时，两式等价。真实 API 曾返回 `score=1.01`、
`probabilities={0:0,1:0.98,2:0.02}`，后者的期望为 1.02，因此不能要求二者逐位相等。
当前 `jev-rounded-wire-v2` 仅当全部数值位于百分位网格时给予有限舍入容差：
每个概率最多 0.005、三档期望与原生 score 差值最多 0.02；高精度数据仍严格校验。
不修改原始概率或 score，另外保存 `probability_sum` 和 `score_expectation_delta`。
范围、类型、类别、模型快照、档位描述及超出容差的算术错误仍拒绝。
这是依据本轮观测制定的兼容规则；官方 SDK 的概率定义亦为近似和 1，
不是保证服务端永远使用两位小数。[SDK 定义](https://github.com/typesafe-ai/typesafe-sdk-python/blob/main/src/typesafe_sdk/_schemas/models.py)

以下是**协议测试示例，非真实 Jev 实测**：

```json
{
  "probabilities": {"negative": 0.10, "neutral": 0.15, "positive": 0.75},
  "native_score": 1.65,
  "sentiment_label": "positive",
  "sentiment_value": 0.65,
  "confidence": 0.80
}
```

这里的 0.65 是基于预测分布的正向倾向，不表示“65% 的用户满意”，也不是情绪强度的物理量。
标签取概率最大的档位，分数取概率期望；中立标签仍可有接近零但非零的分数。
confidence 是服务方从分布导出的不确定性统计，不是正负方向，也不能当成已在本项目
验证过的正确率。参考：[Confidence](https://docs.typesafe.ai/confidence)。

五档 Score 也可扩展为更细的评价量表，再用 `2×score/(档位数−1)−1` 归一化。
本次先用三档保持 `P(positive)−P(negative)` 与旧指标定义一致，避免在迁移时同时更换测量尺度。
不同模型的分数分布仍可能不同，不能直接把迁移前后拼成一条无断点的时间序列。

## 防止把“不知道”当“中立”

模型在同一请求中独立判断整体表达状态：single、mixed、insufficient。
程序组合状态与 Score，不假设某个问题能看到另一个问题的答案。

- mixed：真实正负评价并存，保存 mixed，正式分数设为 null。
- insufficient：缺少图片/上下文、歧义讽刺或正文不足，保存 unclassified 和 null。
- 任一关键回答 confidence 低于暂定阈值 0.6：进入待复核，分数为 null。
- 输入超过 6,000 字符：记录截断；正式分数为 null，避免把不完整文本当完整观察。
- 明确的客观、中立表述仍可获得有效的中立结果。

0.6 是等待人工标签校准的初始门槛，不是推荐给所有任务的通用值。
所有原始档位概率、未经过门控的分数、状态和拒绝原因仍保留，便于后续调整门槛。
缓存存原始类型化回答，改变门槛会重新计算门控，不重复调用模型。

例如 `P(negative)=0.5, P(neutral)=0, P(positive)=0.5` 的期望也是 0，
但它和 `P(neutral)=1` 完全不同。因此不能只存一个数字或把所有零分解释为中立。
混合/待复核记录不进入正式均值与正负中立分母，另报数量和覆盖率。

## 整帖情绪与对象态度

每条帖子一次请求，共 12 个独立问题：

- 主题相关性、内容类型各一个 Choice。
- 整体表达状态一个 Choice，整体情绪一个 Score。
- 公共交通、燃油成本、电动车、燃油车驾驶四类对象，各一个态度证据状态 Choice 和一个 Score。

公共交通判据明确排除油轮/货运水道；燃油成本排除食用油。
对象状态区分 expressed、mixed、factual_only、not_mentioned、insufficient。
只在主题明确相关、态度明确表达且置信度通过时，接受对象分数。
事实新闻本身不建立发帖者态度，未提及的对象也不能被赋予中立分数来凑齐四列。

原有 API 聚合是整帖文本按候选主题分组，不能因此称为“对该主题的态度”。
新候选 schema 使用 `target_sentiments` nested 字段保存对象分数、接受状态和置信度，
以后按目标对象聚合时可同时约束 target 与 accepted，防止数组跨对象误匹配。
完整 Jev 诊断保存在 `jev` 对象中，不做动态字段索引。

当前 `transport-jev-v2` 要求引用、标题和文章摘录之外另有明确的作者赞同/反对，
且不从燃油费用或换乘行为推断对燃油车驾驶的态度，也不从泛指 car 推断发动机类型。
这降低了已发现的错误归属，但本人投诉也可能因暂定置信度门槛被拒绝，仍须校准覆盖率。

Jev 不生成解释或任意引用。若后续需要定位原文证据，可先由程序切分出有限句子候选，
再让 Jev 用 Choice 选择句子编号，程序取回原文；选择正确与否仍需审核。
本次适配没有伪造解释或证据字符串。

## 已实现内容

- `backend/data_process/jev_sentiment.py`：官方 HTTP 协议、固定版本与 rubric、概率/分数校验、
  整帖及对象态度门控、预算、缓存、失败响应审计，以及 `classify` / `classify_many` 接口。
- `processed_document`：输出兼容原有 schema_version=2 的候选记录，保留原文和来源；
  `contextual_sentiment_polarity` 是方向值，旧 `contextual_sentiment_score` 字段仍保存置信度。
- `database/mappings/social_posts_jev.json`：隔离的候选索引 schema，尚未部署到云端。
- `scripts/run_jev_pilot.py`：默认只生成请求文件；带 `--live` 才调用 Jev，最多 30 条、默认预算 US$0.50。
- `test/test_jev_sentiment.py`：19 项协议、数值、缓存、拒绝逻辑与真实本地 ES 测试。
  HTTP 使用明确标识的协议模拟响应，不能作为 Jev 准确率证据。

上阶段完整回归为 175 项通过、1 项原 RoBERTa 权重测试明确跳过，记录保留于
[适配验收记录](evidence/jev-adapter-2026-10-01.json)。本轮针对实际修改运行 19 项 Jev
回归，包含真实本地 ES 落库，全部通过；另对 30 条真实 Jev 结果做缓存重放，0 次 HTTP，语义字段一致。

同一条已接受模型回答按输入哈希、固定模型与 rubric 合约复用；没有自动付费重试。
HTTP 错误会停止后续调用，也不会回退到 RoBERTa/GPT。网络超时保留预算预留，避免误认为未消费。
历史代码和测量结果保留以便回滚及对照；“全面替换”指正式新处理路径和查询使用 Jev，
不是删除历史模型证据。

## 当前可以执行

从 `Desktop/cloud cluster/COMP90024_group_repo` 运行：

```powershell
# 无密钥也可运行；本轮已为相同的30条分层样本生成请求文件。
uv run --no-sync python -m scripts.run_jev_pilot `
  --input data/mastodon_global_en_20261001/review-sample.json `
  --directory data/jev_prepared_20261001 --limit 30
```

凭据通过 `TYPESAFE_API_KEY` 或 `TYPESAFE_API_KEY_FILE` 读取。
本机已保存于 Git 忽略且限制当前 Windows 用户访问的私有文件，运行时只传文件路径。

```powershell
uv run --no-sync python -m scripts.run_jev_pilot `
  --input data/mastodon_global_en_20261001/review-sample.json `
  --directory data/jev_live_pilot --limit 30 --budget 0.50 --live
```

此命令生成本机候选数据和报告，不写云 ES、不切换 API、不创建云资源。
格式/API 失败会非零退出；被门控的语义不确定回答作为成功返回但未评分，单独统计。

## 正式替换顺序与当前进展

1. **已完成小样本接通**：固定模型、30 条分层样本、6 条构造诊断与 8 条本人经历样本；
   报告请求时延、token 费用和 AI 复核。独立人工标签、全面数值/否定测试及服务端负载验收仍待完成。
2. 依据人工标注调 rubric 和置信度门槛；开发集与测试集按线程/近重复组隔离。
   三分类报告 macro F1；概率报告 Brier score/可靠性图；不要用模型自报 confidence 代替准确率。
3. 显式迁移 `social_posts_jev` schema。重新从云 raw 读取首次观察，沿用英语、正文与文本去重规则，
   先处理既有的 1,047 条合格样本；其余 356 条保持原始数据和排除原因。
4. 将常驻 Job/worker 的模型工厂切到 Jev，以专用 Secret 传递 TypeSafe key，保留总预算、
   限流、幂等写入与失败队列。无需每个 worker 加载 RoBERTa/Torch 权重。
5. API 和分析脚本切换到 Jev 索引：整帖与对象指标分别查询；返回模型、rubric、评分版本，
   null 不当零，混合和待复核比例单列。历史 RoBERTa 留作显式对照，默认聚合不混用。
6. 全量读回、断点续跑、服务失败与恢复测试通过后，关闭旧推理调度，完成正式切流。

凭据已不再是阻塞。当前尚未通过正式切换的质量验收：需解决真实投诉的拒绝率、作者归属和
数据组成偏差。小样本试验已经真实调用 API，未进行全量重算、云索引迁移或 API 切流。

## 不需要先微调

官方当前说明，同一模型权重服务所有账户，不提供基于客户数据的微调或 LoRA。
适配方式是 state、instructions、criteria 和原子问题组合。
首先解决标注定义、相关新闻与作者态度混淆、目标归属和阈值；不要把“换模型”当成已解决数据质量。
参考：[模型定制边界](https://docs.typesafe.ai/models#customizing-jev)。

Jev 官方亦披露数值运算、间接判断、长噪声上下文、对抗性输入等局限。
数值换算由代码执行；模型只判断语义。固定输出形式不能保证判断正确。
参考：[Jev 1.13 已知局限](https://docs.typesafe.ai/model-jaggedness/jev-1.13)。
