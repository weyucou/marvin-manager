# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the Fargate one-shot agent worker.
# Builder installs only the `fargate` dependency-group + askcc into a venv;
# runtime copies that venv and adds the CLI tools the agent shells out to.

# ---------- Builder ----------
FROM python:3.14-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Pin uv to a specific version for reproducible builds
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv

WORKDIR /app

# Install only the `fargate` group's deps. --no-install-project skips building
# marvin itself; the source is copied into the runtime stage and imported via
# the script-directory entry on sys.path.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --only-group fargate --no-install-project

# askcc v0.2.8 pinned — installed into the same venv so it ships in /app/.venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python \
        https://github.com/monkut/askcc-cli/archive/refs/tags/v0.2.8.tar.gz

# ---------- Runtime ----------
FROM python:3.14-slim

# CLI tools the agent shells out to via marvin/tools/coding.py:
#   - gh   — GitHub operations
#   - git  — repository operations
# (aws, terraform are not bundled here; add if/when the agent's invocation
#  pattern requires them.)
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

WORKDIR /app

# Copy the prebuilt venv (fargate runtime deps + askcc) from builder
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

COPY marvin/ marvin/
COPY entrypoint.py ./

ENTRYPOINT ["python", "entrypoint.py"]
