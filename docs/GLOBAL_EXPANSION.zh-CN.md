# 全球查询试采与增量验收

2026-10-01 实测：保留第一批 809 条记录，新增 594 条，云端 raw 总量为 **1,403**。
第二批重放新增为 0，两个批次的首次来源和正文逐条读回一致。processed 仍为 0。
这一步完成的是扩大候选数据入口、样本审核和可核对的增量入库。

## 范围与配置

研究范围调整为跨地区的公共交通、燃油成本、EV 和驾驶讨论，先为英语分析准备数据。
地域线索作为分析维度，不再作为采集准入条件。原研究的澳洲问题和历史窗口仍可独立复现。

`backend/parallel_harvester/topics.py` 提供三个显式 profile：

| Profile | 用途 |
|---|---|
| `legacy_au` | 默认值；原 58 条搜索查询与 71 个关键词不变 |
| `global_en` | 移除查询末尾的澳洲地域约束，合并重复项，补充英语表达；62 条查询、87 个关键词 |
| `regional_supplement` | 保留 Myki、PTV、VLine 等本地产品和政策查询，按需补充 |

`global_en` 中加入 public transit、mass transit、gasoline prices、charging costs 等表达。
完整搜索词配置与实际执行的公开标签 API 有区别：**本轮执行的是 8 个 hashtag 时间线入口，
没有获得认证全文搜索权限，也没有执行全部 62 条全文查询。** 现有认证 harvester 的默认
仍为 legacy 配置；本轮通过 pilot 的 `--profile global_en` 明确选择，不自动启动常驻采集器。

语言字段为平台元数据：en/en-* 标记为 `english_candidate`，空值标记为
`needs_language_review`，其他语种标记为 `other_language`。这是待处理路由标签，尚未
接入自动运行的模型 worker；不声称已经验证语言。多语言原文仍留在 raw 层。

`country_text_hints` 仅记录有限词表命中的国家文本线索，不是地理编码或作者所在地。
它会漏掉城市、复合标签和 US 等歧义缩写，也会受新闻内容、链接和附带标签影响。
原 `region_evidence` 只检查澳洲词表，不能把 unknown 理解成“全球没有地域线索”。
新增全球批次的原澳洲研究事件标签清空，避免自动套用维州政策事件。

## 实际采集漏斗

时间范围：2026-07-03 09:10:26 UTC 至 2026-10-01 09:10:26 UTC。
使用 mastodon.social、mastodon.au、aus.social 的公开标签时间线，每标签每站最多 2 页、
每页最多 40 条，预算 48 次请求 / 1,920 条返回。实际 45 次请求：38 次 HTTP 200、
6 次 HTTP 503、1 次 ReadTimeout。未绕过访问限制，也未把失败请求当作空结果的成功采集。

| 层级 | 数量 | 解释 |
|---|---:|---|
| API 返回 | 1,520 | 同一帖子可能在不同站点、标签、页面重复出现 |
| canonical URI 去重后 | 1,083 | 移除 437 次重复，缺失身份 0 |
| 时间窗口内有效正文 | 920 | 窗口外 163；格式错误或空正文 0 |
| 原 71 词纯正文规则命中 | 681 | 用作原规则参照 |
| 全球词表与结构化标签候选 | 920 | 比纯正文规则补回 239 条，包含国际表达和标签适配 |
| 澳洲地域词表线索 | 30 明确 / 9 歧义 / 881 未知 | 三类全保留，地域过滤删除 0 |
| 有国家文本线索 | 327 | 可同时提到多个国家；与上一行不是互斥分类 |
| 云端本次真正新增 | **594** | 第一批未出现的 canonical URI |
| 云端已存在 | **326** | 保留首次来源；不重复计入数据增长 |
| 入库失败 | **0** | 逐条检查 bulk 结果，HTTP 200 本身不等于成功 |

