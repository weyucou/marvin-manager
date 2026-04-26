"""One-shot Fargate/Batch entrypoint for the marvin agent worker.

ECS Fargate launches one container per task with TASK_ENVELOPE_JSON set.
Resolves credentials, pulls S3 context, runs the agent, writes a memory
summary, then exits.

Exit codes:
    0 — task completed successfully
    1 — task failure (agent error during run)
    2 — setup/config failure (parse, credential, or context pull error)
"""

import asyncio
import datetime
import os
import sys
from typing import Any

import structlog
from pydantic import ValidationError

from marvin.context import ContextBundleService, CustomerContextBundle, MemoryEntry
from marvin.credentials import CredentialResolver
from marvin.llm import LLMMessage
from marvin.models import TaskEnvelope
from marvin.runner import AgentRunner

_SENSITIVE_KEYS = frozenset({
    "password",
    "token",
    "secret",
    "ssn",
    "email",
    "credit_card",
    "authorization",
    "api_key",
})


def _scrub_sensitive_fields(
    _logger: structlog.types.WrappedLogger,
    _method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    for key in _SENSITIVE_KEYS & event_dict.keys():
        event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _scrub_sensitive_fields,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def _run_agent(envelope: TaskEnvelope, context_bundle: CustomerContextBundle) -> str:
    messages: list[LLMMessage] = []
    for msg in envelope.conversation_history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            messages.append(LLMMessage.system(content))
        elif role == "assistant":
            messages.append(LLMMessage.assistant(content))
        else:
            messages.append(LLMMessage.user(content))

    runner = AgentRunner(
        agent=envelope.agent,
        session_id=envelope.session_id,
        context_bundle=context_bundle,
    )
    response_text, _ = await runner.chat(
        envelope.user_message,
        conversation_history=messages,
        enable_tools=envelope.enable_tools,
    )
    return response_text


def main() -> int:
    configure_logging()
    log: Any = structlog.get_logger(__name__)

    service = "agent-worker-entrypoint"
    environment = os.getenv("ENVIRONMENT", "production")
    structlog.contextvars.bind_contextvars(service=service, environment=environment, logger=__name__)

    # Step 1: Parse TASK_ENVELOPE_JSON
    raw = os.getenv("TASK_ENVELOPE_JSON", "")
    if not raw:
        log.error("TASK_ENVELOPE_JSON not set", step="parse", status="failed")
        return 2

    log.info("Parsing task envelope", step="parse", status="start")
    try:
        envelope = TaskEnvelope.model_validate_json(raw)
    except ValidationError as exc:
        log.error("Failed to parse task envelope", step="parse", status="failed", error=str(exc))
        return 2

    structlog.contextvars.bind_contextvars(
        task_id=envelope.task_id,
        customer_id=envelope.customer_id,
        session_id=envelope.session_id,
    )
    log.info("Task envelope parsed", step="parse", status="ok")

    # Step 2: Resolve credentials
    log.info("Resolving credentials", step="resolve-credentials", status="start")
    try:
        CredentialResolver().resolve(envelope)
    except RuntimeError as exc:
        log.error("Failed to resolve credentials", step="resolve-credentials", status="failed", error=str(exc))
        return 2
    log.info("Credentials resolved", step="resolve-credentials", status="ok")

    # Step 3: Pull S3 context
    log.info("Pulling S3 context", step="pull-context", status="start", s3_prefix=envelope.s3_context_prefix)
    try:
        context_service = ContextBundleService()
        context_bundle = context_service.pull(envelope.s3_context_prefix)
    except Exception as exc:
        log.error("Failed to pull S3 context", step="pull-context", status="failed", error=str(exc))
        return 2
    log.info("S3 context pulled", step="pull-context", status="ok")

    # Step 4: Run agent
    log.info("Running agent", step="run-agent", status="start")
    try:
        response_text = asyncio.run(_run_agent(envelope, context_bundle))
    except Exception as exc:
        log.error("Agent run failed", step="run-agent", status="failed", error=str(exc))
        return 1
    log.info("Agent run complete", step="run-agent", status="ok", response_length=len(response_text))

    # Step 5: Write memory summary
    log.info("Writing memory entry", step="write-memory", status="start")
    try:
        today = datetime.datetime.now(tz=datetime.UTC).date()
        memory_entry = MemoryEntry(
            date=today,
            filename=f"{today.isoformat()}.md",
            content=f"## {envelope.task_id}\n\n{response_text}\n",
        )
        context_service.push_memory(envelope.s3_context_prefix, memory_entry)
        log.info("Memory entry written", step="write-memory", status="ok")
    except Exception as exc:
        log.warning("Failed to write memory entry", step="write-memory", status="failed", error=str(exc))

    log.info("Task complete", step="exit", exit_code=0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
