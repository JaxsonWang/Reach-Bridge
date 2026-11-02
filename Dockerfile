FROM ghcr.io/astral-sh/uv:0.9.3 AS uv
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=node /usr/local/ /opt/node/
ARG AGENT_REACH_VERSION=1.6.1
LABEL org.opencontainers.image.source="https://github.com/JaxsonWang/Reach-Bridge"
LABEL io.reach-bridge.agent-reach-version="${AGENT_REACH_VERSION}"
ENV UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/opt/uv-bin \
    UV_PYTHON_DOWNLOADS=never HOME=/data/home \
    PATH="/app/.venv/bin:/opt/uv-bin:/opt/node/bin:${PATH}" \
    REACH_BRIDGE_BROWSER_EXECUTABLE=/app/.venv/bin/reach-bridge-chromium \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git chromium chromium-sandbox xvfb x11-utils dbus-x11 \
      ffmpeg fonts-noto-cjk fonts-liberation libnss3-tools openssl \
    && rm -rf /var/lib/apt/lists/* \
    && uv tool install --python /usr/local/bin/python \
      "https://github.com/Panniantong/Agent-Reach/releases/download/v${AGENT_REACH_VERSION}/agent-reach.zip" \
    && mkdir -p /data/home /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && chown 1000:1000 /data/home
COPY docker/patches/agent-reach-1.6.1-cdp-opener.patch /tmp/reach-bridge-cdp.patch
RUN if [ "$AGENT_REACH_VERSION" = "1.6.1" ]; then \
      cd /opt/uv-tools/agent-reach/lib/python3.12/site-packages \
      && test "$(/opt/uv-tools/agent-reach/bin/python -c 'import agent_reach; print(agent_reach.__version__)')" = "1.6.1" \
      && git apply --check /tmp/reach-bridge-cdp.patch \
      && git apply /tmp/reach-bridge-cdp.patch; \
    else echo "CDP opener 补丁仅适用于 Agent-Reach 1.6.1，其他版本保持原包"; fi \
    && rm /tmp/reach-bridge-cdp.patch
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --python /usr/local/bin/python
COPY config ./config
COPY docker/entrypoint.py /usr/local/bin/reach-bridge-entrypoint.py
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --retries=6 --start-period=10s CMD ["reach-bridge-healthcheck"]
ENTRYPOINT ["python", "/usr/local/bin/reach-bridge-entrypoint.py"]
CMD ["reach-bridge"]