920 条候选的平台语言元数据：英语 873、其他语言 14、未知 33。
新增 594 条中分别为 565、11、18；合并两批的 1,403 条中分别为 **1,203、97、103**。
这些是未独立验证的路由数，不是已完成模型分析的数量。

公共交通、油价、EV、驾驶主题分别命中 160、454、272、165 条。主题允许重叠，不能相加。
按空白归一化和大小写折叠比较正文，920 个帖子中只有 881 个不同文本，存在 39 次
完全相同正文的额外出现。此处保留 URL，没有做语义去重；不同文本也不等于独立观点。

## 哪些入口值得继续扩大

| 标签入口 | 本轮窗口内候选 | 相对第一批新增 |
|---|---:|---:|
| publictransit | 113 | 102 |
| masstransit | 21 | 21 |
| gasprices | 129 | 116 |
| fuelprices | 143 | 0 |
| oilprices | 182 | 179 |
| evcharging | 91 | 89 |
| electriccars | 159 | 102 |
| driving | 112 | 0 |

同一帖子可属于多个标签，表格不能直接相加得到新增总数。两次采集时间和入口不同，
这些数字是观察到的增量贡献，不能用于估计“去掉澳洲限制”这一单独因素的因果收益。
两批采集本来就都没有地域硬过滤，本轮的主要变化是英语检索表达和标签入口。

下一轮优先扩大 publictransit、gasprices、evcharging、electriccars 的时间覆盖。
oilprices 新增多，但能源新闻聚合与词义歧义较多，应在分析层区分新闻背景和个人体验。
短时间重复抓取 fuelprices、driving 的相同浅页不会带来有效增长；应采用有预算的时间
分页、增量游标或后续时段采集。Mastodon 单个实例只提供其可见内容，三个实例并不是
整个网络的总体抽样框，两个澳洲实例也仍会影响可见数据构成。

## AI 样本审核

按规则分层、doc_id 哈希排序，每层取 10 条，共 **30 条逐条阅读**：直接相关 18、
相邻话题 8、信息不足 2、无关 2。标签明确标注 `reviewer_type=ai`，绑定样本 SHA-256。
没有打开链接文章或图片，不核实帖子中新闻陈述的真伪，不推断作者居住地或个人属性。

审核层仍沿用历史字段名：`hashtag_added_candidate` 在全球 profile 下包含“新增词语或
结构化标签”补回的候选；另外两层为澳洲地域词表命中和其余原词表命中。
这是诊断用的分层样本，不能把 18/30 宣称为总体准确率，也不能把 30 条审核等同于全量审核。

具体发现：

- 新增入口找到玻利维亚公交应用、西雅图交通信息、充电设施和燃油家庭预算内容。
- `oilprices` 会命中食品与橄榄油新闻（S010）；泛社会感想也可能仅附油价标签（S002）。
- 图片梗与纯摄影标签在正文中信息不足；公交摄影和道路安全通常属于相邻话题。
- 马来西亚补贴新闻带有 Australia 分发标签（S030），长篇国际新闻出现 Australian
  艺术图注（S022）；两者都不能当作澳洲居民意见。普通动词 act 也不构成 ACT 地域。
- S024/S026 为高度相似的美国燃油预算文章；URI 去重不能解决跨帖子新闻转载问题。
- 有反讽电动车评论，也有新闻标题和应用推广。后续情感分析必须区分文本类型与态度对象。

已识别噪声仍保留在 raw 候选层；审核标签单独存储，没有为了提高命中率删改原文。

## 入库与验证

原始 schema 新增 3 个 keyword 字段：collection_profile、analysis_language_route、
country_text_hints。通过中央 `database.migrate` 和 migration 身份显式应用，采集/入库
函数不创建 mapping。验证读取使用应用身份，未扩大迁移账号的读取权限。

pilot 改用 Elasticsearch bulk create。只有 201/created 计为新增；只有 409 且错误类型
为 version_conflict_engine_exception 才计为已有。其他错误会失败并保留本次写入统计。
重跑同批时只跳过已有文档，保留首次来源与正文；原批次仍为 809，新批次为 594。
这也意味着已有帖子的编辑、删除和统计更新需要独立的同步策略，当前 pilot 不刷新它们。

