FROM node:22-slim

# Pinned on purpose, and to this version for a reason. A later CLI may make --bare the default for
# -p, and bare mode never reads CLAUDE_CODE_OAUTH_TOKEN. An earlier one does not know the models:
# on 2.1.263 the alias `opus` still meant Opus 5, and 2.1.280 has no Sonnet 5.5. 2.1.285 knows
# both 5.5 models, supports max effort, and still keeps --bare opt-in.
ARG CLAUDE_CODE_VERSION=2.1.285

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv git curl ca-certificates \
       chromium fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && npm cache clean --force

# Read by every claude process in the box, including sub-agents and any a worker starts from Bash.
# Root-owned, so the agent cannot edit it: only the three subscription models can run (a request
# for Fable is swapped for Opus 5.5), sub-agents inherit their parent's model, fast mode is off.
COPY managed-settings.json /etc/claude-code/managed-settings.json

# Created and owned before the volume is first mounted, so the named volume inherits the owner.
RUN useradd --create-home --shell /bin/bash agent \
    && mkdir -p /workspace \
    && chown agent:agent /workspace

USER agent
WORKDIR /workspace
# A browser for checking pages: the agents are not administrators, so they cannot apt-get one.
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    CHROME_PATH=/usr/bin/chromium \
    PATH=/opt/company/bin:$PATH

CMD ["sleep", "infinity"]
