# 微信公众号接入（附加）

本指南介绍如何把**微信公众号**文章作为可选附加项接入 sembr。微信没有官方的订阅 API，因此本方案依赖一个
**自托管的第三方桥**（[wechat2rss](https://wechat2rss.xlab.app/)）把公众号文章转成全文 RSS，sembr 再用既有
的 RSS 能力消费它 —— **不改 sembr 代码、不新增 source type**。

!!! warning "可选、第三方、风险自负"
    - sembr **不**捆绑、不分发、不背书这个桥 —— 由你自行部署和运维。
    - wechat2rss 自托管需要**付费授权**，并需要你**自备一个专用微信号**（用小号，别用主力号 —— 封号风险真实存在，须自行承担）。
    - 是否合规使用，由你按所在地的微信服务条款自行判断。
    - **没有官方 API**，这是社区 workaround，可用性取决于上游桥。

!!! note "为什么要用桥？"
    微信生态是封闭的 —— 没有公开、官方的方式去订阅任意公众号。桥用一个真实微信号登录，把已关注的公众号重新发布成
    RSS。一旦变成 RSS，sembr 就把它当成普通 RSS feed 处理（去重、全文提取、embedding、intent 匹配、digest）。

---

## TL;DR（6 步清单）

1. 购买 **wechat2rss** 自托管授权。
2. 部署 **wechat2rss** 容器（`docker compose up -d`）。
3. 打开它的管理界面，**用专用微信号扫码登录**。
4. **订阅**你要的公众号（每个号贴一篇文章链接），复制各自的 feed URL。
5. 把 wechat2rss 和 sembr 接到**同一个 Docker 网络**，让 sembr 按容器名访问它。
6. 在 sembr 里**新建一条 `rss` feed**，指向桥的 URL。

下面逐步说明。

---

## 1. 前置条件

- 一个运行中的 sembr 部署（Docker Compose）。
- 一份 **wechat2rss 自托管授权** —— 见 [wechat2rss.xlab.app](https://wechat2rss.xlab.app/)（卖的是软件授权，不含在线服务）。
- 一个**专用、非主力的微信号**作为桥的取数源。它的登录态会定期过期（数天量级）需重新扫码，请提前规划。
- 与 sembr 同一台主机上的 Docker + Docker Compose（网络最简单）。

## 2. 部署 wechat2rss

新建 `~/wechat2rss/docker-compose.yml`：

```yaml
services:
  wechat2rss:
    container_name: wechat2rss
    image: "ttttmr/wechat2rss:latest"
    env_file:
      - .env
    volumes:
      - ./data:/wechat2rss          # 持久化登录态、设置、文章缓存
    ports:
      - "8080:8080"                 # 管理界面 + feeds；若只用隧道访问可绑 127.0.0.1:8080
    networks:
      - wechat2rss-net              # 共享网络，让 sembr 按名字访问（见 §3）
    deploy:
      restart_policy:
        condition: on-failure
        max_attempts: 3
        window: 10s
    logging:
      driver: "json-file"
      options:
        max-size: "100m"
networks:
  wechat2rss-net:
    external: true                  # 在 §3 创建
```

新建 `~/wechat2rss/.env`（`chmod 600`，切勿提交）：

```ini
LIC_EMAIL=<授权邮箱>
LIC_CODE=<激活码>
RSS_HOST=<host:8080>        # 仅影响 UI 里展示的订阅链接
# 可选：重登 / 健康告警（任选其一）。不配的话，桥的登录态过期时会静默失败，你的公众号 feed 会悄悄停更：
# BOT_SERVER_KEY=<server酱 key>
# BOT_TG_TOKEN=<telegram bot token>   BOT_TG_ADMIN_UID=<你的 uid>
# BOT_WEBHOOK_URL=<webhook>           BOT_BARK_URL=<bark url>
```

启动并确认授权校验通过：

```bash
cd ~/wechat2rss && docker compose up -d
docker compose logs --tail=50            # 看授权的 "Expire" 行，以及管理界面 "Token"
```

!!! warning "登录态会过期，请配告警"
    绑定的微信登录态每隔数天过期、需要重新扫码。配置上面任一 `BOT_*` 渠道，让桥在需要重登时**主动通知你**，
    而不是悄悄变陈旧。（sembr 的 Feeds 页也会显示对应 feed 掉到 0，但那是滞后信号。）

### 绑号并订阅

1. 访问管理界面。若端口绑在 `127.0.0.1`，先开隧道：`ssh -L 8080:localhost:8080 <host>`，再打开
   `http://localhost:8080`，输入启动日志里的管理 Token。
2. **微信账号 → 添加账号 → 用专用微信号扫码**，按页面提示完成异地登录验证。
3. **订阅**：把每个目标公众号的任一篇文章链接（`https://mp.weixin.qq.com/s/...`）贴进去。成功后 UI 会显示该号
   的 feed 地址，形如 `http://<RSS_HOST>/feed/<biz_id>.xml`。

## 3. 把桥接到 sembr（生产级写法）

sembr 的 API 跑在容器里，必须通过**共享 Docker 网络按容器名**访问 wechat2rss —— 不要用
`host.docker.internal`，它只在 Docker Desktop 上存在，无法移植到 Linux Docker Engine。

创建一个 external 网络，两个 stack 都接上去（上面的 wechat2rss compose 已声明它）：

```bash
docker network create wechat2rss-net
```

sembr 的 `docker-compose.yml` 在仓库里、会被 `git pull` 覆盖，所以别直接改它。在它旁边加一个**本地**的
`docker-compose.override.yml` —— Docker Compose 会自动合并：

```yaml
# docker-compose.override.yml —— 本地附加层；不要纳入 git。
services:
  api:
    networks:
      - default            # 必须保留 default，否则 API 断开 qdrant / rsshub
      - wechat2rss-net
networks:
  wechat2rss-net:
    external: true
```

让它保持未跟踪，这样 `git pull` 不会冲掉：

```bash
grep -qxF docker-compose.override.yml .git/info/exclude || echo docker-compose.override.yml >> .git/info/exclude
docker compose up -d          # 在共享网络上重建 api 容器
```

验证 API 容器能按名字访问到桥：

```bash
docker exec sembr-api python -c \
  "import urllib.request as u; print(u.urlopen('http://wechat2rss:8080/feed/<biz_id>.xml', timeout=10).status)"
# 200
```

## 4. 在 sembr 里新建 feed

公众号 feed 就是一条 **RSS feed** —— 无需新 source type。URL 用桥的**容器名**地址。从 dashboard 的 **Feeds**
页加，或用 API：

```bash
curl -X POST http://127.0.0.1:8000/feeds \
  -H "X-Dashboard-Token: <DASHBOARD_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "<公众号名>",
    "url": "http://wechat2rss:8080/feed/<biz_id>.xml",
    "source_type": "rss",
    "poll_interval_minutes": 120,
    "tags": ["wechat"]
  }'
```

sembr 的 RSS 采集器会自动提取**全文**并去除 HTML 标签（公众号 feed 与其它 RSS 源一视同仁），无需额外清洗。给这些
feed 打标签（`"tags": ["wechat"]`）便于批量管理。

## 5. 注意事项与限制

- **双层轮询。** sembr 的 `poll_interval_minutes` 控制 sembr 多久去拉桥的 RSS 端点（读本地缓存，很轻），它**不**
  决定新鲜度：新文章多快出现，取决于桥自身的爬取节奏（通常数小时、上限约 24h）。比这更勤地轮询 sembr 只会重复拉到
  同一份缓存（被去重丢弃）。**sembr 侧 60–120 分钟比较合理。**
- **长文 embedding 会截断。** sembr 为控制 embedder token 预算，对 embedding 输入截断（约 8000 字符）。很长的公众号
  研报只会用开头部分做 embedding；全文仍会存储。
- **图片型文章正文很薄。** "一图读懂" / 海报类文章内容在图里，能抽出的文字很少，语义匹配质量自然差 —— 这是内容形态
  限制，不是 bug。
- **LAN 访问的取舍。** 把管理界面绑到 `0.0.0.0:8080` 可让局域网内任意设备访问（重登方便）；管理界面有 token 守门，
  但 feed URL 在 LAN 内可读。想更私密就绑 `127.0.0.1` 走隧道。
- **授权续期。** wechat2rss 授权到期后桥会停，公众号 feed 随之静默停更 —— 到期前记得设提醒。

---

→ 把 sembr 挂在公网 IP 上？硬化清单见 [Public server](../../deployment/public.md)。
→ 刚接触 sembr 的 feed？先看[快速上手](../getting-started.md)。
