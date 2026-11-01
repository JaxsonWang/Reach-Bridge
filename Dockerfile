FROM ghcr.io/astral-sh/uv:0.9.3 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /uvx /usr/local/bin/
ARG AGENT_REACH_VERSION=1.6.1
LABEL org.opencontainers.image.source="https://github.com/JaxsonWang/Reach-Bridge"
LABEL io.reach-bridge.agent-reach-version="${AGENT_REACH_VERSION}"
ENV UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/opt/uv-bin \
    UV_PYTHON_DOWNLOADS=never HOME=/data/home \
    PATH="/app/.venv/bin:/opt/uv-bin:${PATH}" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && uv tool install --python /usr/local/bin/python \
      "https://github.com/Panniantong/Agent-Reach/releases/download/v${AGENT_REACH_VERSION}/agent-reach.zip" \
    && mkdir -p /data/home && chown 1000:1000 /data/home
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --python /usr/local/bin/python
COPY config ./config
USER 1000:1000
EXPOSE 8080
CMD ["reach-bridge"]
