# 原搜索词复用与样本审核

日期：2026-10-01。目标是先复用原有工作、增加可解释的候选量，不设置人为的高命中率门槛。

## 原实现可复用的部分

词库统一在 `backend/parallel_harvester/topics.py`，serial_harvester 已通过兼容导入复用。
保留全部 **58 条 SEARCH_QUERIES、71 个 TOPIC_KEYWORDS 条目、4 类主题**：

| 类别 | 关键词数 | 原查询示例 | 处理方式 |
|---|---:|---|---|
| 公共交通 | 21 | public transport Melbourne / myki Melbourne / replacement bus Melbourne | 直接保留 |
| 油价 | 19 | petrol prices Australia / Hormuz oil price Australia | 直接保留 |
| EV | 17 | electric vehicles Australia / EV charging Australia | 直接保留，宽泛 battery/charging 视为候选信号 |
| 燃油车与驾驶成本 | 14 | petrol car Australia / car fuel cost Australia | 直接保留；drive/driver 单独出现可能是噪声 |

58 条查询中也包括跨主题组合。词条不是每条都独立计量：例如 fuel price 与 fuel prices
会重叠，多个查询也可能返回同一个帖子；最终数量按帖子身份去重。

搜索接口和主题分类不是同一个环节。`generate_queries()` 产生搜索词，`match_topics()`
负责拿到正文后标注候选主题；本轮没有用另一套“更严格规则”替换已有词库。

## 平台适配及小修复

- **原全文查询保留。** 已有 Mastodon full-text search 需要合适的用户 token 与实例搜索
  配置；本机本轮未配置 token。Bluesky 公共历史搜索此前为 403。本轮不把拒绝访问视为
  搜索结果零条，也不轮换域名重试被拒绝的全文请求。
- **匿名可用路径是 public tag timeline。** 从原关键词选出 public transport、myki、tram、
  petrol、fuel prices、electric vehicles、ev、driving，转换成 8 个 hashtag，访问先前测通的
  三个实例。它复用主题词，但不是完整执行原 58 条含 Australia/Melbourne 条件的全文查询。
- **标签补召回。** `match_topics(text, hashtags)` 从 API 的结构化 tags 中识别连写形式，
  如 ElectricVehicles → 原关键词 electric vehicles。普通正文不做任意子串匹配，不把
  clever/never 当成 EV，也不凭空增加一种新主题。
- **地域作证据分组。** 普通 act、单独 Victoria/Perth/Opal 等存在歧义，单独出现不确定为
  澳洲。保留 `explicit_text_hint / ambiguous_text_hint / unknown`，本轮三类都可入候选库。
  即使存在明确地名，也只表示文本提及该地区，不证明作者住在那里。旧 numeric confidence
  字段保持兼容，仍是启发式数值，不是校准后的概率。
- **去重按 canonical ActivityPub URI。** 相同帖子经不同 hashtag/实例返回只计一次；原
  instance-local ID 保存在 source_record_id，便于以后扩展上下文。旧采集器的标识规则暂不
  做全量迁移；此次 canonical ID 适配仅用于新的 pilot 数据集，未重写旧库。

