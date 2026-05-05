# 配置参考

sembr 使用 `pydantic-settings`，优先级从高到低：

```
运行时 API 覆盖
    │
Docker secrets
    │
环境变量
    │
.env 文件
    │
sembr.yaml 文件
    │
内置默认值
```

!!! warning "注意"
    不要在 `docker-compose.yml` 的 `environment:` 块中硬编码字段值——会破坏优先级链，导致运行时 API 覆盖失效。

## 必填

| 变量 | 说明 |
|------|------|
| `EMBEDDER_API_KEY` | SiliconFlow（或 OpenAI 兼容）API Key。缺失时容器启动即退出。 |

## 向量化（Embedder）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EMBEDDER_API_BASE_URL` | `https://api.siliconflow.cn/v1` | OpenAI 兼容的 `/v1/embeddings` 端点 |
| `EMBEDDER_MODEL` | `BAAI/bge-m3` | 向量化模型名称 |
| `EMBEDDER_BATCH_SIZE` | `32` | 每次请求的文章数量 |

## 采集器（Collector）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `COLLECTOR_POLL_INTERVAL_MINUTES` | `30` | RSS 默认轮询间隔（分钟） |
| `COLLECTOR_LOOKBACK_HOURS` | `24` | 首次运行时回溯的时间范围 |

## 匹配器（Matcher）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MATCHER_INTERVAL_MINUTES` | `5` | 匹配任务运行间隔（分钟） |
| `MATCHER_DEFAULT_THRESHOLD` | `0.75` | 默认相似度阈值（0.20–0.95） |
| `MATCHER_LOOKBACK_MINUTES` | `60` | 只匹配该时间窗口内入库的文章 |

## 通知渠道（Notifier）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TELEGRAM_BOT_TOKEN` | — | Telegram Bot Token（来自 @BotFather） |
| `DISCORD_WEBHOOK_URL` | — | Discord Incoming Webhook URL |
| `SLACK_WEBHOOK_URL` | — | Slack Incoming Webhook URL |

## 监控面板（Dashboard）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DASHBOARD_TOKEN` | — | `/dashboard` 和 `/api/dashboard/*` 的认证 Token。为空则无认证（仅限局域网使用）。 |

## 基础设施

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant 服务地址 |
| `DATABASE_PATH` | `/app/data/sembr.db` | SQLite 数据库文件路径 |
| `API_PORT` | `8000` | 容器内部绑定端口（不要修改） |
| `SEMBR_HOST_PORT` | `8000` | 宿主机暴露端口 |

## LLM（可选）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_BACKEND` | `api` | `api` 或 `local` |
| `LLM_API_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容的 chat completions 端点 |
| `LLM_API_KEY` | — | LLM 后端 API Key |
| `LLM_MODEL` | `deepseek-chat` | 模型名称 |
