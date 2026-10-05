# DigitalOcean 云端恢复与验收记录

日期：2026-10-01，Australia/Sydney。

## 本次工作位置

- 主工作区：项目仓库根目录（下述路径以仓库根目录为基准）
- 连接入口：上级目录的 `Connect-Cloud.ps1`
- kubeconfig：上级目录中的 `k8s-1-34-10-do-5-sgp1-1790830229700-kubeconfig.yaml`
- 迁移前的工作区保留为本地备份，后续修改在新工作区进行。

Git 历史和原有 artifacts/data 已保留。Windows 阻止部分目录的直接移动，因此采用
复制、Git 完整性校验和补齐旧副本的方式迁移。新路径的 Python 虚拟环境按 uv.lock
重新建立，避免沿用指向旧目录的命令入口。Docker 本地演示使用原有命名卷，已从
新路径重新启动；这不是云端业务部署。

在新的 PowerShell 窗口中执行：

~~~powershell
& 'C:\Users\RENFEI\Desktop\cloud cluster\Connect-Cloud.ps1'
~~~

脚本设置当前终端的 KUBECONFIG 和仓库目录，并检查节点；不改写全局默认 kubeconfig。
后续自动化命令同样显式指定此 kubeconfig，避免误操作其他集群。
凭据文件位于 Git 仓库外；仓库另加 kubeconfig 文件忽略规则。

迁移后已实际执行连接脚本，并在新虚拟环境下通过源码凭据/TLS 检查、32 项声明式
资源离线校验，以及 Notebook 的 7 个代码单元执行。Notebook 调用的是本地演示 API，
输出位于新工作区的 artifacts 目录；这些结果不作为云端业务验收。

## 第一步：初始预检查快照

| 项目 | 结果 |
|---|---|
| 云平台 / 地区 | DigitalOcean Kubernetes / SGP1 |
| 集群名称 | k8s-1-34-10-do-5-sgp1-1790830229700 |
| Kubernetes | v1.34.10；控制台版本 1.34.10-do.5 |
| 客户端 | kubectl v1.34.1，可成功连接 |
| 节点 | 2 个 amd64 节点，均 Ready |
| 单节点容量 | 4 vCPU、约 8 GiB 物理内存；Kubernetes 可分配内存约 6.26 GiB |
| 系统组件 | 本次检查的 kube-system Pods 均 Running，零重启 |
| 存储 | do-block-storage 为默认 StorageClass，CSI 驱动存在；卷创建和读写尚未验收 |
| 权限 | 允许在 default 命名空间创建 Deployment |
| Metrics API | 尚未注册 metrics.k8s.io；CPU 自动扩容前需安装并验证 metrics-server |
| 业务资源 | 尚无本项目云端业务 Deployment、PVC/PV；Fission、ES 和队列待部署 |

这些结果来自实际 Kubernetes API 调用，摘要见
[cloud-preflight-2026-10-01.json](evidence/cloud-preflight-2026-10-01.json)。
集群连通性成功不代表业务已经上线、数据已经迁入或存储已经通过故障恢复测试。

控制台截图显示集群当前位于 first-project，后续可整理到 transport-analytics。
用户已确认创建时取消了 Add high availability，未启用额外付费的 HA 控制平面。
此项为用户确认，不能从 Kubernetes 工作节点列表验证；节点、存储等费用仍按云平台账单计算。

## 第二步：Elasticsearch 与持久化存储

已部署 ECK 3.5.0 和 Elasticsearch 8.19.22，并建立 20 GiB Retain 持久卷。
当前为单数据节点、零索引副本的短期验收配置。部署、权限、重现方式与计费清理见
[Elasticsearch 运维记录](CLOUD_ELASTICSEARCH.zh-CN.md)。初始预检查 JSON 保留原状，
后续数据库验收证据单独记录，避免将不同阶段的状态混在一起。

