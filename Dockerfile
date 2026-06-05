FROM python:3.12

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install Docker CLI + Compose plugin so the API container can drive RSSHub
# restarts via `docker compose up --force-recreate` against the host daemon
# mounted at /var/run/docker.sock.  Uses the official docker.com apt repo
# (not the Debian backport).
#
# The same layer also installs WeasyPrint's native dependencies (Pango/Cairo +
# fonts) so the optional PDF digest attachment can render. fonts-noto-cjk is
# included because sembr digests are frequently Chinese; without it WeasyPrint
# renders CJK text as tofu boxes. Merged into this single RUN to avoid an extra
# image layer.
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
       libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
       libffi-dev libcairo2 fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY sembr/ ./sembr/
# Dashboard bundle (optional). main.py mounts /dashboard only when web/static/
# exists, so the build succeeds even if a downstream consumer drops the dir.
COPY web/ ./web/
COPY prompts/ ./prompts/

RUN mkdir -p /app/data

EXPOSE 8000

ENV PATH="/app/.venv/bin:$PATH"
# --timeout-graceful-shutdown: after SIGTERM, uvicorn forcibly closes any
# still-open connections (e.g. SSE log streams) after this many seconds and
# proceeds to lifespan shutdown.  Without this, a persistent SSE client keeps
# uvicorn in "Waiting for connections to close" indefinitely, the lifespan
# finally block never runs, and _force_exit is never called.
# 5s is well inside the 8s lifespan_shutdown_timeout and the 10s docker SIGKILL.
CMD ["uvicorn", "sembr.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "5"]
