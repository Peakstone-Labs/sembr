# Contributing

## Development setup

### Requirements

- Python 3.12.x
- Docker + Docker Compose (for E2E testing on macOS / Linux)

### Install dev dependencies

```bash
pip install uv
uv sync --extra dev
```

### Run static checks (any platform)

```bash
uv run python -m py_compile sembr/**/*.py
uv run python -c "import sembr, sembr.api, sembr.collector, sembr.embedder, sembr.vector_store, sembr.matcher, sembr.summarizer, sembr.notifier, sembr.db, sembr.dashboard, sembr.logbus; print('ok')"
uv run pytest tests/ -v
```

The full test suite runs under `pytest-asyncio`. No live Qdrant or SQLite required — the tests stub the network/IO surfaces.

### Lint and format

```bash
uv run ruff check .
uv run ruff format .
```

## Project structure

See [Architecture](architecture.md) for the data flow and design decisions, and [Modules](modules/index.md) for per-module interface contracts. Each module has its own `docs/modules/<name>.md` documenting upstream / downstream / known constraints — read it before changing the corresponding code.

## Adding a new RSS-style source

1. Subclass `sembr.collector.base.BaseSource`. Implement `fetch(since)` returning `tuple[int, int, list[RawArticle]]` (items_seen, items_new, parsed) and `config_schema()` returning a JSON Schema dict
2. Register in `SOURCE_REGISTRY` in `sembr.collector.scheduler` — the dashboard's create-feed form reads from this dict, so the new source appears immediately
3. Add a unit test under `tests/collector/`

A `pyproject.toml` entry-points discovery layer is on the post-1.0 roadmap; the hardcoded registry is the contract today.

## Adding a new notification channel

1. Define a Pydantic config model with a unique `type: Literal["x"]` discriminator and any per-channel fields (recipient list, webhook URL, etc.)
2. Add the new config to the `Intent.channels` discriminated union in `sembr.models`
3. Subclass `sembr.notifier.base.BaseChannel` — note that `BaseChannel` is a **marker ABC with no abstract methods** because per-channel `send()` signatures legitimately diverge; define whatever shape your channel needs
4. Add an `isinstance(ch, XConfig)` arm to the dispatcher in `sembr.main` that calls into your channel
5. Wrap your top-level `send()` in `try / except` and never raise — a delivery failure must not abort the remaining channels in the same tick or crash the summarizer's tick loop. The dispatcher independently logs and swallows; this is intentional defense-in-depth

## Commit style

- `feat:` new feature
- `fix:` bug fix
- `chore:` tooling, deps, CI
- `docs:` documentation only
- `test:` test-only changes

## License

By contributing you agree that your code will be licensed under Apache-2.0.
