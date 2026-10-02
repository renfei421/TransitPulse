# 社交数据扩量：访问实测与取舍

检查时间：2026-10-01。目标是与澳大利亚油价、电动车、公共交通相关、可解释来源的讨论，
不是平台总帖数。当前建议是 **先扩展多实例 Mastodon，保留 Bluesky 增量流；新增来源优先
试验 YouTube 评论，Reddit 等获得访问权限后再安排**。

## 实际探测

脚本：`uv run --no-sync python -m scripts.probe_social_sources`。
只执行有预算的公开只读请求，保存统计、不保存用户正文或身份、不写入 Elasticsearch。
原始统计见 [social-api-feasibility-2026-10-01.json](evidence/social-api-feasibility-2026-10-01.json)。

| 来源 | 探测方式 | 本次结果 | 判断 |
|---|---|---|---|
| mastodon.social | petrol/publictransport/electricvehicles/australia，各取最多 40 条 | 4 次 200，实例内去重 160；严格主题规则命中 58，其中带澳洲文本标记 6 | 当下可取，有相关候选；支持分页后进一步审查 |
| mastodon.au | 同上 | 4 次 200，160；主题 58，主题且澳洲标记 1 | 实例后缀不是用户地域证据 |
| aus.social | 同上 | 4 次 200，160；主题 56，主题且澳洲标记 8 | 可补覆盖，但需跨实例 URI 去重 |
| Bluesky Jetstream legacy v1 | 全网 post collection，60 秒预算 | 1,827 事件、1,600 条唯一非空帖子；宽主题规则 15、严格规则 2、严格且澳洲标记 0 | 流量大，当前严格目标命中少；适合持续增量和流处理工程 |
| Bluesky public search | 三组澳洲交通/油价查询，各请求最多 100 条 | 全部 403 | 当前环境无法匿名回填；不能把 403 算零结果 |

Mastodon 这 480 条是三个实例样本量之和，**没有跨实例去重，不能声称 480 条独立帖子**。
各 hashtag 的 40 条覆盖不同时间跨度（例如 petrol 可跨数周，australia 可仅跨一天），
不能直接比较日增量。严格规则是透明的文本正则代理，不是人工确认的相关性；会漏掉
缩写、连写 hashtag 和依赖上下文的回复，也可能误收国际新闻。文本中的 Australia
不证明作者在澳洲。Bluesky 的一分钟样本不能外推每日/每月相关帖总量。

## 其他平台为何没有直接替换

**YouTube：最值得试采的新来源，但相关评论规模尚未验证。** 可以从澳洲新闻频道、
交通专题视频及电动车评测中先选择视频，再沿评论线程获取上下文。这个入口有希望比
全网流更集中于目标事件，这是采集设计推断，不是已取得的数量结论。
官方 `commentThreads.list` 支持 `videoId`、分页、每页最多 100 个线程，一次请求消耗
1 quota unit。嵌入的 replies 可能不完整，需 `comments.list(parentId=...)` 补全。
公开数据使用 API key；本次没有 key，未调用评论 API，也没有承诺能采多少条。
来源：[评论接口](https://developers.google.com/youtube/v3/docs/commentThreads/list)、
[认证与入门](https://developers.google.com/youtube/v3/getting-started)、
[评论/回复说明](https://developers.google.com/youtube/v3/guides/implementation/comments)。

与本项目直接相关的是 YouTube 的 derived metrics 条件：官方列举 NLP 评论情感分析为
可接受例子，但须接受相应 Developer Policies amendment，并符合 analytics 用途。
评论正文仍受 30 天刷新/删除要求约束，不能把 API 可读理解成可以永久保留所有正文。
申请/接受条件及保留策略确认后再把它接入长期分析链路。
来源：[YouTube 衍生指标政策](https://developers.google.com/youtube/terms/derived-metrics-policy)。

**Reddit：主题契合强，访问条件不适合作为当前几天验收的依赖。**
r/australia、r/AusFinance、r/AustralianEV 等社区可以作为候选讨论池。例如公开可见的
[油价讨论](https://www.reddit.com/r/AusFinance/comments/1wm504d/petrol_prices_have_gone_through_the_roof_again/)
只说明话题存在，不说明本项目已获 API 权限或完整评论数据。官方要求申请访问并用 OAuth，
本次无批准凭证，因此没有通过匿名 `.json` 或网页爬取替代。
来源：[Reddit Data API Wiki](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki)。

**X：有官方搜索 API，但当前为付费用量路线。** 本次没有付费访问，未采样，不能给出
相关数据量排名；对短期、低成本验收，优先级低于已经测通的接口。
来源：[X API pricing](https://docs.x.com/x-api/getting-started/pricing)。

Mastodon 的公开 tag timeline 也受实例设置影响：关闭 public preview 时需要 token。
它只覆盖该实例可见的联邦数据，并非全网档案。Bluesky 当前实测的是 legacy v1；
迁移新版 Jetstream 时须另测事件格式、游标和删除事件。
来源：[Mastodon timeline](https://docs.joinmastodon.org/methods/timelines/)、
[Bluesky Jetstream](https://bsky.network/docs/jetstream/)。

## 针对这个项目的实施顺序

1. **统一问题与时间窗。** 分开定义“澳洲交通/油价讨论”和“国际事件相关讨论”；明确原报告
   历史窗口与新验收窗口。实时流不能恢复过去几个月的缺失档案。原始课程导出仍是历史恢复
   的首选输入（若能取得并允许使用）。
2. **Mastodon 做分页、跨实例去重和上下文扩展。** 三个已测通实例开始；按 tag 与日期预算
   翻页，记录游标、返回条数、时间覆盖和截断。以 canonical URI 作为跨实例身份；不要只用
   instance-local ID。获取种子帖回复上下文，分清直接主题命中与从父帖继承，防止跑题扩散。
3. **Bluesky 做有界连续采集。** 用已有 legacy adapter 的 cursor、重放去重和删除处理，
   记录总事件→语言候选→主题候选→地域证据→成功入库各层数量。查询 403 需解决正式访问
   条件，不轮换域名或无限重试。新采样结果表明应先评估筛选召回率。
4. **YouTube 做小规模可行性门槛。** 完成 key/用途条件后，先选 20–50 个明确相关的视频，
   统计可访问评论、时间分布、重复率和人工相关率，再决定投入。对评论应用视频上下文标签，
   但不能据此把所有评论判断为支持/反对交通政策，更不能推断评论者住址。
5. **先验证质量，再决定规模目标。** 每个来源抽样人工标注主题/地域/跑题/引用/回复依赖，
   优先得到至少 200 条分层审核样本。记录相关率及不确定性、唯一线程、作者集中度和日覆盖；
   没有独立人工标签，不把规则命中率当 accuracy。数量目标在试采后确定，当前不保证十万条
   真实澳洲相关讨论。

简历的强项应是“多来源 API 接入、断点与幂等、跨实例去重、线程上下文、数据质量漏斗、
可恢复队列与测得的处理能力”。真实样本用于分析；明确标记的 synthetic 回放用于压力测试。
这两类证据分开记录，比把无关帖子累计成大数字更能支持面试中的追问。

以上是扩量设计与访问调查；除已有 bounded probe 外，本次没有启动后台采集或新增付费 API。
