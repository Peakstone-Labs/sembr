# 快速上手

## 前置要求

- Docker + Docker Compose
- 一个免费的 [SiliconFlow](https://siliconflow.cn) API Key（用于 BGE-M3 向量化）

## 第一步：克隆并配置

```bash
git clone https://github.com/Peakstone-Labs/sembr.git
cd sembr
cp .env.example .env
```

用编辑器打开 `.env`，填入你的 SiliconFlow Key：

```
EMBEDDER_API_KEY=sk-your-actual-key-here
```

`EMBEDDER_API_KEY` 是唯一必填项。缺失或为空时容器会在启动时立即退出。

## 第二步：启动所有服务

```bash
docker compose up --build
```

首次运行需要拉取镜像（`qdrant/qdrant:v1.17.1` ~100 MB、`python:3.12` ~140 MB），网络较慢时需 5–10 分钟。

## 第三步：验证健康状态

```bash
curl -i http://localhost:8000/health
```

向量化探针运行期间返回：

```
HTTP/1.1 503 {"status":"degraded","components":{"embedder":"loading",...}}
```

探针成功后返回：

```
HTTP/1.1 200 {"status":"ok","components":{"embedder":"ok",...}}
```

## 第四步：配置邮件投递

邮件是当前唯一的内置渠道。在 `.env` 中填好 SMTP：

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@example.com
SMTP_PASSWORD=your-app-password
SMTP_FROM=you@example.com
```

`SMTP_HOST` 留空即关闭邮件投递，应用其余部分仍可运行。

## 第五步：创建第一个意图

```bash
curl -X POST http://localhost:8000/intents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "fed-em-fx",
    "text": "美联储政策对新兴市场汇率的影响",
    "timezone": "America/New_York",
    "schedule": {"mode": "cron", "preset": "daily", "hour": 8, "minute": 0},
    "channels": [{"type": "email", "to": ["you@example.com"]}]
  }'
```

这条意图会在每天纽约时间 08:00 触发，邮件中的时间戳也按该时区渲染。如需事件驱动模式（攒到 N 篇匹配文章 / 等待 T 秒后发送），将 schedule 换为 `{"mode": "event", "trigger_count": 3, "max_wait_seconds": 1800}` 即可。

## 监控面板

浏览器访问 **http://localhost:8000/dashboard**，可查看每个 Feed 的抓取状态、向量化延迟和文章管道计数。

!!! warning "安全提示"
    只要端口暴露在 localhost 之外，务必在 `.env` 中设置 `DASHBOARD_TOKEN`。未设置时，Feed URL 和错误信息对局域网内所有人可见。

## 自定义提示词模板

仪表盘的 **Templates** 标签（位于 Intents 和 Logs 之间）是提示词模板的运行时编辑器：

- **Duplicate** 内置只读 `default` 模板（system 和 instruction 各一个），改副本
- **Rename** —— 单请求里完成文件移动 + 引用该模板的所有 intent 字段的级联更新
- **Delete** 未被引用的模板（被 intent 引用的会返回 HTTP 409 并列出依赖）

保存时服务端会用空字符串占位符走一次严格 dry-render：在 instruction 模板里写个 `{intent}` 这种笔误（允许的占位符是 `{intent_text}`、`{articles}`）会在落盘前以 HTTP 422 拒绝。也可以直接在宿主机的 `./prompts/{system,instruction}/` 编辑——打包的 `docker-compose.yml` 把 `./prompts` 以读写方式 mount 给容器，summarizer 每个 tick 重读盘（无缓存）。

单文件上限 64 KiB；保留名 `default` 在两个子目录里都是只读。完整 CLI 操作示例见 README 的 "Custom prompt templates" 段。

## 数据持久化

Feed 列表和文章指纹存储在 `./data/sembr.db`（SQLite，从宿主机挂载）。重建镜像和重启容器后数据仍然保留，只有 `rm -rf ./data/` 才会永久删除。

!!! note "文件系统要求"
    `./data/` 必须放在本地文件系统（ext4 / APFS / NTFS 本地盘）。SQLite WAL 模式在网络共享路径（NFS、SMB、virtio-9p）下不安全。

## 修改端口

在 `.env` 中设置 `SEMBR_HOST_PORT=8080` 可将 API 暴露在 `localhost:8080`。容器内绑定端口在 Dockerfile CMD 里硬编码为 `8000`。
