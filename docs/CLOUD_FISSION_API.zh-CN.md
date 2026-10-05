# 云端 Fission 与 API 恢复记录

## 2026-10-01 事件实验验收后的默认模型

90,576 条冻结输入的 Jev 推理已完成，云端 API 默认模型现为 `jev-1.13.0`。
该设置写入 `scripts/cloud_fission.py` 的发布配置，并经 Fission specs 部署和云端读取验证；
显式传入 `model=cardiffnlp/twitter-roberta-base-sentiment-latest` 仍查询独立旧基线。
默认值不会改变新闻/油价接口或独立 LLM annotations。社交查询响应 meta 显示实际模型。
完整数量、测试和语义限制见 [事件实验报告](EVENT_EXPERIMENT.zh-CN.md)，
[切换证据](evidence/event-api-cutover-2026-10-01.json)记录默认与显式基线的实测结果。
以下保留此前恢复阶段的操作记录，其当时数据量与默认值不代表最终状态。

2026-10-01，从项目仓库根目录完成部署与验证。

## 已验收的链路

本机 kubectl 身份认证隧道 → Fission router → transport-api Python function →
带 CA 与域名校验的 Elasticsearch → 已有 20 GiB 持久卷。

| 项目 | 实际状态 |
|---|---|
| Fission | 1.23.0；7 个控制组件 Pod Ready；命名空间 fission |
| Python API | default/transport-api；newdeploy；最小 1、最大 3 副本 |
| 路由 | `/api/v1` 及兼容 `/api`；通过 Fission HTTPTrigger |
| Metrics | metrics-server 0.9.0，Metrics API Available，CPU HPA 可读到指标 |
| 存储 | Fission package 使用独立 1 GiB PVC，do-block-storage-retain，Bound |
| 访问 | ClusterIP + 127.0.0.1 隧道；没有创建公网负载均衡器 |
| 验证 | 15 项路由检查、8 项临时数据断言、130 项单元测试通过 |

HPA 有指标、有配置不等于已完成负载扩缩容实验。当前 API 为 1 副本；未测生产吞吐、
跨节点故障恢复或服务 SLA。临时数据测试直接写入预先标注的 synthetic 记录，未执行模型。
历史业务数据仍未恢复，空查询结果是如实返回；无预计算相关性结果时返回 404。

独立证据：[接口/运行状态](evidence/cloud-fission-api-2026-10-01.json)、
[数据查询断言](evidence/cloud-api-data-2026-10-01.json)。旧的本地性能结果不作为云端性能。

## 打开服务

在 PowerShell 执行，保持窗口打开：

~~~powershell
& 'C:\Users\RENFEI\Desktop\cloud cluster\Open-Cloud-API.ps1'
~~~

随后打开：

- http://127.0.0.1:8088/api/v1/meta — 接口目录
- http://127.0.0.1:8088/api/v1/health — Elasticsearch 连通性
- http://127.0.0.1:8088/api/v1/openapi.json — API 合约
- http://127.0.0.1:8088/api/v1/social/posts — 真实观测数据，默认排除 synthetic

Ctrl+C 仅关闭本机隧道，不停止云服务、不停止云端计费。仓库内的可迁移入口为
`scripts/open_cloud_api.ps1 -Kubeconfig <file> -Port 8088`。

## 本次发现并修复的问题

原来的 `.svc.cluster.local` 地址可以被 DNS 解析，但不在 ECK 自动签发的证书 SAN 中，
导致 API health 返回 503。已将默认与部署配置统一改为证书支持的
`https://elasticsearch-es-http.elastic.svc:9200`，并从真实 API 容器验证 HTTP 200。
认证、CA 信任和主机名校验均保留。

Fission 官方 runtime 实测为 Linux/amd64、Python 3.13.13、Alpine/musl。
本地 Windows Python 3.11 的依赖目录不能直接复制进去。新增打包脚本在固定 digest 的
官方镜像内安装带 hashes 的锁定依赖，随后把依赖和源代码作为 deployment archive 发布。
无需公开镜像仓库，也不在函数启动时联网安装 pip 包。框架 Flask 由固定的基础镜像提供，
应用包通过 `cloud_entrypoint.py` 加载 vendor。

