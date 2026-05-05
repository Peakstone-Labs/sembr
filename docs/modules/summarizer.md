# summarizer

> **Status**: pending review

LLM summary generation. Groups matched articles by intent, renders a Jinja2 prompt template, calls the configured LLM backend (OpenAI-compatible API or local mlx-lm), and passes the result to the notifier. Supports per-intent custom system and instruction templates.

<!-- Review and fill in this page before opening the module to contributors. -->
