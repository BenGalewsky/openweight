# Agent sandbox image (see PLAN.md, "Sandbox"): the toolchain PromptDB's steps need plus the
# pinned harness CLIs. Built by `openweight sandbox build`. The version ARGs below are the single
# source of the pins: sandbox.py reads them to name the image tag and to report harness versions.

ARG DOCKER_VERSION=29.8.2
ARG UV_VERSION=0.13.6
ARG CLAUDE_VERSION=2.1.312
ARG OPENCODE_VERSION=2.0.23
ARG MLFLOW_CLAUDE_CODE_VERSION=0.4.0
ARG PYTHON_VERSION=3.13

FROM docker:${DOCKER_VERSION}-cli AS docker-cli
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM node:22-trixie-slim

ARG CLAUDE_VERSION
ARG OPENCODE_VERSION
ARG MLFLOW_CLAUDE_CODE_VERSION
ARG PYTHON_VERSION
ARG AGENT_UID=1000

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
      ca-certificates curl git jq less mdbtools postgresql-client procps tini unzip \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI with the compose and buildx plugins; the daemon is the evaluation's dind container.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins

# uv, with the Python PromptDB asks for preinstalled where the agent user can add others.
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_PREFERENCE=only-managed
RUN uv python install "${PYTHON_VERSION}"

# OpenCode 2 is published as @opencode/cli (the opencode-ai package stops at 1.x).
# @mlflow/claude-code is the Claude Code tracing plugin (its Stop hook writes the trace); it is
# loaded with --plugin-dir from the global node_modules, so nothing is fetched from GitHub at run
# time, unlike `mlflow autolog claude`, which installs it from a marketplace. OpenCode has no
# tracing plugin here: @mlflow/opencode targets OpenCode 1's plugin API, which OpenCode 2 rejects,
# so the runner writes OpenCode traces on the host from the session exports.
RUN npm install --global --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@opencode/cli@${OPENCODE_VERSION}" \
      "@mlflow/claude-code@${MLFLOW_CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# The base image's `node` user would take the agent's uid.
RUN userdel --remove node \
    && useradd --create-home --uid "${AGENT_UID}" --shell /bin/bash agent \
    && chown -R agent:agent /opt/uv/python \
    && git config --system user.name openweight-agent \
    && git config --system user.email openweight-agent@localhost \
    && git config --system init.defaultBranch main \
    && git config --system safe.directory '*'

COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# The agent container shares the dind container's network namespace, where dockerd listens on
# loopback only.
ENV DOCKER_HOST=tcp://127.0.0.1:2375 \
    OPENWEIGHT_HARNESS_CONFIG=/harness_config \
    DISABLE_AUTOUPDATER=1 \
    OPENCODE_DISABLE_AUTOUPDATE=1

LABEL org.opencontainers.image.title="openweight-sandbox" \
      openweight.claude-version="${CLAUDE_VERSION}" \
      openweight.opencode-version="${OPENCODE_VERSION}"

USER agent
WORKDIR /workspace
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["sleep", "infinity"]
