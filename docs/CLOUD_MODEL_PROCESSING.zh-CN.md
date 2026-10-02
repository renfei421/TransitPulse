# 云端模型处理与 LLM 结构化标注验收

2026-10-01。工作目录为 `Desktop/cloud cluster/COMP90024_group_repo`。
后续用户明确要求改用 TypeSafe Jev；[Jev 迁移方案](JEV_MIGRATION.zh-CN.md)记录适配与待上线状态。
本页保留实际 RoBERTa/GPT 验收结果，不能将 GPT 的性能数字解释为 Jev 性能。
本轮接通真实采集数据 → 语言与正文筛选 → 云端 RoBERTa → Elasticsearch → Fission API，
同时实测小样本 LLM 结构化标注。没有微调，也没有把 AI 标注当作人工金标准。

## 已完成的闭环

| 项目 | 实测结果 |
|---|---|
| 云端原始记录 | 1,403，首次正文、来源与发帖时间逐条保留 |
| RoBERTa 处理并入库 | 1,047，创建 1,047，写入失败 0 |
| 筛选决策记录 | 1,403，每条可查原因、政策版本、文本指纹 |
| 模型阶段耗时 | 382.047 秒，包含加载、筛选、推理和写入，不是纯推理基准 |
| Kubernetes Job 总历时 | 09:36:54–09:45:17 UTC，约 8 分 23 秒，包含准备 |
| RoBERTa 输出 | positive 149 / neutral 681 / negative 217；6 条超过 512 tokens 被截断 |
| LLM 样本 | 30 条唯一帖子，首次 25 条通过；补查 5 条中 1 条通过；最终保存 26 条 |
| 新 API | `/api/v1/social/annotations`；RoBERTa 保留在 `/social/posts` 和聚合接口 |
| 验证 | 160 项测试全部通过；真实云端全量读回和分页验证另行通过 |

基线测的是**整帖文本情绪**，不能将负向战争新闻解释为用户反对公共交通。
候选主题仍来自词库，当前 1,047 条不是人工确认的高相关帖子。
这是一轮有界批处理验收，尚未成为持续采集、自动调度和自动扩缩容服务。

## 可审计的数据筛选

| 顺序 | 剩余 | 排除原因 |
|---|---:|---|
| 已按帖子 ID 去重入库 | 1,403 | 前两次采集的重叠记录没有覆盖首次观察 |
| 平台声明英语 | 1,203 | 非英语 97、语言未知 103 |
| 至少 6 个正文词 | 1,185 | 去除链接、标签、提及后正文不足 18 |
| langid 识别英语且分数 ≥ 0.8 | 1,091 | 判为其他语言 71、分数不足 23 |
| 标准化正文去重 | 1,047 | 重复 44，按 doc_id 排序确定保留对象 |

`langid==1.1.6` 的分数不是经本项目校准的“英语准确率”；语言未知的帖子没有被自动猜成英语。
URL/提及标准化版本为 `url_mention_placeholders_v1`。旧采集正文有被 HTML 清洗拆开的 URL，
现有规则不声称能完整还原它们。原始正文不被标准化结果覆盖。

处理后内容形式为：link_post 720、commentary_candidate 165、other_text 158、promotion_candidate 4。
这些是启发式标签，第一人称也可能来自引用，链接帖子也未必是新闻。
增加 `content_kind` 过滤是为了分层检查，不是宣称已有 165 位真实用户表达了个人态度。

全球批次清空处理结果中的旧澳洲事件窗口字段，保留原始记录供溯源。
API 支持显式 `timezone=UTC`，默认仍为 Australia/Sydney，维持旧调用兼容。
27 条缺少父帖上下文；本次明确使用 `--local-only`，没有冒充完成对话级上下文推理。

## 模型部署方式

