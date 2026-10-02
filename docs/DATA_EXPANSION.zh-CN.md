# 数据扩量：先补采集能力，再验证样本质量

补充：本次云端恢复期间另做了多实例 Mastodon / Bluesky API 探测，并核对 YouTube、
Reddit 的接入条件，见 [社交来源调查与建议](SOCIAL_DATA_SOURCES.zh-CN.md)。
以下 1,485 事件记录保留为较早一次采集结果，不与新探测统计混加。
现已完成 [原搜索词复用与样本审核](SEARCH_REUSE_AUDIT.zh-CN.md)：原词库适配 public
hashtag、跨实例去重、45 条 AI 初审，以及 809 条候选原始记录的云端入库/读回。

## 本次真实验证

2026-10-01（悉尼时间）对 Jetstream legacy v1 做了有上限的短时采集：
读取 1,485 个事件、约 1.23 MB，12 条帖子通过当前英语/交通主题规则，
12 条成功创建，并全部通过固定版本 RoBERTa 处理。
这是全球交通关键词样本，不是“12 条澳大利亚代表性样本”。

对 Bluesky public search 的历史回填请求收到 HTTP 403。程序保留了明确
错误，没有把拒绝访问算成“检索结果为空”。EIA 需要有效 API key；
本次没有凭空使用或编造价格数据。未找到可直接导入的 Assignment 1 原始
NDJSON，因此没有声称已导入那批真实数据。

10 万条压测记录、720 条演示帖、60 条演示新闻均为 **synthetic**。
它们验证工程容量，不能作为真实采集成果写进简历。

## 优先顺序

| 路线 | 解决的问题 | 已提供实现 | 实际限制 |
|---|---|---|---|
| Assignment 1 / 合法历史导出 | 快速获得更大的真实历史语料 | NDJSON/gzip 流式读取、格式适配、稳定 ID、断点、隔离坏行 | 需要真实文件及使用权限；未知格式先显式转换 |
| Bluesky 按日期×查询回填 | 避免单次最新搜索截断历史 | 每页 cursor、请求预算、重复游标检测、窗口截断审计 | 当前公共搜索 403；搜索可见性不等于完整档案 |
| Jetstream 连续增量 | 持续积累新帖，避免只依赖关键词搜索分页 | v1 微秒 cursor、重放去重、预算、删除事件处理 | 只能获得流及可回放范围；不是 2–5 月历史恢复工具 |
| GDELT 自适应时间切片 | 突发新闻日超过单窗 250 URL | 满窗拆分、URL 去重、确定性候选排序、截断日志 | API 保留窗口、源站拒绝/失效、全文提取失败仍存在 |
| 多 Mastodon 实例 | 扩大平台覆盖，减少单实例偏差 | 服务端作用域 ID、认证配置、时间窗采集 | 搜索权限与实例索引范围不同；需要分别运行/标记实例 |

官方依据：
- [Jetstream legacy 文档](https://github.com/bluesky-social/jetstream-legacy)：
  此实现明确采用 legacy v1 的 time_us 游标，不混用新协议的序列游标。
- [新 Jetstream 文档](https://github.com/bluesky-social/jetstream/blob/main/docs/README.md)：
  协议迁移必须另写适配器并重新验证参数、游标和删除语义。
- [GDELT DOC API 原始说明](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/amp/)：
  按文档的滚动检索窗口与单次 ArtList 上限设计，不假定当前接口能完整恢复
  数月前的数据。更老的历史应优先使用原始课程数据或有明确许可的存档。
- [Mastodon 搜索接口](https://docs.joinmastodon.org/methods/search/)：
  核对具体实例的认证与搜索能力后再安排采集。

## 可以直接执行的命令

~~~powershell
# 文件格式可以是平台记录、ES _source 导出，或文档约定的标准字段。
uv run python -m backend.ingestion.import_ndjson data/imported/assignment1.ndjson.gz --source-dataset assignment1_2026 --topics-only --checkpoint data/checkpoints/assignment1.json

# 明确分割时间与请求预算；HTTP 403/401 需要解决访问条件，不能靠无限重试。
uv run python -m backend.ingestion.backfill --start 2026-09-01 --end 2026-09-07 --query "public transport Australia" --query "petrol prices Australia" --request-budget 30

# 当前时间段的增量。第二次运行沿用同一个 cursor 文件。
uv run python -m backend.ingestion.jetstream --seconds 600 --max-events 100000 --max-mb 100 --australia-only --checkpoint data/checkpoints/jetstream-au-v1.json
~~~

导入标准最小记录：

~~~json
{"platform":"bluesky","post_id":"at://did:plc:example/app.bsky.feed.post/1","raw_text":"Melbourne tram fares are expensive.","created_at":"2026-09-01T12:00:00Z"}
~~~

每条记录还应尽量提供 parent_post_id、thread_root_id、lang 和可追溯的来源。
未知 Assignment 1 包装结构会明确拒绝，不猜字段。拒绝记录保存行号、
错误类型和内容哈希，原始输入文件仍是修复依据。gzip 恢复通过跳过已提交
行实现，内存有界，但恢复时读取旧前缀仍有 I/O 成本。

## 扩量目标与验收门槛

下面是下一批真实数据的目标，不是本次已完成数量。

1. 先取得 1–5 万条去重后的真实帖子，覆盖至少 8–12 周。
   按平台、周、主题报告数量，不把同一帖多次检索算多条。
2. 观察独立作者/线程数、直接匹配与继承主题占比、缺失父帖比例、
   英语占比、地区未知比例和单个作者贡献上限。
3. 每个平台/主题抽样核验相关性。battery、station、drive 等词可能有
   非交通含义；地区关键词也不能证明作者居住地。澳洲分析需单独限定样本。
4. 扩至 10 万真实记录前，运行断点恢复、队列积压恢复和资源压测。
   以合格且可解释的样本为目标，不设“无论质量都抓满百万”的任务。
5. 将有效样本量、失败率、限流次数和新增长期曲线作为交付物。
   缺失时间窗要显式显示；扩大当前数据不能补造历史事件期间的观测。

GDELT 的 seen time 是发现/索引时间，不必然等于文章发布时间。
当前新闻采样按关注度分配日预算，仍会偏向关注度高且可访问的媒体；
候选 hash 排序不能消除这些偏差。应分别做每日报告与来源分布检查。

## 后续值得做的分析

在真实数据足够后，比较关键词命中与主题继承的增量、不同平台/模型
的分歧、采集缺口与结论敏感性。油价与情绪的相关分析要同时报告样本量、
滞后搜索与序列相关问题。若要回答“冲突导致交通偏好变化”，需要额外
的研究设计、对照与混杂因素，当前相关图不提供这一结论。
