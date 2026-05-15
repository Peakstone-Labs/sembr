<p align="center">
  <img src="assets/brand/logo-lockup.png" alt="sembr" width="320">
</p>

<p align="center">
  <b>你的私人情报分析员。</b><br>
  <i>你说盯什么、怎么分析 —— sembr 在你指定的信息流上持续扫描，靠语义（不是关键词）匹配，按你的要求出分析报告。</i>
</p>

<p align="center">
  <a href="https://github.com/Peakstone-Labs/sembr/actions/workflows/ci.yml"><img src="https://github.com/Peakstone-Labs/sembr/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="Dockerfile"><img src="https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white" alt="Docker"></a>
</p>

<p align="center">
  <a href="https://panel.peakstone-labs.com/#news"><b>在线 demo</b></a> ·
  <a href="README.md">English</a> ·
  <a href="https://peakstone-labs.github.io/sembr">文档站</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#给-ai-agent-用">给 AI agent 用</a> ·
  <a href="https://github.com/Peakstone-Labs/sembr/discussions">Discussions</a>
</p>

---

**sembr** 是一台**自部署的意图雷达**。你只需一次描述你的关注点——*"美联储政策对新兴市场货币的传导"*——它持续扫描 RSS 订阅、新闻 API 和社交信息流，通过语义向量将文章与关注意图匹配，并按你的分析框架生成报告。

<p align="center">
  <img src="assets/brand/hero.png" alt="sembr — 反向 RAG" width="720">
  <br>
  <sub><i>实时 demo：sembr 是 <a href="https://panel.peakstone-labs.com">Peakstone Labs A股看板</a> 里 News tab 的引擎。</i></sub>
</p>

<!-- TODO: 等部署后截 intent 编辑 / dashboard / digest email 三连屏，替换占位 -->

## 为什么是 sembr

