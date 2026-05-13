# Contributing to sembr

[中文版 / Chinese version](./CONTRIBUTING.zh-CN.md)

Thanks for your interest in sembr! This document covers how to set up a development environment, our code style and commit conventions, and the pull-request flow.

By participating in this project you agree to abide by our [Code of Conduct](./CODE_OF_CONDUCT.md).

## Ways to contribute

- **Report a bug** — open an issue using the *Bug report* form
- **Suggest a feature** — open an issue using the *Feature request* form
- **Ask a question / share a use case** — please use GitHub Discussions (not Issues)
- **Fix a bug or implement a feature** — read this guide, then open a PR
- **Improve docs** — typo fixes through full-page rewrites are all welcome
- **Report a security issue** — see [SECURITY.md](./SECURITY.md); do **not** open a public issue

## Development setup

### Prerequisites

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) for dependency and virtual-env management
- Git

### Clone and install

```bash
git clone https://github.com/Peakstone-Labs/sembr.git
cd sembr
uv sync --extra dev
```

`uv sync` creates `.venv/`, installs runtime + dev dependencies, and resolves against `uv.lock`. Always commit `uv.lock` together with any `pyproject.toml` change.

### Run tests

```bash
uv run pytest tests/ -v
```

A few tests are currently expected to fail on `main` (`test_restart_endpoint`, `test_newsapi_fire_endpoint`) — these are tracked and CI tolerates them. Anything else that fails locally and is green in CI is likely environment-related; please open an issue.

### Run the dev server

See the `Quickstart` section of [README.md](./README.md) for the Docker Compose flow. For local Python iteration:

```bash
uv run uvicorn sembr.app:app --reload
```

## Code style

### Formatting (required, CI strict)

```bash
uvx ruff format --check .
```

CI rejects any PR where this command exits non-zero. Run `uvx ruff format .` (no `--check`) to auto-fix before pushing.

Config lives in `pyproject.toml` under `[tool.ruff]`: line length 100, target Python 3.12.

### Linting (advisory)

```bash
uvx ruff check .
```

Lint findings (modernization hints like UP / I / F / SIM rules) are **not** a PR gate. Cleanups are welcome as separate PRs but you don't need to fix unrelated lint warnings in your feature PR.

### SPDX license headers (required, CI strict)

Every `.py` file's first line must be:

```python
# SPDX-License-Identifier: Apache-2.0
```

CI rejects any new `.py` missing this header. If you add a new module, just include the line.

### Other defensive grep gates (CI strict)

CI also rejects PRs that introduce:

- References to the private `sembr-dev-docs` repo or `design.md` paths
- Internal-numbering identifiers matching `Dxx` / `Rxx` / `DDxx` (a `noqa: D[0-9]` carve-out exists for legitimate pydocstyle rule codes)

If you trip these by accident, the CI message will tell you the offending file.

## Commit messages

We follow a lightweight subset of [Conventional Commits](https://www.conventionalcommits.org/). The five types below cover almost every change — reach for one of these first:

| Type | When |
| --- | --- |
| `feat` | New user-facing functionality |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `chore` | Tooling, build, deps, CI, repo plumbing |

Additional types are also accepted when they fit better than the five above:

| Type | When |
| --- | --- |
| `perf` | Performance improvement with no behavior change |
| `test` | Add or fix tests only |
| `build` | Build system, packaging, or runtime-dep changes (`pyproject.toml`, Dockerfile, `uv.lock`) |
| `ci` | CI / GitHub Actions / workflow changes only |
| `style` | Formatting, whitespace, or pure-style edits (no logic change) |
| `revert` | Reverts a previous commit |

Optional **scope** in parentheses is allowed — use it when the change is clearly confined to one module:

```
<type>(<scope>): <imperative summary, lower case, no trailing period>

<optional body explaining "why" — wrap at 72 cols>
```

Examples:

```
feat: add NewsAPI source adapter
fix(matcher): prevent duplicate intent firing on SSE reconnect
docs: clarify uv setup in README
perf(qdrant): switch to scalar int8 quantization for news collection
revert: "feat: experimental redis cache" (causes startup deadlock)
```

Use `!` after the type or a `BREAKING CHANGE:` footer for backwards-incompatible changes — these end up in the major-version section of [CHANGELOG.md](./CHANGELOG.md):

```
feat!: rename DASHBOARD_TOKEN env var to SEMBR_API_TOKEN
```

## Pull-request flow

1. **Fork** the repo and create a feature branch off `main`
2. **Make your changes** following the style rules above
3. **Run `uvx ruff format --check .` and `uv run pytest`** locally
4. **Push** your branch and open a PR against `Peakstone-Labs/sembr:main`
5. **Fill out the PR template** including the Contributor Acknowledgment checkboxes
6. **Wait for review** — sembr is maintained by one person as a side project, so reviews may take a few days, especially around weekends and holidays. A polite ping on the PR after a week is fine.

CI runs automatically on every PR. The strict gates (format, SPDX, defensive greps) must pass; advisory checks (lint, full pytest) are informational.

## License of your contribution

sembr is licensed under [Apache-2.0](./LICENSE). By submitting a pull request you confirm that:

- You wrote the code yourself (or have the right to submit it), and
- You agree to license your contribution under Apache-2.0

There is **no separate CLA to sign**. The PR template includes a Contributor Acknowledgment checkbox that records this agreement; checking it is sufficient.

The `Contributor Acknowledgment` is **not** a Developer Certificate of Origin (DCO) — you do **not** need to `git commit -s` or add a `Signed-off-by:` trailer.

## Questions?

If anything in this guide is unclear, please open a GitHub Discussion. Improvements to this document via PR are also very welcome.