- 模型：`cardiffnlp/twitter-roberta-base-sentiment-latest`。
- 固定 revision：`3216a57f2a0d9c45a2e6c20157c20c49fb4bf9c7`。
- 实际运行版本：torch 2.8.0+cpu、transformers 4.56.2、langid 1.1.6。
- Job：`transport-model-b7c1ca29ae`，限 2 CPU / 3 GiB，最大 2,000 条、最长 1 小时、失败不自动重试。
- 批量推理 8 条，处理分组 32 条；截断保留前 512 tokens，记录原 token 数和截断标记。
- 源码以 SHA-256 校验归档上传；应用代码不写入 ConfigMap。ES 凭据来自 Secret，并严格验证 TLS。
- 初始化容器使用固定 Python 镜像及带哈希的锁定依赖，下载固定模型；处理容器使用离线模型缓存。
- 本轮只用已有节点和 emptyDir，没有新增 PVC、节点或公网负载均衡器。

这是适合短期验收的部署折中：初始化仍依赖软件源和模型站点可用性。
长期运行应把依赖和权重构建进现有 Dockerfile 的 model 镜像目标，发布不可变镜像。
已完成的 Job 配置 24 小时 TTL；日志和状态已另存 artifacts。TTL 不会释放原有集群和磁盘费用。

复现时先按数据库迁移流程显式建立两个新 schema：`social_processing_decisions`、`social_posts_annotations`；
处理进程不创建 mappings。固定源数据的重跑会更新 processed 记录的运行时间，不是零写入回放。

```powershell
uv sync --frozen --extra model --extra notebook
uv run --no-sync python -m scripts.cloud_model_job `
  --kubeconfig '..\k8s-1-34-10-do-5-sgp1-1790830229700-kubeconfig.yaml' `
  --source-dataset mastodon_query_reuse_20261001 `
  --source-dataset mastodon_global_en_20261001 `
  --start 2026-10-01T00:00:00Z --end 2026-10-02T00:00:00Z --max-documents 2000
```

提交命令只负责上传并启动。确认其返回 Job 的 Complete 状态后，使用
`scripts.verify_cloud_models --kubeconfig <path> --job-name <returned-name>` 做读回验证。
该脚本针对本次数据验收窗口，不是任意规模历史库的通用监控。

## LLM 能提供什么，以及实测缺陷

本次使用固定快照 `gpt-4.1-mini-2025-04-14` 和严格 JSON Schema，输出：
主题相关性、内容类型、整帖情绪、各交通/能源对象的态度及原文证据、明确的出行模式转变、复核标记。
这是任务型信息抽取，暂不需要 Agent 工具调用或微调。

LLM 调用由本机编排，结果写入云 ES；**没有声称已部署云端 LLM worker**。
API key 只读取本机已有环境变量，没有写进 Git 或 Kubernetes。
每次运行限制最多 30 条、默认预算 US$0.50，3 个并发；拒绝、超时、格式/证据错误不会自动产生付费重试。
缓存键包含帖子 ID、完整输入哈希、模型快照、提示词/Schema 合约哈希。
同一合约的已接受结果通过 create-only 写入保持首次记录；不同合约可以并存。

30 条来自前次分层审核样本，包括弱相关、非相关和图片依赖样本，并非只挑容易的帖子。
首次通过 25/30。为诊断失败又显式调用原 5 条，其中 1 条通过、4 条被拒绝：
2 条证据过长、2 条重复目标。总调用 35 次，最终 26 条可查询，4 条没有伪造结果补齐。
初次失败未保存具体响应；现已补上独立响应审计。诊断原因不能被倒推成初次每条失败的确切原因。

26 条有效结果的请求耗时中位数为 **1.43 秒**，最大 3.75 秒。
这不包含失败请求，也不是并发压力测试或生产 p95。
已留存 token 用量对应估算 US$0.0124736，但缺少最初 5 条被拒响应的用量，不能当完整账单。
全部 35 次请求累计保守预算预留为 US$0.1213488；真实账单以提供方为准。

我逐条做了 AI 复核，问题包括：

- S001 只有标签，模型没有开启复核；S002 仅靠末尾标签被放宽到邻近相关。
- S022 把国际油运海峡错误归为 public_transport，虽然引用确实来自原文。
- S019、S023、S030 容易把事实报道中的充电建设或燃油负担解释成发帖者态度。
- S024/S026 内容近重复，却被分为 opinion 和 news_or_link，分类稳定性不足。
- S029 识别了讽刺负面语气，但未按提示词开启讽刺复核。
- S010 能排除橄榄油噪声；S011 能承认看不到梗图的上下文；这些有用，但不构成总体准确率。

