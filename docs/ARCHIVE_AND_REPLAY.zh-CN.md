# 私有归档、本地恢复与云资源收尾

工作目录：`<project-root>`。

本文件描述实际采用的归档流程。云端是否已销毁以 `docs/evidence/cloud-retirement.json` 的最新状态为准；停止 Job、暂停 automation、导出成功都不等于停止云资源计费。

## 三种入口

| 入口 | 数据 | 依赖 |
|---|---|---|
| `frontend/transitpulse.html` | 冻结实验的汇总结果 | 浏览器；可直接打开，无接口请求 |
| 8765 本地恢复观察台 | 私有归档中的真实帖子与汇总 | Docker 本地 ES/API + Python 页面服务 |
| 原 9090 demo | 720 条合成帖子、60 条合成新闻 | 原 `compose.yaml`；与真实实验分开 |

## 已完成验收

18 个索引共 434,092 条完整文档指纹一致；11 组云端/本地聚合响应一致；11 项路由与真实分页检查通过；离线统计重算一致。本轮测试 208 项通过、1 项跳过。原始私有 ZIP 使用逐成员 SHA-256 核验，独立副本和资源销毁状态单独记录。

## 本地恢复

归档位置：`data/archives/iran-20260228-final/`。包含 `manifest.json`、`index-metadata.json` 和每个索引的完整 `_id`/`_source` gzip NDJSON。`restore-verification.json` 是逐文档恢复验收结果。`data/experiments/iran-20260228/processed.ndjson` 只是分析字段导出，不能代替完整归档。

```powershell
uv sync --frozen --extra experiment
docker compose -f compose.replay.yaml up -d --build

# 只对全新的专用 ES 实例执行一次；任何同名索引存在时拒绝覆盖。
uv run --no-sync python -m scripts.archive_project restore --directory data/archives/iran-20260228-final

# 之后重启容器即可，数据在 transitpulse-replay_replay-es 卷中。
uv run --no-sync python -m scripts.archive_project verify --directory data/archives/iran-20260228-final
uv run --no-sync python -m scripts.portfolio_snapshot verify
uv run --no-sync python -m scripts.serve_event_dashboard --local-api http://127.0.0.1:9092 --port 8765
```

API：`http://127.0.0.1:9092/api/v1`。Elasticsearch：`http://127.0.0.1:19222`。两者仅映射本机回环地址。恢复使用与原云端相同的 ES 8.19.22，镜像 digest 固定，运行同一份 API 路由及精确 Jev 默认版本。没有模型 API 凭据、没有后台采集、没有推理任务。

停止本地环境并保留数据：`docker compose -f compose.replay.yaml down`。不要附加 `--volumes`，除非明确准备从完整归档重新恢复。

## 离线重新计算

```powershell
uv run --no-sync python -m scripts.replay_analysis
uv run --no-sync python -m scripts.portfolio_snapshot build
```

第一条命令从完整归档重新检查输入 ID、文本 SHA-256、模型/题目契约与评分门控，重算日统计、阶段统计、bootstrap 关联与费用，并与原分析比较；输出到 `artifacts/offline-analysis/`。不访问云端或模型 API。第二条命令将已保存的汇总响应重新嵌入独立 HTML。

## 归档的范围和边界

- 正式实验：124,491 条原始帖子、90,576 条 Jev 结果、90,587 条调用回执。
- 完整数据库还包含前期 pilot、1,047 条旧 RoBERTa 结果和 26 条结构化 LLM 注释；保留它们不代表它们属于同一个实验窗口。
- 导出先确认无活动 Job/未暂停 CronJob，再为 `v2_*` 应用索引开启写入阻断。读 API 保持可用。归档源保持冻结，不能在这些索引上重启采集/推理。
- 每份 gzip 校验文件 SHA-256；顺序无关的文档指纹覆盖 ID、完整 source 及 routing。恢复只允许 loopback，要求同 ES 版本，并拒绝覆盖已有索引。
- 没有导出 ES 系统安全索引、云端访问令牌或 Secret 明文。未来重建云环境需要重新配置凭据；本地结果回放不需要它们。
- 文件逻辑归档不是原虚拟机快照，不保留 Lucene 段、集群 UUID 或已关闭 Pod 的运行内存。

## 删除云资源前的条件

1. 全部应用索引导出完成，`manifest.complete=true`。
2. 本地恢复的每个索引通过数量及完整文档指纹检查。
3. 原云端与本地 API 的 11 组聚合响应一致；本地真实帖子分页与旧模型访问另做验证。
4. 私有归档包及代码副本已生成校验和，并存入第二个独立位置。
5. 云控制台登录可用，资源身份与归档清单对应。

本机 C/D/E 是同一个物理磁盘的分区；在其间复制不等于独立备份。未获得外部副本位置时，不将备份写成已完成双副本，也不将云端存储写成已安全销毁。

## 待销毁资源的身份

- 集群：`k8s-1-34-10-do-5-sgp1-1790830229700`，区域 SGP1。
- 节点：`pool-v394x3mp2-3xp5y2`、`pool-v394x3mp2-3xp5yl`，各 4 vCPU / 8 GiB。
- ES 云盘：`1d1d9f7b-bd5e-11f1-b2ef-fe722b6d0cd1`，20 GiB。
- Fission 云盘：`bd1e7987-bd61-11f1-b2ef-fe722b6d0cd1`，1 GiB。

以上两个 PV 都采用 Retain 策略。控制台删除集群后仍需核对关联云盘、工作节点及独立快照/负载均衡器等，不能只检查 Kubernetes 内部对象。此前已发生的用量仍可出现在后续账单。

官方流程：[销毁 DOKS 集群](https://docs.digitalocean.com/products/kubernetes/how-to/destroy-clusters/)。本项目不使用批量删除整个账号资源的命令。

## 私有恢复包

生成：`uv run --no-sync python -m scripts.bundle_private_archive`。输出 `artifacts/retirement/transitpulse-private-recovery.zip` 及 `.manifest.json`。包含完整索引归档、各阶段私有数据、SQLite 一致性备份、源码与本地 Git 历史、部署包、验证日志、截图和视频。当前运行凭据文件（`data/private`）、kubeconfig 和 ES 安全索引不进入包；Git 历史未经历史凭据全面清理，不能据此声称恢复包完全不含旧凭据。

包内每个文件的 SHA-256 在 `PACKAGE-MANIFEST.json`，生成后逐成员重读验证。第二位置复制完成后应再核对整个 ZIP 的 SHA-256。ZIP 包含原始帖子及历史代码，只用于私人恢复，不是可直接公开的作品集发布包；公开展示使用独立 HTML 和经审查的当前源码。

已生成并逐成员核验私有恢复包：382,428,363 字节，834 个成员，SHA-256 `c4aecb731286f6e04aab696f8ed964d5cc1a75c567cac81e21ebee4dd851dcef`。独立副本尚未确认。
