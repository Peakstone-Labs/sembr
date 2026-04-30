FROM python:3.12

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY sembr/ ./sembr/
COPY scripts/ ./scripts/
# Dashboard bundle (optional). main.py mounts /dashboard only when web/static/
# exists, so the build succeeds even if a downstream consumer drops the dir.
COPY web/ ./web/

RUN mkdir -p /app/data

EXPOSE 8000

ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "sembr.main:app", "--host", "0.0.0.0", "--port", "8000"]
