# wyc6k-task-runner

The **agent runtime** of the WYC6k multi-tenant agent harness. Dequeues `TaskEnvelope` messages from SQS, runs an LLM agent in a tool-call loop against a GitHub issue, and writes memory back to S3 on completion.

## System Architecture

wyc6k-task-runner is the agent runtime of the **WYC6k** multi-tenant agent harness. See [weyucou/wyc6k-spec](https://github.com/weyucou/wyc6k-spec) for the full system architecture, and [ARCHITECTURE.md](ARCHITECTURE.md) for wyc6k-task-runner internals.

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/weyucou/wyc6k-task-runner.git
cd wyc6k-task-runner
uv sync

# 2. Start LocalStack (SQS/S3)
docker-compose up -d

# 3. Set required environment variables
export SQS_QUEUE_URL=http://localhost:4566/000000000000/tasks
export AWS_DEFAULT_REGION=us-east-1
export ANTHROPIC_API_KEY=your-key-here

# 4. Start the worker
python -m marvin
```

## Requirements

- Python 3.14
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (for local SQS/S3 via LocalStack)

## Available Tools

All tools inherit from `BaseTool` (`marvin/tools/base.py`) and are registered via `register_builtin_tools()`.

### Core Tools (`marvin/tools/builtin.py`)

| Tool | Name | Description |
|------|------|-------------|
| `DateTimeTool` | `get_datetime` | Current date/time in any timezone |
| `CalculatorTool` | `calculator` | Safe arithmetic expression evaluator |
| `MemorySearchTool` | `memory_search` | Returns empty in stateless mode |

### Coding Tools (`marvin/tools/coding.py`)

| Tool | Name | `require_approval` | Description |
|------|------|-------------------|-------------|
| `ReadTool` | `read` | No | Read file contents with optional line range |
| `WriteTool` | `write` | Yes | Write/overwrite a file |
| `EditTool` | `edit` | Yes | Targeted unique-string replacement in a file |
| `ApplyPatchTool` | `apply_patch` | Yes | Apply a unified diff patch via the `patch` CLI |
| `ExecTool` | `exec` | Yes | Run shell commands (includes `gh` CLI for GitHub) |
| `ProcessTool` | `process` | Yes | Manage long-running background shell sessions |
| `WebFetchTool` | `web_fetch` | No | Fetch URL content as plain text |
| `RealWebSearchTool` | `web_search` | No | Search via Brave Search API |
| `SessionsSpawnTool` | `sessions_spawn` | Yes | Spawn a Claude CLI sub-agent session |
| `SessionsSendTool` | `sessions_send` | Yes | Send a prompt to a Claude CLI sub-agent |
| `ImageTool` | `image` | No | Analyze images via Claude vision |
| `BrowserTool` | `browser` | Yes | Browser automation via Playwright |

### Tool Profiles

| Profile | Tools |
|---------|-------|
| `minimal` | None |
| `coding` | `read`, `write`, `edit`, `apply_patch`, `exec`, `process`, `web_fetch`, `web_search`, `sessions_spawn`, `sessions_send`, `image`, `memory_search` |
| `full` | All tools |

### Required Environment Variables

| Variable | Required By | Description |
|----------|-------------|-------------|
| `SQS_QUEUE_URL` | worker | SQS queue URL to poll for tasks |
| `ZAATAR_SEARCH_API_URL` | `web_search` | Base URL of a deployed [zaatar-search-api](https://github.com/monkut/zaatar-search-api) instance (e.g. `http://localhost:5000`) |
| `ANTHROPIC_API_KEY` | `image` | Anthropic API key for vision analysis |

### Optional Runtime Dependencies

| Dependency | Required By | Install |
|------------|-------------|---------|
| `playwright` + Chromium | `browser` | `pip install playwright && playwright install chromium` |
| `claude` CLI | `sessions_spawn`, `sessions_send` | Install from [Claude Code](https://claude.ai/code) |

## Development

### Run Tests

```bash
uv run poe test
```

### Run Linter

```bash
uv run poe check
```

### Run Type Checker

```bash
uv run pyright
```

## Project Structure

```
wyc6k-task-runner/
├── marvin/                 # Stateless SQS worker
│   ├── llm/                # LLM clients (Anthropic, Gemini, OpenAI, Ollama)
│   ├── tools/              # Tool base classes, registry, built-in and coding tools
│   ├── models.py           # Pydantic models (AgentConfig, TaskEnvelope)
│   ├── runner.py           # AgentRunner — tool-call loop
│   ├── worker.py           # SQS consumer entry point
│   ├── context.py          # ContextBundleService — reads from S3
│   └── rate_limiter.py     # Thread-safe sliding-window rate limiter
├── tests/                  # pytest tests
├── docker-compose.yaml     # LocalStack (SQS/S3) for development
└── pyproject.toml          # Project dependencies
```

## License

See [LICENSE](LICENSE) file.