- **语义，不是关键词。** intent 是一个 embedding，不是一串 `OR`。*"新兴市场货币传染"* 能匹中 *"土耳其里拉跳水，市场押注美联储再加息"* —— 一个共同词都没有。
- **中英开箱混用。** [BGE-M3](https://huggingface.co/BAAI/bge-m3) 是专门为 CJK + 英文混合内容选的。Bloomberg / Reuters / Nature /财联社 / 华尔街见闻 / 36氪 多语言信息源都能匹配上你的用单一语言写的关注意图。
- **每个 intent 自带分析视角。** 每个 intent 可绑定专属分析模板（system + instruction，dashboard 里改）。同一篇文章在 *"宏观资产配置视角"* 下能输出跨资产轮动信号和再平衡机会，在 *"短线大宗商品交易视角"* 下输出供需边际变化和短期交易催化 —— sembr 不只是"找到相关文章"，而是"按你的方式分析文章"。分析模板高度可定制化（后续会上线更多自带模板）。
- **Embedding 全免费，一份报告不到1毛钱。** 默认 embedder（[SiliconFlow](https://siliconflow.cn) 上的 BGE-M3）在任何用量下都免费。默认 LLM（DeepSeek-V4-Flash）按 token 计费（输入 $0.14/1M、输出 $0.28/1M）。一份典型日报——几十篇文章全文送进去再加分析输出——通常不到人民币一毛钱。OpenAI 兼容协议意味着你可以切换其他模型。
- **数据主权在你手里。** 你的 intent 和匹配历史存在本地 Qdrant，不经过任何第三方。默认 embedder 和 LLM 走云端 API（SiliconFlow / DeepSeek）以便快速起步，但两个后端都是 ABC 抽象  —— 接入本地模型（Ollama / mlx-lm）即可做到全程数据不出机器。
- **Cron 或 Event。** 每个 intent 自定节奏：固定时间（*"工作日 09:00 Asia/Shanghai"*）或者事件模式（*"关注的事情有进展就推送"*）。
- **处处可插拔。** Source / channel / embedder / LLM 全部是 ABC 接缝。Telegram / Discord / Slack 通道、本地 LLM (mlx-lm / Ollama)、Reddit / HN / Mastodon 源都是后 1.0 工作已经搭好的脚手架。
- **Agent友好型设计。** AI agent 一键部署、Skills集成、专为agent调用的设计的同步 fire endpoint。详见 [给 AI agent 用](#给-ai-agent-用)。

## "反向 RAG" 是怎么工作的

> *Attention is all you need.* —— Vaswani 等，2017
>
> *AI 就是你的注意力。* —— sembr

传统 RAG：用户输入 query → 应用检索匹配文档 → LLM 回答。

**反向 RAG (sembr)**：用户定义 intent → sembr 把 intent 向量化一次 → 每条新文章过一遍所有 intent 向量 → 命中的被 LLM 总结然后推送。

翻转很小，含义很大。query 变成了一等公民 —— 你可以命名它、编辑它、给它排程、给它做版本管理。retrieval 变成了一个长时间运行的任务，不再是请求-响应。*"答案好不好"* 变成了 *"sembr 最近 10 次告诉我的东西，跟我关心的事有多相关"*。

<p align="center">
  <img src="assets/screenshots/intents.jpeg" alt="sembr Intents tab —— 5 个用自然中文写的实际 intent，各自带 cron 排程 / 相似度阈值 / 语言 / 标签" width="900">
  <br>
  <sub>真实部署的 5 个 intent。每条都是自然语言 brief；cron 预设 + 阈值 + 标签完整定义 matcher 行为。实时日报：<a href="https://panel.peakstone-labs.com/#news">panel.peakstone-labs.com</a>。</sub>
</p>

→ 完整架构说明：[docs/architecture.md](docs/architecture.md)

## 快速开始

**机器上有 Agent / 龙虾？** 直接看下面 [给 AI agent 用](#给-ai-agent-用) —— 一键部署 + 部署后用的 Agent Skills bundle 一起讲。

**手动装**（下面这套，约 15 分钟）。需要 Docker + Docker Compose。第一次跑会拉 Qdrant + RSSHub 然后构建 API 镜像（Python 3.12 base + Docker CLI + pip 依赖）—— **总网络下载约 1 GB，家庭网速 10–15 分钟**。embedder probe 通之前 `/health` 返回 `503`。

```bash
git clone https://github.com/Peakstone-Labs/sembr.git
cd sembr
cp .env.example .env                 # 1. 拷一份配置
# 编辑 .env，把 EMBEDDER_API_KEY 改成你在 https://siliconflow.cn 申请的免费 key
docker compose up --build            # 2. 起来

# 另一个终端，1–2 分钟后：
curl -i http://localhost:8000/health         # embedder probe 通了就 200
open http://localhost:8000/dashboard          # 浏览器打开 web UI
```

开箱即用：53 条预置源（RSS / NewsAPI / Twitter，中英混合）、监控 dashboard、可用的 `/intents` API。建你的第一个 intent：

```bash
curl -X POST http://localhost:8000/intents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "fed-emerging-markets",
    "text": "美联储政策对新兴市场货币和资本流动的影响",
    "timezone": "Asia/Shanghai",
    "schedule": {"mode": "cron", "preset": "daily", "hour": 9, "minute": 0},
    "channels": [{"type": "email", "to": ["you@example.com"]}]
  }'
```

到点了 digest 就发。完事。

→ 一步一步走：[docs/getting-started.md](docs/getting-started.md)
→ 准备把 sembr 挂在公网 IP 上？先读 [docs/deployment/public.md](docs/deployment/public.md) —— TL;DR 保持默认 `127.0.0.1` 绑定，前面套一层带 TLS 的反向代理，`DASHBOARD_TOKEN` 必须设强一点。

## 给 AI agent 用

sembr 从设计上就是给 AI coding agent **部署**、**调用**、**接进编排栈**用的。仓库里配了三块脚手架：

### 1. 一键部署

机器上有 AI coding agent 且能跑 shell（Claude Code / Cursor / Cline / Aider / Continue / Roo / OpenClaw / Hermes / ……）—— 直接把下面这一句丢给它：

> 读 https://github.com/Peakstone-Labs/sembr/blob/main/agent/INSTALL.md 然后照着帮我把 sembr 装到这台机器上。

[`agent/INSTALL.md`](agent/INSTALL.md) 是 6 个 phase 的剧本：硬件自检 → Docker 安装 → 拉仓库 → 校验 key → 选访问模式（localhost / LAN / 公网）→ 起 stack → 第一次 `/health` 来回。镜像拉取后台跑、你的 API key 前台问，并行起来 wall-clock 约 15 分钟，其中约 10 分钟你不用看着。

选了公网部署的话，agent 会拐进 [`agent/PUBLIC_INSTALL.md`](agent/PUBLIC_INSTALL.md) —— DNS 检查、side service 端口必锁（qdrant/rsshub）、Caddy / nginx+certbot / Cloudflare Tunnel / trycloudflare 任选、ufw、docker.sock 取舍 —— 然后回到 Phase 5 起 stack + 外部验证。

### 2. 部署后驱动 sembr 的 skill bundle

sembr 起来之后，[`agent/sembr/`](agent/sembr/) 是配套的 [Agent Skills](https://agentskills.io) bundle，教任何 agent 怎么调 sembr 的 HTTP API：

| 文件 | 内容 |
| --- | --- |
| `SKILL.md` | 认证模型、fire 端点决策表、护栏 |
| `references/endpoints.md` | 31 个端点完整表（feeds / intents / fire / external-fire / settings / prompts / translate） |
| `references/schemas.md` | `IntentCreate` / `FeedCreate` / `ExternalFireRequest` body 形状，含 cron/event 鉴别字段联合体和 channel 鉴别器 |
| `references/recipes.md` | 可直接复制的 curl + Python `httpx` 工作流 |
| `references/errors.md` | 状态码表与 scrub-after 错误约定 |

**Claude Code**：`cp -r agent/sembr ~/.claude/skills/sembr` 即可自动加载。**其他平台**：直接把 `agent/sembr/SKILL.md` 丢给 agent，或查阅你平台的 skill 加载文档。

### 3. 给 agent 调的 fire endpoint

`POST /api/external/intents/{id}/fire` 是给编排器（orchestrator）专用的诊断 endpoint：

- **同步** —— 命中文章 + LLM 总结在响应里直接返回，不用 poll，不用拿 `task_id`
- **不触发通知** —— intent 配置的 email 收件人不会被打扰；适合"这个 intent 现在会命中啥"这种试探用法
- **无状态写** —— 不动 `match_seen`，反复调幂等
- **单次覆写** —— `lookback_seconds`（`300`–`2_592_000`）、`threshold`（`0.20`–`0.95`，比 intent 创建时的 `0.60`–`0.95` 还宽，方便诊断时往低扫）、`feed_ids`（子集或 `null` 表示全部）

接进任意编排器（Hermes / OpenClaw / LangGraph / 自己撸的），让它自己决定什么时候看一眼世界。响应形状、错误约定、限流（1 次/intent/60 秒）、cron-mode-only 约束都在 [`agent/sembr/references/endpoints.md`](agent/sembr/references/endpoints.md) 里。

## 盒子里都有什么

**53 条预置源，分三种来源类型** —— 都是挑过的"有正文长内容"或"标题即事实"的源：

| 来源类型 | 预置 | 例子 |
| --- | --- | --- |
| RSS feeds | 22 | The Guardian、SCMP、NPR、Washington Post、Bloomberg Markets、华尔街见闻、第一财经、36氪、虎嗅、财联社电报、澎湃、国家统计局、Nature ×3、HelloGitHub |
| Twitter | 1 | Elon Musk —— 自行加 user / 关键词搜索，需要 `TWITTER_AUTH_TOKEN` cookie |
| [NewsAPI.ai](https://newsapi.ai) 聚合 | 30 | Reuters、BBC、NYT、WSJ、FT、Economist、Bloomberg、The Atlantic、NPR、TechCrunch、Wired、Ars Technica、Vox、…… |

需要 JS 渲染的 RSS 路由（多数中文源、Twitter）走内置 **[RSSHub](https://rsshub.app)** sidecar，开箱即用。NewsAPI.ai 注册送的免费 token 大约够用一个月正常轮询；去 [newsapi.ai](https://newsapi.ai) 申请一个，填进 `.env` 即可。完整每条源的列表见 [docs/getting-started.md](docs/getting-started.md)。

<p align="center">
  <img src="assets/screenshots/feeds.jpeg" alt="sembr Feeds tab —— Reuters 展开显示真实文章标题 + URL，下方还有 70 条 feeds 中的其余" width="900">
  <br>
  <sub>Feeds 页。每行是一个活跃源；展开就能看到最近抓到的文章 + 源 URL + 时间戳。</sub>
</p>

- **BGE-M3 embedding**，跑在 SiliconFlow 免费档，也可以指向任意 OpenAI 兼容的 `/v1/embeddings`
- **[Qdrant](https://qdrant.tech) 向量库**，scalar int8 量化（1000 万条向量约占 600 MB RAM）
- **LLM 总结**，OpenAI 兼容的 `/v1/chat/completions` —— 默认走 SiliconFlow 上的 DeepSeek-V4-Flash
- **Email 推送**（SMTP，multipart/related，每个 intent 自己的时区，每篇文章带 matcher 分数徽章）
- **监控 dashboard**：每个 feed 的健康度、embedder 延迟、各容器 CPU / 内存 / uptime、Qdrant 文章库按日期 / 源 / 标题筛、log SSE、一键重启
- **运行时 settings 编辑器** —— 写宿主机 `.env` 然后原地重建受影响的容器，全程在 UI 里搞定
- **自定义 prompt 模板** —— system + instruction 两种，落盘前严格校验占位符，dashboard 提供 CRUD

→ 模块细节：[docs/modules/](docs/modules/index.md)

## 配置

`pydantic-settings`，四级优先（高的覆盖低的）：

1. Shell 环境变量
2. `.env` 文件（项目根）
3. `sembr.yaml`（项目根）
4. 内置默认值

敏感值（`EMBEDDER_API_KEY` / `LLM_API_KEY` / `DASHBOARD_TOKEN` / SMTP 凭据）放环境变量或者权限收紧的 `.env`，**别**提交进代码库。完整配置项见 [docs/configuration.md](docs/configuration.md)。

> ⚠️ **只要 host 能被 `localhost` 之外访问到，就必须设 `DASHBOARD_TOKEN`。** 不设的话 `/api/dashboard/*` 和 settings 编辑器全是无认证的。Settings 编辑器还会 bind-mount 宿主机 docker socket 才能重建容器 —— 这是单租户场景下有意的取舍（和 Watchtower / Portainer 一样）；任何拿到 API 访问的人都等于在 host 上拿到 docker root。多租户主机上别这么跑。完整加固清单见 [docs/deployment/public.md](docs/deployment/public.md)。

<p align="center">
  <img src="assets/screenshots/settings.jpeg" alt="sembr Settings 页 —— Embedder / LLM / NewsAPI / RSSHub / Email / Dashboard / Maintenance 等分组，LLM 组展开可见浏览器里改 .env 的 inline 文档" width="900">
  <br>
  <sub>Settings 页。浏览器里直接改宿主机 <code>.env</code>；secret 字段自动 mask；保存前 dry-run 校验，然后 <code>RestartController</code> 原地重建受影响的容器。</sub>
</p>

## 技术栈

Python 3.12 · FastAPI 0.115 · Pydantic v2 · APScheduler 3.11 · aiosqlite (WAL) · Qdrant 1.17 · httpx · BGE-M3 · DeepSeek-V4-Flash · Apache-2.0

**4 GB 内存就能跑舒服**（homelab / Mac mini / NAS / $10 VPS）—— 默认 53 源工作负载下三个容器加起来约 1 GB 实测。如果你跑到几百万条向量量级，把 `qdrant.mem_limit` 调到 4G+。

## 状态

**v1.0** —— 首个稳定版本。已经发布的能力：RSS 摄入、BGE-M3 embedding、Qdrant 双 collection、intent CRUD（cron + event）、LLM 总结的 email digest、监控 dashboard、运行时 settings 编辑器、公网部署加固指南。

**后 1.0：** Telegram / Discord / Slack 通道、本地 LLM 后端（mlx-lm / Ollama）、Reddit / HN / Mastodon 源插件、entry-points 插件发现、通知重试 / DLQ、多 worker 部署。

→ 版本策略和 changelog：[CHANGELOG.md](CHANGELOG.md)

## 那些"差不多"的东西，以及 sembr 为什么存在

市面上最接近的几样：

- **Feedly Pro+ "AI Feeds"**（约 $99 / 年） —— 最近的语义竞品。支持 15 种语言，但非英文文章的翻译被截到 ~1,600 字符，你的关注清单存在 Feedly 服务器上，AI 这一层还被门槛卡在中高档套餐之上。
- **Inoreader Pro**（约 $90 / 年） —— 规则 + 关键词过滤 + 月度 token 预算的 AI 总结。没有"对常驻 intent 做向量匹配"这一层。
- **Brand24 / Mention**（$199+ / 月） —— 企业级提及监控，关键词驱动，纯托管，按分析师人头收费。
- **Bloomberg Terminal**（约 $32,000 / 年 / 席位） —— 机构桌面的金标准；对长尾用户不相干。
- **FreshRSS / miniflux** —— 你可能已经在跑的自部署 RSS 阅读器。没有语义匹配、没有 LLM 总结、没有 intent 概念。
- **Google Alerts** —— 免费，但只能关键词，并且中文一直不太行。

**DIY 派路径** —— n8n / Huginn + LangChain + 向量库 + 自己的调度器 —— 技术上当然能拼。你要自己装 5+ 个组件，并且独自承担源解析、embedding 限流、去重、prompt 管理、通知可靠性这一长串维护成本。sembr 是这套栈的开箱即用版。

如果你是有预算的机构，跑 Bloomberg / Brand24。如果你不在意托管、关注清单也不敏感，Feedly Pro+ 已经挺好。sembr 想覆盖的是这样一群人：(a) 想用自然语言写关注 brief，(b) 想在中英混合源上做语义匹配，(c) 想按自己定的节奏拿到 LLM 总结的 digest，(d) 想付接近 $0、数据全在自己手里。**这四件事的交集，目前我们没找到第二家在做。**

### 跟 Perplexity 啥区别？自己写脚本 wrap 它的 API 不行吗？

Perplexity 是"先搜后总结"：它向搜索引擎发 query（关键词排名），把 top 结果用 LLM 包一层解释。sembr 是反过来的 —— 你预先存好语义意图，向量引擎在你指定的源上持续扫描匹配。

| | Perplexity | sembr |
| --- | --- | --- |
| 模式 | **拉** —— 你问，它答 | **推** —— 你定义一次，它持续盯 |
| 检索层 | 搜索引擎 + 关键词排名 | 预存意图向量匹配（BGE-M3） |
| 源可控性 | 看搜索引擎索引到什么 | 看你给 sembr 配什么 —— RSS / NewsAPI / Twitter / 自定义 |
| 语言 | 单 query 单语言 | 跨语言混合（一个 intent 同时能命中中英文） |
| 成本结构 | **O(查询)** —— 每次扫描都花钱 | **O(命中)** —— 扫描免费，只有命中才调 LLM |
| 关注清单 | 每次查询发给 Perplexity | 向量留在你本地 Qdrant |

**"那我自己 wrap 它的 API + cron 不就行？"** 一两个低频主题可以。但有三道结构性差距绕不过去：

1. **成本** —— 每次 ~$0.005–0.02 vs sembr "命中之前免费"。10 个意图 × 一天扫 24 次 × 365 天 = 8.76 万次 API 调用，账单会很难看。
2. **匹配质量** —— 你每次都要手撸 search query，而不是写一句自然语言意图让 BGE-M3 一次向量化永久使用。*"新兴市场货币传染"* 在关键词排名里搜不到 *"土耳其里拉跳水，市场押注美联储再加息"*；语义向量能。
3. **关注清单泄露** —— 每次轮询都把"你正在监控什么"发给第三方。*你监控什么本身就是信号* —— sembr 让这件事留在你自己的硬件上。

## 是谁做的

[Peakstone Labs](https://github.com/Peakstone-Labs) —— AI-native 量化研究。sembr 起源于内部 alpha 研究流水线的新闻侧；把它开出来，是因为同样在盯这个世界的人，远比我们多。

有想法 / 找到 bug / 想要某个 source 或 channel 插件：[Discussions](https://github.com/Peakstone-Labs/sembr/discussions) 聊想法和提问，[Issues](https://github.com/Peakstone-Labs/sembr/issues) 报 bug 和具体 feature request，[SECURITY.md](SECURITY.md) 报安全漏洞。欢迎 PR —— 看 [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)。

## License

[Apache-2.0](LICENSE)。© 2025–2026 Peakstone Labs 和 sembr 贡献者。
