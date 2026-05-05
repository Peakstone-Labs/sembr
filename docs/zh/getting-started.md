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

## 创建第一个意图

```bash
curl -X POST http://localhost:8000/intents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "fed-em-fx",
    "text": "美联储政策对新兴市场汇率的影响",
    "channels": [{"type": "telegram", "chat_id": "@yourchannel"}]
  }'
```

匹配任务每 5 分钟运行一次，匹配到的文章会以推送通知形式送达。

## 监控面板

浏览器访问 **http://localhost:8000/dashboard**，可查看每个 Feed 的抓取状态、向量化延迟和文章管道计数。

!!! warning "安全提示"
    只要端口暴露在 localhost 之外，务必在 `.env` 中设置 `DASHBOARD_TOKEN`。未设置时，Feed URL 和错误信息对局域网内所有人可见。

## 数据持久化

Feed 列表和文章指纹存储在 `./data/sembr.db`（SQLite，从宿主机挂载）。重建镜像和重启容器后数据仍然保留，只有 `rm -rf ./data/` 才会永久删除。

!!! note "文件系统要求"
    `./data/` 必须放在本地文件系统（ext4 / APFS / NTFS 本地盘）。SQLite WAL 模式在网络共享路径（NFS、SMB、virtio-9p）下不安全。

## 修改端口

在 `.env` 中设置 `SEMBR_HOST_PORT=8080` 可将 API 暴露在 `localhost:8080`。`API_PORT` 控制容器内部绑定端口，保持 `8000` 不变即可。
