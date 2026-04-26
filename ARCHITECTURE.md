# Architecture — marvin

marvin is the **agent runtime** of the WYC6k system. It provides the LLM harness, tool profiles, and SQS-based task dispatch that agent workers use to process tasks.

For the full system architecture see [weyucou/wyc6k-spec](https://github.com/weyucou/wyc6k-spec).

## Responsibility

marvin is a stateless worker. It receives a hydrated context bundle (pulled from S3 by the worker entrypoint) and a task description, then runs an `AgentRunner` loop to completion. It does not own scheduling, dispatch, or customer identity — those belong to jones.

## Worker Paths

Two execution modes share the `marvin/` codebase. Choose based on deployment context:

| Mode | Entry point | Invocation | When to use |
|------|------------|------------|-------------|
| **SQS consumer** | `marvin/worker.py` | `python -m marvin` | Long-lived container polling an SQS queue (e.g. ECS service) |
| **One-shot Fargate** | `entrypoint.py` | container `ENTRYPOINT` | ECS Fargate RunTask per task — container starts, runs one `TaskEnvelope`, exits |

### One-shot Fargate flow (`entrypoint.py`)

```
ECS RunTask (TASK_ENVELOPE_JSON env var)
    ↓
entrypoint.py
    1. Parse   — TaskEnvelope.model_validate_json(TASK_ENVELOPE_JSON)
    2. Resolve — CredentialResolver.resolve(envelope) → GITHUB_TOKEN / api_key
    3. Pull    — ContextBundleService.pull(s3_context_prefix) → CustomerContextBundle
    4. Run     — AgentRunner.chat(user_message, ...)
    5. Memory  — ContextBundleService.push_memory(...) → daily memory file on S3
    6. Exit    — 0 (success) | 1 (task failure) | 2 (setup/config failure)
```

The one-shot path never polls SQS. `TASK_ENVELOPE_JSON` is injected by the ECS task definition override at launch time (see `wyc6k-infra`).

## Package Structure

| File | Purpose |
|------|---------|
| `marvin/models.py` | `AgentConfig`, `TaskEnvelope`, `LLMProvider`, `ToolProfile` — Pydantic models |
| `marvin/worker.py` | SQS consumer loop entry point (`python -m marvin`) |
| `marvin/runner.py` | `AgentRunner` — orchestrates tool-call loop with rate limiting |
| `marvin/context.py` | `ContextBundleService` — reads customer context from S3 |
| `marvin/llm/` | LLM clients (Anthropic, Gemini, OpenAI, Ollama) |
| `marvin/tools/` | Tool base classes, registry, built-in tools, coding tools |
| `marvin/rate_limiter.py` | Thread-safe sliding-window rate limiter (keyed by agent name) |

## TaskEnvelope flow

```
SQS → poll_once() → TaskEnvelope.model_validate()
    → ContextBundleService.pull(s3_prefix)  # fetch CLAUDE.md, SOPs, memories
    → AgentRunner(agent=AgentConfig, session_id=str)
    → runner.chat(user_message, ...)
    → LLM client (generate / generate_with_tools loop)
    → result dict → delete SQS message
```

## MemorySearchTool in stateless mode

`MemorySearchTool.execute()` returns an empty result in stateless mode. The tool is still registered in the registry so agents that reference it do not error — they simply receive "Memory search not available in stateless mode."

## AgentRunner

`marvin/runner.py` — orchestrates the tool-call loop.

- Up to 10 iterations per task
- Rate limiting between LLM calls
- Accepts a `context_bundle: ProjectContextBundle` loaded from S3 at worker startup

## Tool System

All tools inherit from `BaseTool` (`marvin/tools/base.py`) and are registered via `register_builtin_tools()`.

### Tool Profiles

| Profile | Purpose |
|---------|---------|
| `MINIMAL` | No tools — LLM response only |
| `CODING` | File I/O, shell, web fetch/search, sub-agent sessions, image analysis, browser |
| `MESSAGING` | Messaging-channel tools |
| `FULL` | All registered tools |

Per-agent allow/deny lists can further restrict or extend a profile.

### Key Coding Tools

| Tool | `require_approval` | Description |
|------|-------------------|-------------|
| `ReadTool` | No | Read file contents |
| `WriteTool` | Yes | Write/overwrite a file |
| `EditTool` | Yes | Targeted string replacement |
| `ExecTool` | Yes | Run shell commands (includes `gh` CLI) |
| `SessionsSpawnTool` | Yes | Spawn a Claude CLI sub-agent |
| `SessionsSendTool` | Yes | Send prompt to a sub-agent |
| `BrowserTool` | Yes | Browser automation via Playwright |

## LLM Clients

`BaseLLMClient` ABC with implementations for:
- Anthropic Claude
- Google Gemini
- Ollama (local)
- OpenAI / vLLM

Provider is configured per `AgentConfig` instance.

## Multi-Tenancy

Customer isolation is enforced via `customer_id` on `AgentConfig`. All operations are scoped to the customer context passed in the `TaskEnvelope`.
