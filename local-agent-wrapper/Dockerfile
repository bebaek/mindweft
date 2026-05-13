FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent \
    CODEX_HOME=/home/agent/.codex \
    XDG_CONFIG_HOME=/home/agent/.config \
    XDG_DATA_HOME=/home/agent/.local/share \
    XDG_STATE_HOME=/home/agent/.local/state \
    PATH="/home/agent/.local/bin:${PATH}"

ARG INSTALL_CODEX=false
ARG INSTALL_PI=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
    && npm install -g opencode-ai \
    && if [ "$INSTALL_CODEX" = "true" ]; then npm install -g @openai/codex; fi \
    && if [ "$INSTALL_PI" = "true" ]; then npm install -g @earendil-works/pi-coding-agent; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY local_agent_wrapper ./local_agent_wrapper
COPY codex_agent_wrapper ./codex_agent_wrapper
RUN python3 -m pip install --break-system-packages --no-cache-dir .

RUN groupadd --system agent \
    && useradd --system --gid agent --home-dir /home/agent --no-create-home agent \
    && mkdir -p /home/agent/.codex /home/agent/.pi/agent /home/agent/.config/opencode /home/agent/.local/share/opencode /home/agent/.local/state/opencode \
    && chown -R agent:agent /home/agent /app

USER agent

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/health').read()"

CMD ["uvicorn", "local_agent_wrapper.app:app", "--host", "0.0.0.0", "--port", "8010"]
