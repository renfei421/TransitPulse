# Elasticsearch 与持久化存储恢复

2026-10-01 完成，最终 Green / Ready。已通过真实 TLS、认证、读写、bulk upsert、
越权拦截和 Pod 重建后的持久化验收；[机器可读证据](evidence/cloud-elasticsearch-2026-10-01.json)
记录了前后 Pod UID、保留的 PVC/PV 及测试记录 ID。单次重建到验收收尾约 127 秒，
这不是持续测量的服务中断时间或承诺的恢复 SLA。

本阶段在 DigitalOcean 的 SGP1 集群上重建数据库服务。原学校云端的数据尚未恢复或导入。
工作目录为 `C:\Users\RENFEI\Desktop\cloud cluster\COMP90024_group_repo`。

## 部署配置

| 项目 | 配置 |
|---|---|
| 管理器 | Elastic 官方 ECK 3.5.0，镜像锁定 SHA-256；仅监听 elastic 命名空间 |
| 数据库 | Elasticsearch 8.19.22，镜像锁定 SHA-256，Basic license |
| 服务地址 | https://elasticsearch-es-http.elastic.svc:9200 |
| 访问方式 | ClusterIP；本地验收使用仅绑定 127.0.0.1 的临时端口转发 |
| 数据节点 | 1 个，CPU request 500m / limit 2，内存 request / limit 均为 2 GiB |
| JVM heap | 1 GiB |
| 持久卷 | 20 GiB，do-block-storage-retain，ReadWriteOnce |
| 应用索引 | v2_ 前缀，显式迁移建立 12 个业务索引，1 primary / 0 replicas |
| TLS | ECK 管理证书；客户端校验 CA 和主机名，不关闭 verify_certs |

沿用 8.19 系列以兼容当前应用，同时使用已发布的 8.19.22 修补版本。
本地 Docker 验收环境仍为原先的 8.19.1；此前本地基准不能直接当成这次云端基准。
此配置适合短期功能验收，单节点重启期间会中断服务。它不提供 Elasticsearch 高可用。
为了避免更改宿主机内核参数，本配置关闭 mmap；正式容量测试前需重新规划内存与
vm.max_map_count，并重新测试性能。

## 凭据与权限

ECK 管理员凭据保存在 elastic 命名空间的 `elasticsearch-es-elastic-user` Secret。
业务程序使用 default 命名空间的独立凭据：

| Secret | 用途 |
|---|---|
| transport-secrets | transport-app，仅对 v2_* 查询、写入、更新、bulk upsert 和读取索引元信息，并可读取集群监控信息 |
| transport-migration-secrets | transport-migration，仅管理 v2_* 的索引结构，并可读取集群监控信息 |
| transport-es-ca | ca.crt，供后续 Fission/Job 挂载 |

密码由脚本随机生成，经标准输入提交 Kubernetes，未写入仓库、命令行参数或证据文件。
重复执行 provision 复用现有密码，保留同一 Secret 中其他来源的 API key。
后续若更换函数运行命名空间，需要同步这两个应用必需的 Secret；迁移账号只用于迁移 Job。
ECK 轮换 CA 后也需要重新同步 `transport-es-ca`，当前脚本不提供持续证书同步控制器。

ES 8.x 的内置 write 权限仍包含显式修改 mapping 的能力，因此本配置使用具体写入
action 权限，并验证显式修改 mapping 返回 403。为兼容现有动态字段 schema，保留
自动新增字段的 auto_put 权限；这不等于严格禁止一切 mapping 变化。升级 ES 时必须
重新验证这些 action 权限和 bulk 行为，不能只修改镜像版本。业务账号也没有删除
文档、创建索引或管理用户的权限。

## 重现部署与验收

从仓库根目录执行；所有命令显式指定 kubeconfig 和对应上下文，不依赖个人默认集群。
install 会创建 20 GiB 计费磁盘，verify 的重启选项会短暂停服。

~~~powershell
$cloudConfig = '..\k8s-1-34-10-do-5-sgp1-1790830229700-kubeconfig.yaml'
uv run --no-sync python -m scripts.cloud_elasticsearch install --kubeconfig $cloudConfig
uv run --no-sync python -m scripts.cloud_elasticsearch provision --kubeconfig $cloudConfig
uv run --no-sync python -m scripts.cloud_elasticsearch verify --restart-pod --kubeconfig $cloudConfig
~~~

install 校验 `deploy/cloud/eck-lock.json` 中的官方 YAML 摘要，缓存到 artifacts，
安装 ECK 后应用 `deploy/cloud/elasticsearch.yaml`。已存在的不同 ECK 版本不会被自动覆盖。
provision 建立账号、同步证书，使用迁移账号执行当前项目的 database.migrate。
verify 会真实检查未认证/错误密码的 401、越权操作的 403、正确凭据的读写，以及
Pod 重建前后的记录内容、Pod UID、PVC UID 和 PV 绑定。测试记录带 synthetic 标记，
保存在单独的 v2_cloud_storage_probe 索引中，不作为真实采集数据。

默认 verify 不重启 Pod，但仍会写入一条验收记录；它不是纯只读健康检查。
临时隧道和本地证书临时目录由脚本在结束时清理。运行结果保存在
`artifacts/cloud-elasticsearch-verification.json`，已完成的验收另存至 docs/evidence。
本次权限调试与最终验收累计留下 3 条 synthetic 探针记录；12 个业务索引当前均为空。

后续渲染应用发布必须传入 `--es-replicas 0`，GitLab 部署变量设置 `ES_REPLICAS=0`。
通用渲染器仍默认 1，以保留原多节点部署行为。迁移只为新索引设置副本数；
将来增加 ES 节点时，需要另行显式调整已有索引副本数并验证 shard 分配。

## 持久化范围与清理

`DeleteOnScaledownOnly` 使删除 Elasticsearch CR 时保留 PVC；但缩容仍可能删除对应 PVC。
StorageClass 的 Retain 策略进一步保留底层 PV/磁盘，降低误删数据风险。
这些机制不是备份；磁盘故障、整集群删除、跨节点挂载和快照恢复仍需单独验证。

20 GiB 存储按 US$0.10/GiB/月计，约 US$2/月，实际按小时计费。即使删除 Pod 或
Elasticsearch CR，保留的磁盘仍可能继续计费。结束短期验收时，先导出数据与配置，
再按验收记录中的 PVC/PV 名核对 DigitalOcean Volumes，显式处理保留卷。不要以
Pod 列表为空作为已经停止所有计费的判断依据。本阶段不创建 LoadBalancer。

## 官方依据

- [ECK YAML 安装](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/install-using-yaml-manifest-quickstart)
- [ECK 兼容范围](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s)
- [Elasticsearch 8.19.22](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/release-notes-8.19.22.html)
- [ECK 持久卷与删除策略](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/volume-claim-templates)
- [ES 8.19 权限定义](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/security-privileges.html)
- [8.19.22 action 权限实现](https://github.com/elastic/elasticsearch/blob/v8.19.22/x-pack/plugin/core/src/main/java/org/elasticsearch/xpack/core/security/authz/privilege/IndexPrivilege.java)
- [mmap 与内核参数](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/virtual-memory)
- [DigitalOcean 存储计费](https://docs.digitalocean.com/products/volumes/details/pricing/)