完整验收已通过：TLS 与主机名校验、401/403 权限边界、bulk upsert，以及 Pod 重建后
同一 PVC/PV 上的记录读回。最终为 Green / Ready，12 个业务索引已建立，历史数据
尚未导入。实际结果见 [云端 ES 验收证据](evidence/cloud-elasticsearch-2026-10-01.json)。
本次还通过 127 项单元测试（10 项服务/模型相关测试未在该命令中运行）、32 项资源
离线校验及源码检查；云端 ES 的实测独立记录，不沿用本地性能数字。

## 第三步：Fission 与 API

已安装 Fission 1.23.0 和 metrics-server 0.9.0，以 Fission specs 发布 API。
7 个 Fission 控制组件 Pod 与 API Pod 均 Ready；HPA 能读取 CPU 指标，当前 1 副本。
新增 1 GiB Retain PVC 存放函数部署包；没有新增公网 LoadBalancer。

修复了 ES 服务域名与证书 SAN 不匹配的问题，保留认证和 TLS 校验。
15 项 API 路由检查与 8 项真实 ES/API 临时数据检查通过；两条 synthetic 测试记录已清理。
本轮 130 项单元测试通过，10 项服务/模型测试未包含在该单元测试命令中；云端集成检查
独立执行。重现命令、访问入口和具体边界见 [Fission/API 记录](CLOUD_FISSION_API.zh-CN.md)。

当前业务索引尚无历史数据。采集器、模型 worker、Redis/KEDA 待恢复；CPU HPA 可用
不作为实际负载下扩缩容成功的证据。社交 API 可行性与实采统计见
[来源调查](SOCIAL_DATA_SOURCES.zh-CN.md)。

## 后续验收顺序

补充：第三步之后执行了有上限的本机 API 采集 pilot，将 809 条真实候选原始记录写入
云 ES，并通过云 Fission `/api/v1/quality` 确认 raw=809、processed=0。45 条样本已有
明确标识为 AI 初审的标签。它复用原词库，地域分组不硬过滤；尚未部署常驻采集器或
执行云端模型任务，也不等于原报告历史窗口已恢复。见 [查询复用与审核](SEARCH_REUSE_AUDIT.zh-CN.md)。

继续以全球英语入口试采后，594 条真正新增、326 条与首批重叠，云端 raw 总量为
1,403。再次执行同批入库新增为 0；逐条读回确认两批首次正文与来源未覆盖。Fission
quality 返回 raw=1,403、processed=0；30 条新增分层样本完成 AI 初审。全球查询配置、
语言与地域标签、各入口的新增贡献见 [全球扩展验收](GLOBAL_EXPANSION.zh-CN.md)。

最新阶段已完成云端 RoBERTa Job：1,403 条中处理 1,047 条、过滤 356 条，全部决策可追溯，
写入失败 0。Fission quality 现为 raw=1,403、processed=1,047；另有 26 条实验性 LLM
结构化标注可独立查询。160 项测试、云端全量读回和分页检查通过；具体错误样本、成本边界
与复现方式见 [模型处理与 LLM 记录](CLOUD_MODEL_PROCESSING.zh-CN.md)。前面的零处理量是历史阶段状态。

1. HA 已确认关闭，部署工具已准备；核对节点与持久存储费用。
2. Elasticsearch、认证权限与持久卷重启验收已完成。
3. Fission、Metrics API 与查询 API 已恢复；应用包通过内部存储发布，无需公开镜像仓库。
4. 有界采集与单个云端模型 Job 闭环已完成；持续调度及云端 LLM worker 尚未部署。
5. 验证 Redis 重放与 KEDA 扩缩容；按实际可分配内存限制并发，而非只看节点标称内存。
6. 导出数据、配置及验收证据；短期验收结束后逐项核对并释放计费资源。

第一步目录迁移与预检查只读取云端状态；第二、三步创建了 ECK、ES、Fission 和计费磁盘。
