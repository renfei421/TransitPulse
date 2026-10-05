# 工程改进交付报告

> Historical implementation-stage document. For the completed experiment and actually executed architecture, see [TransitPulse case study](PROJECT_CASE_STUDY.zh-CN.md) and [archive/replay runbook](ARCHIVE_AND_REPLAY.zh-CN.md). Earlier pending items and counts are not current status.


交付日期：2026-10-01（Australia/Sydney）。
项目：TransitPulse 多源交通舆情数据与推理平台，版本 0.2。
基线：main 的 4dcd20b；工作分支：codex/analytics-platform-hardening。

## 交付结论

这次已将改进落实到可运行代码、测试、容器、部署规格和文档中。主要价值是
**可靠的数据采集与处理、可解释的分析口径、可恢复的推理链路和可复现验证**。
它适合展示数据工程、AI 平台工程和分析工程能力；目前不是一个 LLM Agent，
也没有完成分布式云端容量认证。

本文件描述该交付阶段的实现与实测，历史数据规模、实验结论和云端部署状态
与本次验证结果分别记录。
完成的 Notebook 和 Brent 采集器参考 origin/haoming 的 2ab788f 整合；
未整分支覆盖当前代码，历史凭据和旧 Notebook 输出未带入维护版本。

## 可以直接查看