首次执行：created=594、already_present=326、failed=0、read_back=920。
重放执行：created=0、already_present=920、failed=0、read_back=920。
首次与重放记录分别保存在 `sink_history` 和 `sink`，没有用重放结果覆盖首次证据。

`verify` 逐条读取两批并集 1,403 条，比较首次正文及 source_dataset；再调用实际云端
Fission `/api/v1/quality`。API 返回：全体 raw=1,403、原批次 raw=809、新批次 raw=594；
processed 均为 0。API 目前仍采用 Australia/Sydney 日历，核对时显式扩大日期边界覆盖
UTC 采集窗口；尚未把旧分析接口改成全球 UTC 口径。

完整非模型测试 150 项通过；随后新增采集回归用例，相关文件 11 项通过，合计覆盖
151 个不同用例。1 项神经模型测试未运行。32 项声明资源校验、ruff、凭据/TLS 检查通过。
真实本地 ES 测试验证了跨批重叠不会覆盖来源；上面的云端重放和 API 读回是独立实测。

## 本地复现

从仓库根目录执行。前提为云端 ES/Fission 已恢复、中央 mapping migration 已执行；
kubeconfig 位于仓库之外，凭据从 Kubernetes Secrets 读取，日志与命令不携带密码。

```powershell
$pilot = 'data/mastodon_global_en_20261001'
$baseline = 'data/mastodon_query_reuse_20261001'
$clusterConfig = '..\k8s-1-34-10-do-5-sgp1-1790830229700-kubeconfig.yaml'

# 已有批次的只读复核
uv run --no-sync python -m scripts.audit_search_reuse verify --directory $pilot --baseline-directory $baseline --kubeconfig $clusterConfig
uv run --no-sync python -m scripts.audit_search_reuse analyse --directory $pilot --baseline-directory $baseline

# 新一次采集必须使用新目录，避免覆盖历史快照
uv run --no-sync python -m scripts.audit_search_reuse collect --directory data/global_next_run --profile global_en --pages 2 --per-stratum 10
```

新批次 collect 后必须审阅 review-sample.json 并写入绑定其 SHA-256 的 review-labels.json，
再运行 review 和 ingest；没有审核文件时 ingest 会失败，不会暗自生成“已审核”标签。
同批重复 ingest 用于验证幂等性；只想查看数据时使用 verify 即可。

完整原文、批次观察入口和审核工作表留在 Git 忽略的 `data/` 中。提交的证据只有计数、
请求状态、审核摘要和哈希：[采集与增量](evidence/global-expansion-2026-10-01.json)、
[AI 审核](evidence/global-expansion-review-2026-10-01.json)、
[云端验证](evidence/global-expansion-cloud-2026-10-01.json)。

## 后续验收目标

1. 对英语候选作语言与文本类型检查，区分个人讨论、新闻转载和推广；记录排除原因，
   不将所有 raw 直接当成可分析样本。补充正文重复指纹，原文和来源继续保留。
2. 恢复单个模型处理任务，从明确过滤后的语料跑通 raw → processed → Fission API；
   保留模型名称、版本、处理窗口和实际覆盖数，再比较 VADER/RoBERTa 的具体错误。
3. 给全球分析显式增加 UTC/地域范围，澳洲政策事件与全球能源事件分开；在完成前不
   把跨国混合情感曲线与澳洲油价、维州政策直接作因果解释。
4. 有了真实处理负载后再验证队列重放、积压和扩缩容。1,403 条是试采规模，不能宣称
   已经证明大规模吞吐，也不能把本地 synthetic 压测数字写成云端真实采集性能。

协议参考：[Mastodon hashtag timeline](https://docs.joinmastodon.org/methods/timelines/#tag)、
[Elasticsearch 8.19 bulk create](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-bulk.html)。
