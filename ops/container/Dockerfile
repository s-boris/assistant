# A fresh container for your own assistant: Debian 12 + Python 3.13 (at /usr/local/bin/python3.13, the managed
# tools' shebang) + Node 24 + the Claude Code CLI + this repo, installed by ops/install.sh into the user's home.
# QUICKSTART.md § Container walks through it. Build from the repo root (a clone; the build copies it in):
#   docker build -f ops/container/Dockerfile -t my-assistant .
#   docker run -d --name my-assistant -v my-assistant-home:/home/bot my-assistant
#   docker exec -it -u bot my-assistant bash -l          # then: claude, /login (QUICKSTART step 9)
# The home is a volume: it keeps the login, profile, secrets and state across image rebuilds. Only the image's
# system layer (packages, /usr/local) is rebuilt. No account, token or personal value is baked into the image.
ARG NODE_IMAGE=node:24-bookworm-slim
FROM ${NODE_IMAGE} AS node

FROM python:3.13-slim-bookworm
ARG USER=bot
ARG UID=1000
ARG CLAUDE_CODE_VERSION=latest

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git curl ca-certificates cron procps tini less ffmpeg poppler-utils age jq \
 && rm -rf /var/lib/apt/lists/*

# Node from the official image (no apt repo, no curl | sh), then the Claude Code CLI.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && claude --version

# The bot user (no sudo: /usr/local/bin/python3.13 is already the image's interpreter). Its home is the volume.
RUN useradd --uid "${UID}" --create-home --shell /bin/bash "${USER}"
USER ${USER}
WORKDIR /home/${USER}
COPY --chown=${USER}:${USER} . /home/${USER}/opt/assistant
RUN mkdir -p workspace inbox \
 && sh opt/assistant/ops/install.sh \
 && sh opt/assistant/ops/install.sh --check

# cron runs as root (it switches to the user for the user's crontab); the user's session is started with
# `docker exec -u bot` (QUICKSTART steps 9-12). tini reaps the background jobs cron leaves.
USER root
COPY --chmod=755 ops/container/entrypoint.sh /usr/local/bin/assistant-entrypoint
ENV ASSISTANT_USER=${USER}
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/assistant-entrypoint"]
