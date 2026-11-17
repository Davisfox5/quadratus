FROM python:3.12-slim
# The Grok binary is downloaded below and checked against this digest. No
# digest for it is recorded in this repository, so there is no default and
# the build stops here without one:
#   docker build --build-arg GROK_SHA256=<sha256> -t <tag> tools/acceptance
ARG GROK_SHA256
RUN if ! printf '%s' "${GROK_SHA256}" | grep -Eq '^[0-9a-f]{64}$'; then echo "GROK_SHA256 is required: pass --build-arg GROK_SHA256=<the 64 lowercase hex characters of the sha256 of grok-1.0.30-linux-aarch64>" >&2; exit 1; fi
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm git curl ca-certificates ripgrep ffmpeg && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex@0.154.0 @anthropic-ai/claude-code@2.1.269
# Checked before it is installed, so a swapped download never lands on PATH.
RUN curl -fSL https://x.ai/cli/grok-1.0.30-linux-aarch64 -o /tmp/grok-download && echo "${GROK_SHA256}  /tmp/grok-download" | sha256sum -c - && install -m 755 /tmp/grok-download /usr/local/bin/grok && rm /tmp/grok-download
# Pinned to the versions recorded for the image the blind acceptance ran in;
# constraints.txt holds that whole recorded set and says where it came from.
COPY constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir -c /tmp/constraints.txt flask==3.1.3 pytest==9.1.1 python-dotenv==1.2.3 colorama==0.4.6 anthropic==1.6.0 openai==3.14.0 gradio==6.27.0 playwright==1.62.0 jsonschema==4.26.0
RUN PLAYWRIGHT_BROWSERS_PATH=/opt/playwright python -m playwright install --with-deps chromium && chmod -R a+rX /opt/playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN npm install -g node@24.15.0
RUN node --version && codex --version && claude --version && grok --version
RUN pip install --no-cache-dir -c /tmp/constraints.txt openpyxl==3.1.5
RUN python -c "import importlib.metadata, subprocess; subprocess.run(['npm', 'install', '-g', '--prefix', '/opt/browser-tools', 'playwright@' + importlib.metadata.version('playwright')], check=True)"
ENV NODE_PATH=/opt/browser-tools/lib/node_modules