基础镜像预装 chardet 7.4.3，与应用 requests 2.32.5 的可选依赖范围不匹配，导入 requests
时会有 RequestsDependencyWarning；本次 ES/API 验收未受影响。未来发布自有 runtime 时可
使用现有 `deploy/Dockerfile.fission` 的卸载兼容方案。本次没有隐藏该告警或放宽锁文件。

API Pod 不挂载 Kubernetes ServiceAccount token，应用凭据从 Secret 注入；schema 管理
身份不注入 API。非敏感配置摘要进入运行模板，使配置变更可以触发新的运行环境。
该 API profile 只应用一个 Function、两个 HTTPTrigger、一个 Environment 和一个 Package；
没有启动采集、模型任务、Redis、KEDA 或业务定时任务。

## 可重现的安装与发布

安装工具：kubectl 1.34.1、Fission CLI 1.23.0、Helm 3.22.0、Docker、uv。
Helm 位于上级 `tools/helm.exe`，官方 Windows archive SHA-256 为
`899615865726d39f9b245e71e848c5bf4adc7ed33a8c43ede660facb48151b43`。
运行以下命令前，ES、transport-secrets、transport-es-ca 和 Retain StorageClass 应已按
[ES 运维记录](CLOUD_ELASTICSEARCH.zh-CN.md) 建立。

~~~powershell
$cfg = (Resolve-Path '..\k8s-1-34-10-do-5-sgp1-1790830229700-kubeconfig.yaml').Path
$ctx = 'do-sgp1-k8s-1-34-10-do-5-sgp1-1790830229700'
uv sync --frozen
uv run --no-sync python -m scripts.prepare_fission_install

# 以下为新集群的首次安装；现有集群已安装，不应借此升级另一版本的 CRD。
kubectl --kubeconfig $cfg --context $ctx apply -f artifacts/fission-install/metrics-server-rendered.yaml
kubectl --kubeconfig $cfg --context $ctx create -f artifacts/fission-install/fission-crds.yaml
kubectl --kubeconfig $cfg --context $ctx apply -f deploy/cloud/fission-storage.yaml
..\tools\helm.exe upgrade --install fission artifacts/fission-install/fission-all-1.23.0.tgz -n fission --kubeconfig $cfg --kube-context $ctx -f deploy/cloud/fission-values.yaml --wait --timeout 8m

# 每次应用代码更新
uv run --no-sync python -m scripts.package_cloud_api
uv run --no-sync python -m scripts.cloud_fission deploy --kubeconfig $cfg --context $ctx
uv run --no-sync python -m scripts.cloud_fission verify --kubeconfig $cfg --context $ctx
# 写入两个 UUID 命名的 synthetic 文档；finally 中仅清理这两个 ID
uv run --no-sync python -m scripts.cloud_api_data_probe --kubeconfig $cfg --context $ctx
~~~

上游 chart、8 个 CRD 与 metrics manifest 的 URL/hash 固定在
`deploy/cloud/fission-lock.json`。准备脚本拒绝 hash 不匹配的缓存。
应用 runtime 与 metrics image 也固定 digest；Fission chart 使用固定版本 tag，实拉取的
image ID 保存在验收 JSON。镜像 tag 不是不可变的供应链保证。

`artifacts/cloud-api-specs` 为自动生成的 API 子集，保留原 Fission DeploymentConfig UID，
后续可沿同一部署标识恢复其余组件；本次没有使用删除其他资源的参数。
`artifacts/fission-install/rendered.yaml` 可能包含 Helm 生成的内部认证 Secret/私钥，
与所有 artifacts 一起被 Git 忽略，不应发布或作为普通日志分享。

## 后续与费用

下一步是恢复采集及一个模型 worker，完成真实数据入库→推理→API 的闭环，再做队列恢复
与扩缩容实验。Fission 包存储新增 1 GiB 计费卷，ES 保持已有 20 GiB；节点数量不变。
没有增加 HA 控制平面或公网 LoadBalancer。具体金额以 DigitalOcean 账单为准。

Retain 防止误删 PVC 时自动丢数据，但不会自动停止磁盘费用。验收结束应先导出需要的
数据和证据，再明确清理 Fission、ES、PVC/PV 对应的云磁盘与节点。暂未执行资源销毁。

版本依据：[Fission compatibility](https://fission.io/docs/installation/compatibility/)、
[metrics-server 0.9.0](https://github.com/kubernetes-sigs/metrics-server/tree/v0.9.0)。
