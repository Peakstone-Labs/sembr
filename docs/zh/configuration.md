# 配置参考

sembr 使用 `pydantic-settings`，优先级从高到低（高优先级覆盖低优先级）：

```
shell 环境变量
    │
.env 文件
    │
sembr.yaml 文件（可选，位于工作目录）
    │
内置默认值
```

目前**未支持** `secrets_dir=` —— Docker secrets 进入容器后是以 shell 环境变量的形式呈现，跟普通 env 同优先级。

!!! warning "注意"
    不要在 `docker-compose.yml` 的 `environment:` 块中硬编码字段值——它跟宿主机 shell `export` 同级，会静默盖掉后续 `.env` 修改，并且使运行时设置编辑器的"改完即重启"流程失效。

每个 intent 和 feed 自身的取值（相似度阈值、扫描间隔、回溯窗口、轮询节奏……）保存在数据库的 `Intent` / `Feed` 行上，通过 REST API 或仪表盘管理，不走环境变量。如果某个开关你在下面找不到，去 [api 模块文档](../modules/api.md) 看接口。

## 必填

| 变量 | 说明 |
|------|------|
| `EMBEDDER_API_KEY` | SiliconFlow（或任何 OpenAI 兼容）`/v1/embeddings` 端点的 API Key。缺失或为空时容器启动即非零退出 |

LLM 默认共享同一把 Key（SiliconFlow 同时托管 BGE-M3 和 DeepSeek-V4-Flash），通常一把就够。

## 存储

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant 服务地址。打包的 `docker-compose.yml` 已配好这个地址 |
| `SQLITE_PATH` | `/app/data/sembr.db` | 容器内 SQLite 数据库路径。宿主机通过 compose bind mount 把 `./data/` 挂到这里 |
| `SEMBR_HOST_PORT` | `8000` | 宿主机暴露端口。容器内绑定端口在 Dockerfile CMD 里硬编码为 `8000`，宿主侧从这里改 |

## 向量化（Embedder）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EMBEDDER_BACKEND` | `siliconflow` | 当前只内置 `siliconflow` |
| `EMBEDDER_API_BASE_URL` | `https://api.siliconflow.cn/v1` | OpenAI 兼容 `/v1/embeddings` 端点。指向其他同协议供应商即可换厂商 |
| `EMBEDDER_MODEL` | `BAAI/bge-m3` | 传给端点的模型名 |
| `EMBEDDER_TIMEOUT_SECONDS` | `30` | 启动探针 + httpx 客户端默认超时。批量 embed 调用使用动态超时 `max(30s, total_chars / 1500)`，所以小于 30 的值**不会**收紧批量路径 |

## LLM（摘要）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_API_BASE_URL` | `https://api.siliconflow.cn/v1` | OpenAI 兼容 `/v1/chat/completions` 端点 |
| `LLM_API_KEY` | — | 留空时默认与 `EMBEDDER_API_KEY` 共用 |
| `LLM_MODEL` | `deepseek-ai/DeepSeek-V4-Flash` | 模型名 |
| `LLM_TIMEOUT_SECONDS` | `60` | 单次请求 HTTP 超时 |
| `LLM_MAX_PROMPT_CHARS` | `2_000_000` | prompt 端总字符预算（system + instruction + 文章）。pipeline 预留 ~15% 给响应，剩余按 water-fill 喂文章——短文章保留完整，只裁最长几篇。按你的模型 ctx 调整：DeepSeek-V4-Flash 1M token ctx 折合 ~2M 中文字 / ~4M 英文字，`2_000_000` 宽松；本地 8K-token 模型应降至 `~16_000`。单位是字符不是 token，非英文场景请保守一些。下界 `2_000` |

目前只内置 API 风格的 backend（任何 OpenAI 兼容 `/v1/chat/completions` 端点）。

## 邮件通知