LLM 与之前 AI 相关性审核在 26 条可比记录上有 20 条一致。
两边都是 AI 标注，且边界存在主观性，这个数**不能写成 76.9% 准确率**。
全部 30 条复核意见、被拦截样本和测量限制均保存于证据目录。
`needs_review` 是模型自报；本次只有 2 条为 true，不能把 false 当作人工审核通过。

```powershell
# 已有本机 OPENAI_API_KEY 时运行；不要把密钥写到命令行、源码或截图。
uv run --no-sync python -m scripts.run_llm_pilot `
  --sample data/mastodon_global_en_20261001/review-sample.json `
  --directory data/new_llm_pilot `
  --kubeconfig '..\k8s-1-34-10-do-5-sgp1-1790830229700-kubeconfig.yaml' `
  --limit 30 --budget 0.50 --ingest
```

调用这个命令会重用成功缓存，但会再次调用之前失败的帖子；它不是无成本的状态查询。
查询已有结果请用下面的 API。

## 如何查看成果

在 `Desktop/cloud cluster` 运行 `Open-Cloud-API.ps1`，保持隧道窗口打开，然后访问：

```text
http://127.0.0.1:8088/api/v1/quality?from=2026-07-03&to=2026-10-02&timezone=UTC
http://127.0.0.1:8088/api/v1/social/posts?from=2026-07-03&to=2026-10-02&timezone=UTC&limit=25
http://127.0.0.1:8088/api/v1/social/annotations?from=2026-07-03&to=2026-10-02&timezone=UTC&limit=30
http://127.0.0.1:8088/api/v1/social/annotations?from=2026-07-03&to=2026-10-02&timezone=UTC&content_type=opinion
```

`quality` 的 raw/processed 比率是处理覆盖率 74.63%，不是主题命中率。
`content_kind` 只影响选择后的处理数量，分母仍使用同源/日期/平台原始队列。
LLM 标签不混入 RoBERTa 正负概率或总体情绪平均值。API 增补 `oil_vehicle` 主题并校验枚举，
UTC/Sydney 边界和独立存储由真实 ES 测试覆盖。

## 对项目与简历的价值

可以实证描述：在 Kubernetes/Fission 上完成 1,403 条真实社交记录的可审计处理链路，
通过语言、正文与重复筛选，对 1,047 条执行固定版本 CPU 模型推理；引入带预算、缓存、证据校验、
失败审计和独立查询 API 的 LLM 结构化标注，并通过 160 项测试及云端全量读回。

暂不声称“LLM 优于 RoBERTa”“发现公众出行模式改变”或“生产级吞吐”。本轮没有可靠的人工
目标态度标签，样本也没有明确个人模式转变实例。

下一轮优先：统一新闻/个人态度/目标归属标注规范，建立 200–300 条人工复核样本，
按帖子线程和近重复内容分组划分开发/测试集；比较目标提取、宏 F1、证据支持率、拒绝率、
延迟和每千条成本。先改提示词、目标唯一性结构和确定性的复核规则，再决定是否更换模型。
只有明确错误模式、足够可靠训练标签且提示词与检索仍不能解决时，再考虑微调/蒸馏。

## 证据与参考

- [云端 Job、存储、API 验收](evidence/cloud-model-processing-2026-10-01.json)
- [LLM 样本、费用及延迟](evidence/llm-pilot-2026-10-01.json)
- [逐条 AI 复核](evidence/llm-sample-review-2026-10-01.json)
- [160 项测试证据](evidence/model-connection-tests-2026-10-01.json)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [GPT-4.1 mini 模型及价格](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
- [CardiffNLP 模型卡](https://huggingface.co/cardiffnlp/twitter-roberta-base-sentiment-latest)
- [langid 实现](https://github.com/saffsd/langid.py)

帖子正文、模型原始响应、完整日志、模型归档和 kubeconfig 不提交到仓库。
提交的仅为代码、操作文档、汇总与哈希；此前各阶段的测量记录保留原样。
