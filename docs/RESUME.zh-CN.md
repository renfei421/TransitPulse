# TransitPulse：简历与面试材料

身份建议：**课程团队项目基础上的独立工程扩展**。初始团队工作与个人后续模块在面试中分别说明；不要将整份原团队作业描述为个人从零独立开发。

## AI 工程 / 应用基础设施版

**TransitPulse｜多源数据与成本受控 AI 推理平台**  
Python · Kubernetes · Fission · Elasticsearch · Docker · Jev API

- 构建多源交通舆情处理流程，在 91 天事件窗口内采集并去重 124,491 条帖子，完成 90,576 条目标级情感推理，保存来源、筛选决策、模型版本与原生响应。
- 实现持久化调用账本、跨重启预算控制、缓存恢复与有界重试，追踪 90,587 次调用尝试，在 US$15 预算内完成全量处理，累计模型记账约 US$11.73。
- 在 Kubernetes/Fission 上部署可追溯分析 API，支持精确模型版本过滤与旧基线查询；原发布通过 201 项本地测试及 22 项云端路由/模型切换检查。

已通过恢复验收，可选替换第三条（证据：`docs/evidence/archive-restore-verification.json`）：

- 建立完整数据归档与本地 Docker 恢复流程，通过全部文档指纹及云端/本地 API 聚合比对，实现无需云端和模型 API 的结果回放。

英文版本：

- Built a multi-source transport discourse pipeline covering 124,491 deduplicated posts over a 91-day event window; completed 90,576 target-level sentiment evaluations with traceable inputs, routing decisions and model contracts.
- Implemented a durable inference ledger, cross-restart budget enforcement, cached-response recovery and bounded retries; tracked 90,587 request attempts and completed the cohort with approximately US$11.73 in model cost accounting under a US$15 cap.
- Deployed version-filtered analytics APIs on Kubernetes and Fission, preserving explicit access to the earlier baseline; validated the release with 201 passing local tests and 22 cloud API checks.

## 数据分析版

**交通舆情与能源价格事件窗口分析**  
Python · SQL/SQLite · Elasticsearch · 统计分析 · 数据质量 · 可视化

- 整合 Bluesky、Mastodon 的 124,491 条去重帖子与 63 个交易日的 Brent 价格，建立来源、时间、主题、语言与模型有效性的分层质量检查。
- 基于 90,576 条结构化推理结果，分析公交、燃油成本及电动车等对象的态度变化，区分作者态度、事实转述和拒判，避免将缺失对象视为中性。
- 完成事件前后分组、交易日对齐和区块 bootstrap 关联分析；在 52 个有效日期对中未观察到明显同期线性关联，并识别检索覆盖、平台构成与低样本日的限制。

英文版本：

- Integrated 124,491 deduplicated Bluesky/Mastodon posts with 63 trading days of Brent prices, documenting source coverage, topic/language filtering and model eligibility at each stage.
- Analysed 90,576 structured model outputs to separate attitudes towards public transport, fuel affordability and electric vehicles from factual reporting and unmentioned targets.
- Compared event-window phases and aligned sentiment with observed trading days; block-bootstrap analysis of 52 paired dates found no clear same-day linear association, with explicit coverage and selection-bias limitations.

## 一分钟项目介绍

“这个项目研究油价变化窗口中的交通讨论，但难点不只是情感分类。帖子可能同时赞扬公交、抱怨油价，平台检索也存在截断。我把原始数据、筛选决策、具体对象的态度和拒判分开保存。工程上，外部模型调用可能成功但客户端超时，所以我在调用前保存费用预留，之后保存原生响应，再写结果，并限制跨重启的重试次数。最终完成九万多条推理，模型记账约十二美元。分析没有找到明显的同日油价收益与燃油态度线性关联，我保留这个结果和覆盖限制，而不是把图上的同期变化解释为因果。”

## 面试深挖问题

| 问题 | 可证实的回答重点 |
|---|---|
| 为什么不一次失败就重跑？ | 外部请求是否收费可能未知；回执、缓存和预算需要跨进程持久化。 |
| 能否保证 exactly-once？ | 不能保证远程供应商与本地数据库的跨系统事务；能复用已保存响应，并控制不确定重试。 |
| 为什么不用整帖情感？ | 态度对象可能相反；新闻坏消息不等于作者反对公交。 |
| 为什么不是大数据/生产系统？ | 已验证十万量级数据、恢复与查询，未进行生产高并发压测或多节点故障容灾。 |
| 为什么不微调？ | 优先建立人工标注和边界评测；缺少金标时微调效果无法可靠判断。 |
| 没有显著关联是否失败？ | 没有强行制造结论；覆盖、缺失处理和可复核的负结果本身是分析产出。 |
| 与 Agent 的关系？ | 当前是有界推理与分析系统，不是自主规划 Agent；可以作为未来 Agent 的受控数据工具层。 |

避免写：未经测量的准确率提升/成本降低百分比、自研 Jev、生产级 KEDA 扩容、已部署多 Agent、冲突导致态度变化、已完成尚未做的人类标注。
