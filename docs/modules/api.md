# api

> **Status**: pending review

FastAPI REST layer. Exposes CRUD endpoints for feeds and intents, fire-on-demand endpoints, settings read/write, and prompt template management. All routes under `/api/*` pass through the dashboard auth middleware when `DASHBOARD_TOKEN` is set.

Interactive API docs are auto-generated at **http://localhost:8000/docs** (Swagger UI) and **http://localhost:8000/redoc**.

<!-- Review and fill in this page before opening the module to contributors. -->
