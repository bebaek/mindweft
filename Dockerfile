FROM node:22-bookworm-slim AS frontend-build

WORKDIR /web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web ./
RUN npm run build

FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.2 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY --from=frontend-build /web/dist ./app/static/console
COPY minigent_client ./minigent_client
COPY minigent_mcp ./minigent_mcp
COPY minigent_workspace ./minigent_workspace
RUN uv sync --frozen --no-dev
RUN python -c "import app.main, minigent_mcp, minigent_workspace"

RUN mkdir -p /data && chown -R app:app /app /data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