邮件是当前唯一的内置通知渠道。`SMTP_HOST` 留空即关闭邮件投递，应用其余部分仍可运行。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SMTP_HOST` | `""` | SMTP 服务器主机名（如 `smtp.gmail.com`、`smtp.sendgrid.net`）。留空关闭邮件 |
| `SMTP_PORT` | `587` | SMTP 端口。`587` 走 STARTTLS（默认），`465` 走 `SMTP_SSL` |
| `SMTP_USERNAME` | `""` | SMTP 登录用户名。留空跳过 `AUTH` |
| `SMTP_PASSWORD` | `""` | SMTP 登录密码（`SecretStr`，永不输出到日志） |
| `SMTP_FROM` | `""` | `From:` 地址。留空时回退到 `SMTP_USERNAME` |
| `SMTP_USE_STARTTLS` | `true` | 在 plain SMTP 连接后执行 `STARTTLS` |
| `SMTP_USE_SSL` | `false` | 直接走 `SMTP_SSL`（端口 465 风格）。`true` 时 `SMTP_USE_STARTTLS` 被忽略 |

每条 intent 自己的 timezone（`Intent.timezone`）才是邮件模板渲染 `published_at` 的依据；下面 `DISPLAY_TIMEZONE` 仅供仪表盘消费，不参与邮件渲染。

## 仪表盘 & 日志

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DASHBOARD_TOKEN` | `""` | `/dashboard` 和 `/api/dashboard/*` 的可选共享密钥。留空关闭认证——只要端口暴露在 localhost 之外就务必设置（feed URL 与 dead-article 错误信息否则对外可见） |
| `DASHBOARD_POLL_INTERVAL_SECONDS` | `10` | 前端 snapshot 轮询节奏。范围 `[2, 120]`。通过 `/api/dashboard/config` 暴露给打包的 JS |
| `DASHBOARD_LOG_RETENTION_DAYS` | `7` | `feed_fetch_log` / `embed_call_log` 行的最大保留天数。范围 `[1, 90]` |
| `DASHBOARD_LOG_MAX_PER_FEED` | `1000` | 单 feed 上 `feed_fetch_log` 行的 FIFO 上限。范围 `[10, 100000]` |
| `DASHBOARD_LOG_LEVEL` | `INFO` | 启动时套用到全部 7 个 LogBus tag 的默认 level，`DEBUG / INFO / WARNING / ERROR` 之一。仪表盘 `PUT /api/dashboard/logs/level` 可在运行时调单个 tag，但只在进程内存中保存，重启后失效 |
| `DASHBOARD_LOG_BUFFER_PER_TAG` | `1000` | 每个日志 tag 的 ring buffer 容量。范围 `[100, 10000]`。内存约 `7 × buffer × ~500 B`，上限大约 35 MB |

## 显示

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DISPLAY_TIMEZONE` | `Asia/Shanghai` | 仪表盘渲染时间戳用的 IANA 时区。**不**参与邮件通知渲染——邮件用的是每条 intent 自己的 `timezone` 字段 |

## 提示词模板

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROMPTS_DIR` | `/app/prompts` | 模板根目录。子目录为 `system/` 和 `instruction/`。宿主机改模板下一个 tick 即生效，无需重启。可通过 `SEMBR_PROMPTS_DIR` 覆盖 |

## Lifespan / 关停

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LIFESPAN_SHUTDOWN_TIMEOUT` | `8.0` | lifespan 优雅关停的最大秒数，超出强制退出。设小于 docker stop 的 SIGKILL 截止（默认 10s）。仅适用于自重启路径（如 settings 保存 → SIGTERM）；普通 `docker compose down` 不受影响 |

## 采集器 / RSSHub

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_HOSTS` | `rsshub:1200` | 逗号分隔的 `host[:port]` 列表，用于标识"前置代理多个后端"的网关（打包的 RSSHub 实例就是典型例子）。对这些主机，per-host 并发限流器会按 URL 第一段路径再细分一层，避免代理后面的多个后端共用一个 semaphore |

## RSSHub 透传变量

下列环境变量原样透传给打包的 RSSHub 容器——它们由 RSSHub 自己读，不被 sembr 代码消费。设置编辑器接受新键的条件:符合 `^[A-Z][A-Z0-9_]*$` 且以下列前缀之一开头:`TWITTER_`、`TELEGRAM_`、`GITHUB_`、`RSSHUB_`、`SOCIAL_`、`OPENAI_`。

| 变量 | 用途 | 备注 |
|---|---|---|
| `TWITTER_COOKIE` | RSSHub Twitter 路由 | 浏览器登录态 cookie 全文,至少包含 `auth_token=...; ct0=...` |
| `TELEGRAM_TOKEN` | RSSHub Telegram 路由 | BotFather 颁发的 bot token,适用于公开频道 |
| `TELEGRAM_SESSION` | RSSHub Telegram 路由 | Telethon / Pyrogram 生成的 user session 字符串,用于受限频道 |
| `GITHUB_ACCESS_TOKEN` | RSSHub GitHub 路由 | PAT —— 把 API 速率上限从 60 提升到 5000 req/h |
