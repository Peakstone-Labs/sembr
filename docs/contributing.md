# Contributing

## Development setup

### Requirements

- Python 3.12.x
- Docker + Docker Compose (for E2E testing, runs on Mac/Linux)

### Install dev dependencies

```bash
pip install uv
uv sync --extra dev
```

### Run static checks (Windows / any platform)

```bash
python -m py_compile sembr/**/*.py
python -c "import sembr, sembr.api, sembr.collector, sembr.embedder, sembr.vector_store, sembr.matcher, sembr.summarizer, sembr.notifier, sembr.db; print('ok')"
pytest tests/ -v
```

### Lint and format

```bash
ruff check .
ruff format .
```

## Project structure

See [Architecture](architecture.md) for the data flow and design decisions, and [Modules](modules/index.md) for per-module interface reference.

## Adding a new source

1. Subclass `sembr.collector.base.BaseSource`
2. Implement `fetch(since)`, `health()`, and `config_schema()`
3. Register via `pyproject.toml` entry point under `sembr.sources`

## Adding a new notification channel

1. Subclass `sembr.notifier.base.BaseChannel`
2. Implement `send()`, `health()`, and `split_message()`
3. Register via `pyproject.toml` entry point under `sembr.channels`

## Commit style

- `feat:` new feature
- `fix:` bug fix
- `chore:` tooling, deps, CI
- `docs:` documentation only

## License

By contributing you agree that your code will be licensed under Apache-2.0.
