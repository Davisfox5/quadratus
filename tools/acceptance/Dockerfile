FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm git curl ca-certificates ripgrep ffmpeg && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex@0.154.0 @anthropic-ai/claude-code@2.1.269
RUN curl -fSL https://x.ai/cli/grok-1.0.30-linux-aarch64 -o /usr/local/bin/grok && chmod 755 /usr/local/bin/grok
RUN pip install --no-cache-dir flask pytest python-dotenv colorama anthropic openai gradio playwright
RUN PLAYWRIGHT_BROWSERS_PATH=/opt/playwright python -m playwright install --with-deps chromium && chmod -R a+rX /opt/playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN npm install -g node@24.15.0
RUN node --version && codex --version && claude --version && grok --version
RUN pip install --no-cache-dir openpyxl==3.1.5
RUN python -c "import importlib.metadata, subprocess; subprocess.run(['npm', 'install', '-g', '--prefix', '/opt/browser-tools', 'playwright@' + importlib.metadata.version('playwright')], check=True)"
ENV NODE_PATH=/opt/browser-tools/lib/node_modules
