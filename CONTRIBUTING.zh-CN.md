# 参与 sembr 贡献

[English version](./CONTRIBUTING.md)

感谢你对 sembr 感兴趣！本文介绍开发环境搭建、代码风格与提交规范，以及 PR 流程。

参与本项目即表示你同意遵守 [行为准则](./CODE_OF_CONDUCT.zh-CN.md)。

## 参与方式

- **提交 bug** —— 用 *Bug report* 表单开 Issue
- **提议新特性** —— 用 *Feature request* 表单开 Issue
- **提问 / 分享使用场景** —— 请用 GitHub Discussions（不要开 Issue）
- **修 bug 或实现新特性** —— 读完本文后提 PR
- **改进文档** —— 错别字到全篇重写都欢迎
- **报告安全漏洞** —— 见 [SECURITY.md](./SECURITY.md)；**不要**公开开 Issue

## 开发环境搭建

### 前置

- Python 3.12 或更新
- [uv](https://docs.astral.sh/uv/)（依赖与虚拟环境管理）
- Git

### 克隆与安装

```bash
git clone https://github.com/Peakstone-Labs/sembr.git
cd sembr
uv sync --extra dev
```

`uv sync` 会创建 `.venv/`，按 `uv.lock` 安装运行时 + 开发依赖。修改 `pyproject.toml` 后必须把 `uv.lock` 一起提交。

### 跑测试

```bash
uv run pytest tests/ -v
```

`main` 分支上目前有少量已知失败（`test_restart_endpoint`、`test_newsapi_fire_endpoint`），CI 已容错，待后续修复。除此之外本地红 / CI 绿的多半是环境问题，欢迎开 Issue。

### 启动本地 dev server

Docker Compose 用法见 [README.md](./README.md) 的 Quickstart。本地 Python 跑：

```bash
uv run uvicorn sembr.app:app --reload
```

## 代码风格

### 格式化（必需，CI 严格门禁）

```bash
uvx ruff format --check .
```

CI 在这条命令非零退出时拒绝 PR。推送前跑 `uvx ruff format .`（去掉 `--check`）即可自动修复。

配置在 `pyproject.toml` 的 `[tool.ruff]`：行宽 100，目标 Python 3.12。

### Lint（仅建议）

```bash
uvx ruff check .
```

Lint 提示（UP / I / F / SIM 等现代化建议）**不**作为 PR 门禁。欢迎单独 PR 做清理，但你的功能 PR 不需要去修无关 lint 警告。

### SPDX license header（必需，CI 严格门禁）

每个 `.py` 文件首行必须是：

```python
# SPDX-License-Identifier: Apache-2.0
```

CI 拒绝任何新增 `.py` 缺这一行的 PR。新增模块直接加上即可。

### 其他防回流 grep 门禁（CI 严格）

CI 还会拒绝引入以下内容的 PR：

- 引用私有的 `sembr-dev-docs` 仓库或 `design.md` 路径
- 内部编号 `Dxx` / `Rxx` / `DDxx`（保留了 `noqa: D[0-9]` 例外，给 pydocstyle 规则码合法使用）

不小心碰到，CI 信息会告诉你具体文件。

## 提交信息规范

我们使用 [Conventional Commits](https://www.conventionalcommits.org/) 的轻量子集。下面五种 type 覆盖绝大多数改动，优先用它们：

| Type | 用于 |
| --- | --- |
| `feat` | 新增用户可见的功能 |
| `fix` | 修 bug |
| `docs` | 仅修改文档 |
| `refactor` | 不改行为、不增功能、不修 bug 的代码改动 |
| `chore` | 工具、构建、依赖、CI、仓库杂事 |

需要更精确时，也接受以下扩展 type：

| Type | 用于 |
| --- | --- |
| `perf` | 性能优化，行为不变 |
| `test` | 仅新增或修复测试 |
| `build` | 构建系统、打包或运行时依赖（`pyproject.toml` / Dockerfile / `uv.lock`） |
| `ci` | 仅 CI / GitHub Actions / workflow 改动 |
| `style` | 格式化、空白或纯样式调整（不动逻辑） |
| `revert` | 回滚先前 commit |

可选 **scope** 写在括号里，仅在改动明确限定在单个模块时使用：

```
<type>(<scope>): <祈使句概述，小写，不带句号>

<可选正文，解释 "为什么"，按 72 列换行>
```

例：

```
feat: add NewsAPI source adapter
fix(matcher): prevent duplicate intent firing on SSE reconnect
docs: clarify uv setup in README
perf(qdrant): switch to scalar int8 quantization for news collection
revert: "feat: experimental redis cache" (causes startup deadlock)
```

不兼容改动用 `type!:` 或 footer `BREAKING CHANGE:` 标识，这些会归入 [CHANGELOG.md](./CHANGELOG.md) 的大版本段：

```
feat!: rename DASHBOARD_TOKEN env var to SEMBR_API_TOKEN
```

## PR 流程

1. **Fork** 本仓库，从 `main` 拉特性分支
2. **修改代码**，遵循上面的风格规范
3. **本地跑** `uvx ruff format --check .` 与 `uv run pytest`
4. **推送**特性分支，对 `Peakstone-Labs/sembr:main` 提 PR
5. **填写 PR 模板**，包括 Contributor Acknowledgment 勾选项
6. **等 review** —— sembr 是单人副业项目，review 可能需要几天，特别是周末和节假日。一周后在 PR 礼貌 ping 一下没问题。

每个 PR 都会自动跑 CI。严格门禁（格式 / SPDX / 防回流 grep）必须过；建议性检查（lint、完整 pytest）仅作参考。

## 贡献的 License

sembr 使用 [Apache-2.0](./LICENSE)。提交 PR 即表示你确认：

- 代码由你本人编写（或你有权提交），且
- 你同意以 Apache-2.0 协议发布你的贡献

**无需单独签 CLA**。PR 模板的 Contributor Acknowledgment 勾选项即记录此同意，勾上即可。

`Contributor Acknowledgment` **不是** Developer Certificate of Origin (DCO)——你**不需要** `git commit -s`，也不需要在 commit 加 `Signed-off-by:`。

## 问题

文档不清楚的地方，请开 GitHub Discussion。也欢迎直接提 PR 改进本文件。
