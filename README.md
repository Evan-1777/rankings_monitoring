# rankings_monitoring

监控 [致知｜大模型智力长期追踪榜单](https://llm2014.github.io/llm_benchmark/) 的新增收录模型与分数变更。

当前范围是站点中的推理（`logic`）与 Agentic（`code_v3`）分类，共 37 个榜单；视觉（`vision`）与历史 `code` 分类不同步。

## 功能

- 按固定间隔拉取榜单目录与 CSV，使用 ETag 条件请求减少流量。
- 识别四类事件：新榜单、新增模型、分数变更、模型移除。
- 中文看板包含变更动态、当前榜单、分类筛选与运行状态。
- SQLite 持久化快照与事件；首次启动只建立基线，不回放历史。
- 标准库实现，无第三方 Python 依赖；镜像支持 amd64 与 arm64。

## Docker Compose 部署

镜像发布在 GitHub Container Registry，包为公开，无需登录：

```bash
docker compose pull
docker compose up -d
```

打开 http://localhost:8080 。修改宿主端口：

```bash
HOST_PORT=8090 docker compose up -d
```

数据存放在命名卷 `rankings-data`。若改为宿主机目录挂载，目录需要允许容器内 UID `10001` 写入。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POLL_INTERVAL` | `900` | 轮询间隔秒数，最小 60 |
| `PORT` | `8080` | 容器内监听端口 |
| `DATA_DIR` | `./data` | SQLite 数据目录 |
| `SOURCE_URL` | `https://llm2014.github.io/llm_benchmark/` | 上游站点地址 |
| `REQUEST_TIMEOUT` | `20` | 单次请求超时秒数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 检测规则

- 数据目录出现新的 CSV：产生一条“新榜单”事件；其中此前未在任何监控榜单出现过的模型，再产生“新增模型”事件。
- 已有榜单新增模型：逐条产生“新增模型”事件。
- 已有榜单的分数列变化：按模型聚合为一条“分数变更”事件，携带字段级旧值与新值。
- 模型从榜单消失：产生“移除”事件。
- 推理分类的分数列为“极限分数”“中位分数”；Agentic 分类的分数列为各任务列。
- 内容未变化的榜单不会重复产生事件；空响应、HTML 错误页或缺少模型列的 CSV 会被拒绝，保留上一版快照。

## 本地运行

需要 Python 3.12 或更高版本：

```bash
python3 app.py
python3 -m unittest discover -s tests -v
```

默认监听 8080，数据写入 `./data/rankings.db`。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/overview` | 运行状态、模型/榜单/事件计数、分类统计 |
| GET | `/api/events?type=&category=&limit=&offset=` | 变更事件，默认最近 50 条 |
| GET | `/api/leaderboard?category=&dataset=` | 分类下的榜单列表与选中榜单数据 |
| POST | `/api/refresh` | 触发一次检查，15 秒冷却 |
| GET | `/healthz` | 健康检查 |

`type` 可取 `dataset_added`、`model_added`、`score_changed`、`model_removed`。

## 镜像构建

推送 `main` 或 `v*` 标签会触发 GitHub Actions：先运行单元测试，再构建并推送两个架构的镜像到 `ghcr.io/evan-1777/rankings_monitoring`。

本地构建：

```bash
docker build -t rankings_monitoring .
docker run --rm -p 8080:8080 -v rankings-data:/data rankings_monitoring
```
