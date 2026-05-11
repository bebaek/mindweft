FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEX_HOME=/home/codex/.codex \
    PATH="/home/codex/.local/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
    && npm install -g @openai/codex \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY local_agent_wrapper ./local_agent_wrapper
COPY codex_agent_wrapper ./codex_agent_wrapper
RUN python3 -m pip install --break-system-packages --no-cache-dir .

RUN groupadd --system codex \
    && useradd --system --gid codex --create-home codex \
    && mkdir -p /home/codex/.codex \
    && chown -R codex:codex /home/codex /app

USER codex

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/health').read()"

CMD ["uvicorn", "local_agent_wrapper.app:app", "--host", "0.0.0.0", "--port", "8010"]
