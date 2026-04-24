FROM python:3.14-slim

# System tools: git (for gh), curl/gnupg for apt key, then gh CLI itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git \
  && install -d -m 0755 /etc/apt/keyrings \
  && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
  && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
  && apt-get update && apt-get install -y --no-install-recommends gh \
  && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install Python dependencies before copying source so layer is cached.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# askcc CLI is called as a subprocess by AskccRunTool; install to system Python
# so it is available on PATH.  Confirm the package name with the askcc maintainer
# before shipping (open question from issue #66).
RUN uv pip install --system askcc-cli

COPY marvin/ marvin/
COPY entrypoint.py ./

ENTRYPOINT ["uv", "run", "python", "entrypoint.py"]