官方依据：[Mastodon 搜索](https://docs.joinmastodon.org/methods/search/)、
[公开 hashtag timeline](https://docs.joinmastodon.org/methods/timelines/)。

## 审核口径

本轮由 **Codex/AI 阅读样本文本作初步审核**，不是人工双人标注，也不是模型 accuracy 的
金标准。审核类别：related（直接相关）、adjacent（弱相关/上下文相关）、unrelated
（明显无关）、uncertain（当前正文不足以判断）。地域审核只看文本及讨论背景，不推断
个人居住地或敏感属性。命中关键词但主要谈手机电池、USB drive 或电台 station 的内容
可以标作噪声；不因为缺少 Australia 或没有明确赞成/反对态度，就把交通帖子删掉。

在当前 90 天窗口内，按“标签新增候选、原规则候选且有地域词、其他原规则候选、无主题
命中”分层，各层按 doc_id 哈希顺序取最多 15 条。保留缺少明确地域证据的样本。
这是诊断性抽样，不是平台分布的随机代表样本；不把分层样本的比例外推成总体精确率。

## 复现及边界

~~~powershell
# 新目录避免覆盖原始抽样；每实例每标签最多 2 页，每页最多 40 条。
uv run --no-sync python -m scripts.audit_search_reuse collect --directory data/mastodon_query_reuse_20261001
# 审核 data/<dataset>/review-sample.json，标注存入相邻 review-labels.json。
# 标签文件绑定 sample_sha256；校验样本身份、完整性并汇总：
uv run --no-sync python -m scripts.audit_search_reuse review --directory data/mastodon_query_reuse_20261001
# 如需重采，使用不同目录名；不能把不同批次标签混用。
uv run --no-sync python -m scripts.audit_search_reuse ingest --directory data/mastodon_query_reuse_20261001 --kubeconfig '..\k8s-1-34-10-do-5-sgp1-1790830229700-kubeconfig.yaml'
~~~

网络请求上限为三实例 × 八标签 × 两页，共 48 次；短页、重复游标、拒绝访问或限流会
提前停止相应请求。语种不硬过滤，后续 English RoBERTa 的输入要另做语言路由。
返回→身份去重→日期/正文校验→主题候选→地域证据分组→bulk 写入/逐条读回，分别计数。
地域分组之和等于候选数，不是额外删帖门槛。成功入库也不等于已完成情感分析。

完整正文和审核工作表位于被 Git 忽略的 data/；仓库只保存脱离正文的统计和审核结论。
原报告研究窗口为 2026-02-14 至 2026-05-04；本次当前窗口试采不冒充历史数据恢复。
原始词库有 oil_vehicle 第四类，而现有 API 聚合仅有 ev/public_transport/fuel_price 三类；
第四类保留在 raw 数据中，本轮不偷偷合并或声称已经在图表中完整展示。

## 本轮结果

当前窗口：2026-07-03 08:45:56 UTC 至 2026-10-01 08:45:56 UTC。
实际 47 次请求：42 次 HTTP 200、3 次 503、2 次 ReadTimeout。保留错误记录，未把失败
响应计成零相关帖子，也未为补满页数无限重试。

| 环节 | 数量 | 说明 |
|---|---:|---|
| 返回记录 | 1,680 | 含跨标签/跨实例重复 |
| 去重后帖子 | 983 | 去掉 697 次重复；无缺失身份 |
| 日期/正文校验后 | 809 | 174 条在本次窗口外；无空正文/日期错误 |
| 原正文关键词规则命中 | 689 | 使用原有 71 个词条 |
| 加入结构化标签适配后的候选 | 809 | 补回 120 条；不是 809 条已验证高质量讨论 |
| 地域证据 | 明确文本线索 60 / 歧义线索 2 / 未知 747 | 三类全保留，地域过滤删除 0 |
| 云端入库及读回 | 809 / 809 | raw 索引 created=809，failed=0；尚无本轮模型处理 |

主题计数允许重叠：公共交通 302、油价 284、驾驶/燃油车 147、EV 180，不能相加当成
独立帖子总量。API 返回的语言标记为英语 638、未知 85、其他语种 86；这些也是元数据，
审核中发现“en”标记的正文仍可能包含转写外语。

**AI 样本审核：45 条全部读完，直接相关 22、弱相关 16、无法确定 5、明显无关 2。**
无主题命中层本次为空；其余三层各 15 条。因为是按规则分层的诊断样本，不能将
22/45 或 (22+16)/45 宣称为采集器准确率。新增标签候选里既补回了有效 EV/油耗主题，
也包含大量边缘标签内容；保留该层来源，后续可分别分析。

有价值的直接样本包括 Myki 收费/支付、墨尔本公交可达性、EV 长途使用、燃油税和汽车
购买价格。弱相关样本包括电车赛事、公交摄影和道路安全；并非都需要删掉，但不适合
直接作为“油价改变了出行选择”的证据。两个明显噪声分别是带油价标签的泛社会感想、
带 driving 标签的城市摄影。新闻聚合帖子另有重复标题传播，canonical URI 去重只去掉
同一帖子，不会自动把不同帖子对同一新闻的传播合并为一个独立观点。

地域方面，S042 正文讨论马来西亚补贴，但聚合标签带 Australia；S031/S036 是国际赛事
提到澳洲参赛城市；S034 仅靠 Victoria 会模糊，但 Yarra/Metro 运营背景足以增强文本情境
判断。因此地域应作为可解释的证据维度，不宜直接当作硬过滤或作者居住地标签。

809 条全部保存在云端 `v2_social_discussion_posts_raw`，`source_dataset` 为
`mastodon_query_reuse_20261001`，`dataset_kind=live`。这是保留噪声、待进一步处理的 raw
候选层，**不把已审核的 45 条冒充全量审核，也不把 809 条冒充处理完成或真实观点数**。
审核标签单独保存并绑定样本 SHA-256，避免为了提高命中率悄悄删改原始数据。

本轮采集脚本在本机运行并经 TLS 隧道写入云端 ES；未部署常驻采集器或定时任务。
统计：[查询复用证据](evidence/search-reuse-2026-10-01.json)；
不含正文的审核记录：[AI 样本标签](evidence/search-reuse-review-2026-10-01.json)。

验证：146 项单元/真实本地服务集成测试通过，1 项神经模型测试未运行（本轮没有修改模型）；
32 项部署声明离线检查及源码检查通过。云端 bulk 逐条结果、逐 ID 读回和 Fission quality
接口另行实测。上述测试均不把 AI 审核标签当作模型准确率金标准。
