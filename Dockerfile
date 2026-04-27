FROM python:3.12

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/models

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev || uv sync --no-dev

COPY sembr/ ./sembr/

RUN mkdir -p /app/data /app/models

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "uvicorn", "sembr.main:app", "--host", "0.0.0.0", "--port", "8000"]