- 一键启动：在仓库根目录执行 .\scripts\quickstart.ps1。
- 本机 API：[资源目录](http://127.0.0.1:9090/api/v1)、
  [OpenAPI](http://127.0.0.1:9090/api/v1/openapi.json)。
- [交互分析 HTML](../artifacts/analysis.html)。
- [已执行 Notebook](../artifacts/hormuz_analysis.executed.ipynb)。
- [工程审查与实现对照](ENGINEERING_REVIEW.md)。
- [数据扩量方案与命令](DATA_EXPANSION.zh-CN.md)。
- [部署、迁移和恢复手册](OPERATIONS.md)。

本机演示服务已启动。重启后可再次运行 quickstart。演示数据为独立
demo_v2_ 命名空间中的 720 条合成社交帖、60 条合成新闻及 60 个日期的合成油价/
新闻关注度记录，模型明确标记为 VADER。默认 API 排除合成数据；演示查询显式
传 dataset_kind=synthetic，时间范围为 2026-08-01 至 2026-09-29。

## 关键改动及其意义

| 环节 | 已实现 | 解决的问题 |
|---|---|---|
| 配置与安全 | ES 客户端、Secret/env 配置、默认 TLS 校验；迁移专用身份 | 移除代码中的连接凭据，避免日常应用拥有建表权限 |
| 数据写入 | 稳定 ID、逐条 bulk 结果核对、created/updated/noop/failed 分开统计 | 重试不重复计数，HTTP 200 不再掩盖部分写入失败 |
| 扩量入口 | NDJSON/gzip 断点导入、历史搜索分页、Jetstream v1 增量、GDELT 自适应切窗 | 解除一次搜索/少量种子限制，保留预算、采集缺口和来源 |
| 增量处理 | created_at 与 fetched_at 分离、48 小时重叠窗口、有界批处理 | 处理迟到记录，避免每次把全量历史加载到内存 |
| 情绪语义 | 有符号 polarity 与 confidence 分离；反对回复不机械翻转情绪；主题继承可追溯 | 修正“高置信负面被当成高正面”等会改变分析结论的问题 |
| 模型执行 | 固定 RoBERTa revision、批量推理、离线权重镜像；VADER 显式基线 | 不再因加载失败悄悄混用模型，便于复现及比较成本 |
| 推理可靠性 | 可选 Redis Streams、ES 写入后 ACK、pending 接管、三次失败入死信、显式重放 | 处理宕机、重试与突发积压；交付语义为至少一次 |
| HTTP 服务 | /api/v1 资源式路由、分页、输入验证、模型/样本筛选、质量诊断、兼容适配 | 降低接口使用成本，避免错误筛选和不同数据口径混用 |
| 统计与图表 | 标签净情绪、真实日历滞后、最低 14 对样本、缺失不插值、滞后搜索校正 | 避免不等间隔数据错配；保留探索性分析的解释边界 |
| 部署与运维 | 集中 mapping、显式迁移、Fission specs、GitLab CI、版本锁、JSON 日志 | 消除 ConfigMap 执行脚本、不可复现依赖和隐式建索引 |
| 验证 | 单元、真实服务集成、HTTP 端到端、真实模型、Fission 容器加载 | 覆盖从原始数据到分析接口以及失败恢复路径 |

保留 CronJob 做按日期对齐的油价、新闻和相关分析；需要扩展昂贵社交推理时
启用 Redis/KEDA 模式。队列模式移除每日社交处理定时触发，避免两种调度同时工作。
KEDA 规格以未投递与 pending 积压驱动 1–4 个 worker，尚未在真实云集群验收。

## 本次实际验证结果

| 验证 | 结果 | 条件与边界 |
|---|---|---|
| 自动化测试 | **132 passed，0 failed，0 skipped** | 启用本机 ES、Redis 及真实 RoBERTa；34.35 秒 |
| 后端总体行覆盖率 | **50.98%** | 分母包含保留的旧离线绘图/解析脚本；不是 90% 全仓库覆盖 |
| 核心模块覆盖 | sentiment 94%，API router 86%，查询 93%，新闻推理 93%，相关计算 86% | 精确比例保存在 verification.json；覆盖率不等于没有缺陷 |
| Fission 运行时 | specialization、meta、health 与分析 HTTP 均成功 | 真正的 Fission Python 镜像，本机 Docker；非 Kubernetes 集群部署 |
| 规格检查 | 32 个声明式资源通过离线检查 | Fission 使用官方 v1.23.0 CRD schema；K8s/KEDA 仅本地结构/约束检查 |
| Notebook | 7 个代码单元执行成功 | 生成可交互、只含聚合图表的 HTML；演示数据为合成 |
| 新真实数据 | Jetstream 1,485 个事件中保留 12 条，12 条完成 RoBERTa 推理 | 约 48 秒、1.23 MB，全球英语交通关键词样本，非澳洲代表性调查 |
| ES 容量试验 | **100,000 条合成记录**，约 **7,719 条/秒** 写入 | 本机单节点，batch=500、1 shard、0 replica；不含神经模型推理 |
| 查询延迟 | p50 6.88 ms，**p95 9.31 ms**，p99 10.34 ms | 4 并发、100 次已预热重复聚合；不是完整 API 或生产 p95 |
| Bulk 对照 | 同样 200 条，单条写入 1.157 s，批量 0.069 s | 单次小样本对照，约 16.7 倍；不外推为所有流量收益 |
| CPU 推理对照 | batch 1：27.4 条/s；batch 16：**60.1 条/s** | 96 条不同短合成文本、2 CPU 线程、清缓存、不计模型加载；单次约 **2.19 倍** |

原始结果与限制见 [evidence/README.md](evidence/README.md)。压测与模型对照是
不同实验，不能把 7,719 条/秒写成端到端 RoBERTa 处理吞吐。
Python traced peak 1.74 MiB 也不是进程 RSS 或 Elasticsearch 总内存。

## 尚需外部条件的事项

1. **云集群连接**：当前配置的远程 Kubernetes API 超时。没有进行新云端发布，
   没有删除旧 FastAPI pod，也没有声称 KEDA 扩容已实测。服务恢复后按 Operations
   完成原生 Fission 校验、迁移、发布、故障演练及扩容验收。
2. **真实历史数据**：没有找到可用的历史原始导出；导入器已测试，真实导入仍需文件。
   Bluesky 公共历史搜索实测 HTTP 403；应先解决访问条件，不能用反复重试伪装扩量。
   当前可工作的 Jetstream 只能增加当前样本，不能补造历史研究窗口的数据。
3. **外部密钥**：EIA 等数据源需有效 Secret。本次未读取所提供的旧凭据压缩包、
   未将其部署或提交到仓库。已从当前源码移除硬编码；旧 Git 历史未重写，
   其中仍有效的旧凭据应轮换。
4. **人工标签与模型效果**：已提供 thread 分组切分、macro F1、混淆矩阵、
   root/reply 切片及线程 bootstrap 工具；没有人工金标，因此没有新准确率、
   “RoBERTa 胜过 VADER”或 fine-tuning 收益。
5. **持续运营**：尚无长期运行的采集成功率、真实日增长曲线、HA Redis 或多节点 SLA。
   CI/CD 定义已入库，本次未在远端 GitLab runner 上执行。

这些是外部部署、数据访问或人工评测的后续验收项；已完成的本地实现与测试可独立复现。

## 后续执行顺序

1. 用有使用权限的真实历史导出回填到新的 v2_ 前缀，并校验源格式与使用范围。
2. 并行积累当前 Jetstream 样本，按平台、周、主题、独立线程检查分布。
3. 先达到 1–5 万条去重真实帖、8–12 周覆盖，再讨论 10 万真实记录目标。
   这是目标，尚未达到；不能只靠放宽关键词制造无关样本。
4. 人工复核 300–500 条，比较 VADER、RoBERTa 和上下文规则。
5. 连接云环境后做积压、worker 宕机、限流、重放与资源成本测试，
   用实测确定是否需要更多 worker、分片或 fine-tuning。

## 简历写法

推荐项目名：**多源交通舆情数据与推理平台**。
可以按岗位从以下三条中选择两到三条，不把合成数据改写为真实业务数据：

- 重构多源采集与情绪分析链路，实现流式断点导入、稳定 ID 去重、来源与模型版本追踪，
  提供 Fission 资源式 API 与 Redis Streams 可恢复推理，补充 132 项通过的自动化测试。
- 构建可复现的 Elasticsearch 容量试验，在本机单节点 10 万条合成数据上实现约
  7.7k 条/秒批量写入、4 并发预热聚合查询 p95 9.3 ms，并验证失败重放的幂等性。
- 将固定版本 RoBERTa 推理批量化，在 96 条短文本、2 CPU 线程的对照中将吞吐
  从 27.4 提升至 60.1 条/秒；修正情绪置信度/方向混用、回复主题继承和日历滞后计算。

AI infra 岗应重点讲模型启动、batch、队列交付语义、重试、可观测性、镜像和测试。
数分岗应重点讲数据口径、采样偏差、处理覆盖、标签质量、时间对齐和结论限制。
“已证明冲突导致偏好变化”“生产百万数据”“云端自动扩容已上线”“准确率提升 X%”
均不属于本次有证据支持的表述。

面试时结合代码与演示解释架构设计、数据口径和实测结果，并说明尚未验证的能力边界。
