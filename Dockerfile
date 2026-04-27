FROM python:3.14-slim

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

# Pin uv to a specific version for reproducible builds
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY marvin/ marvin/
COPY entrypoint.py ./

# askcc v0.2.8 pinned — not on PyPI, installed from GitHub archive
RUN uv pip install --system https://github.com/monkut/askcc-cli/archive/refs/tags/v0.2.8.tar.gz

ENTRYPOINT ["uv", "run", "python", "entrypoint.py"]
