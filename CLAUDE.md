# CLAUDE.md

This file provides guidance to AI agents (Claude Code and similar) when working with this repository. Keep it accurate and concise — agents read it on every session start.

Org-wide conventions (coding style, error handling, branching, testing, tool preferences) are defined in the system-level `~/.claude/CLAUDE.md` and apply automatically. Only include sections below that are **project-specific** or **override an org default**.

## Project Overview

wyc6k-task-runner is the agent execution layer of the WYC6k multi-tenant agent harness. It provides a stateless SQS worker (`marvin/`) that dequeues `TaskEnvelope` messages, pulls per-customer context (CLAUDE.md, SOPs, memories) from S3, runs an LLM agent through a tool-call loop, and returns results. The worker supports multiple LLM providers (Anthropic, Gemini, Ollama, vLLM) and four tool profiles (MINIMAL, CODING, MESSAGING, FULL). Task scheduling and customer identity are managed by a separate service (`wyc6k-task-manager`). For system-wide design, see [weyucou/wyc6k-spec](https://github.com/weyucou/wyc6k-spec).

## Key Files

| File | Purpose |
|------|---------|
| `marvin/models.py` | `AgentConfig`, `TaskEnvelope`, `LLMProvider`, `ToolProfile` — core Pydantic models |
| `marvin/worker.py` | SQS consumer loop and `main()` entry point (`python -m marvin`) |
| `marvin/__main__.py` | Module entry point — delegates to `worker.main()` |
| `marvin/runner.py` | `AgentRunner` — builds the LLM client, registers tools, runs the tool-call loop |
| `marvin/context.py` | `ContextBundleService` — `pull()` reads customer context from S3; `push_memory()` writes daily memory entries |
| `marvin/llm/factory.py` | `create_llm_client()`, `create_client_from_agent_config()` |
| `marvin/llm/` | LLM client implementations: Anthropic, Gemini, Ollama, OpenAI/vLLM |
| `marvin/rate_limiter.py` | Thread-safe sliding-window rate limiter, keyed by agent name (`str`) |
| `marvin/tools/registry.py` | `ToolRegistry` — maps tool names to `BaseTool` instances; serialises for each provider |
| `marvin/tools/builtin.py` | Built-in tools (`DateTimeTool`, `MemorySearchTool`); `MemorySearchTool` is a no-op in stateless mode |
| `marvin/tools/coding.py` | File I/O, shell exec, web fetch/search, browser, sub-agent session tools |
| `marvin/definitions.py` | S3 path length constants and enum base classes |
| `marvin/functions.py` | `get_s3_client()` (respects `S3_ENDPOINT_URL`), `uuidv7()` |
| `tests/` | pytest tests (no Django test runner) |
| `tests/integration/` | End-to-end pipeline tests (SQS → agent → GitHub → S3 memory) on moto; `control_plane.py` holds the routing stand-in that belongs to `wyc6k-task-manager` |
| `pyproject.toml` | Project config; pure Python, no Django deps |

## Architecture

See [weyucou/wyc6k-spec — Architecture](https://github.com/weyucou/wyc6k-spec/blob/main/docs/architecture.md) for system-wide design, task processing flow, S3 context layout, tool profiles, and LLM provider details.

## Development Commands

```bash
uv run pytest                    # run all tests
uv run pytest tests/integration  # end-to-end pipeline tests (moto; no Docker daemon)
uv run ruff check                # lint
uv run ruff format               # format
python -m marvin                 # start the SQS worker
```

Key environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SQS_QUEUE_URL` | — | **Required.** SQS queue to poll |
| `SQS_ENDPOINT_URL` | — | LocalStack override (`http://localhost:4566`) |
| `S3_ENDPOINT_URL` | AWS regional endpoint | LocalStack override |
| `AWS_DEFAULT_REGION` | `ap-northeast-1` | AWS region |
| `POLL_INTERVAL_SECONDS` | `5` | Sleep between polls |
| `VISIBILITY_TIMEOUT` | `300` | SQS message visibility timeout (seconds) |
| `SQS_WAIT_TIME_SECONDS` | `20` | Long-poll wait per SQS receive call |
| `MAX_MESSAGES` | `1` | Messages per SQS receive call |

## Do Not

- Do not add Django, psycopg, or DRF dependencies to `marvin/`.
- `RateLimiterRegistry` uses `str` keys (agent names), not `int` (DB IDs).
- Do not store state between task executions — the worker is stateless by design; each task gets a fresh context bundle from S3.
