FROM python:3.12

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install Docker CLI + Compose plugin so the API container can drive RSSHub
# restarts via `docker compose up --force-recreate` against the host daemon
# mounted at /var/run/docker.sock.  Uses the official docker.com apt repo
# (not the Debian backport) per design.md Risk R1.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo \
       "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
       https://download.docker.com/linux/debian \
       $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY sembr/ ./sembr/
COPY scripts/ ./scripts/
# Dashboard bundle (optional). main.py mounts /dashboard only when web/static/
# exists, so the build succeeds even if a downstream consumer drops the dir.
COPY web/ ./web/
COPY prompts/ ./prompts/

RUN mkdir -p /app/data

EXPOSE 8000

ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "sembr.main:app", "--host", "0.0.0.0", "--port", "8000"]
