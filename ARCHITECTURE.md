# Architecture — marvin

marvin is the **agent runtime** of the WYC6k system. It provides the LLM harness, tool profiles, and SQS-based task dispatch that agent workers use to process tasks.

For the full system architecture see [weyucou/wyc6k-spec](https://github.com/weyucou/wyc6k-spec).

## Responsibility

marvin is a stateless worker. It receives a hydrated context bundle (pulled from S3 by the worker entrypoint) and a task description, then runs an `AgentRunner` loop to completion. It does not own scheduling, dispatch, or customer identity — those belong to jones.

## Worker Paths

The `marvin/` codebase supports two execution modes that share the same agent runtime:

| Path | Entry point | Trigger | Lifecycle |
|------|-------------|---------|-----------|
| **SQS consumer** | `marvin/worker.py` (`python -m marvin`) | Long-lived container polls SQS | Loops until `SIGTERM`/`SIGINT` |
| **Fargate one-shot** | `entrypoint.py` | ECS `RunTask` per task (via `TASK_ENVELOPE_JSON` env var) | Runs one task, then `sys.exit` |

Use the **SQS consumer** when you want a persistent, polling worker (e.g., EC2, ECS Service).  
Use the **Fargate one-shot** when you want per-task isolation with ECS Fargate or AWS Batch — the scheduler enqueues a `RunTask` call and the container exits after the task completes.

### One-shot entrypoint sequence (`entrypoint.py`)

```
ECS Fargate RunTask  (TASK_ENVELOPE_JSON env override)
    ↓
entrypoint.py
    1. Parse TASK_ENVELOPE_JSON → TaskEnvelope  [exit 2 on failure]
    2. CredentialResolver.resolve(envelope)      [exit 2 on failure]
    3. ContextBundleService.pull(s3_prefix)      [exit 2 on failure]
    4. AgentRunner.chat(user_message, ...)       [exit 1 on failure]
    5. S3MemoryWriteTool (agent-invoked during step 4) → daily memory file on S3
    6. sys.exit(0)
```

Structured JSON logs are emitted to stdout for each step, keyed by `step` and `status` fields.

## Package Structure

| File | Purpose |
|------|---------|
| `marvin/models.py` | `AgentConfig`, `TaskEnvelope`, `LLMProvider`, `ToolProfile` — Pydantic models |
| `marvin/worker.py` | SQS consumer loop entry point (`python -m marvin`) |
| `entrypoint.py` | One-shot Fargate/Batch entry point (one task, then exit) |
| `marvin/runner.py` | `AgentRunner` — orchestrates tool-call loop with rate limiting |
| `marvin/context.py` | `ContextBundleService` — reads customer context from S3 |
| `marvin/llm/` | LLM clients (Anthropic, Gemini, OpenAI, Ollama) |
| `marvin/tools/` | Tool base classes, registry, built-in tools, coding tools |
| `marvin/rate_limiter.py` | Thread-safe sliding-window rate limiter (keyed by agent name) |

## SQS consumer flow

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
