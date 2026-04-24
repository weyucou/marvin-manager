"""One-shot ECS Fargate entrypoint for wyc6k-task-runner.

Reads TASK_ENVELOPE_JSON from the environment, runs a single agent task,
and exits.

Exit codes:
  0 - success
  1 - task failure (agent or runtime error)
  2 - setup/config failure (env missing, parse error, credential error,
      context-pull error)
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from marvin.context import ContextBundleService, CustomerContextBundle
from marvin.credentials import CredentialResolver
from marvin.llm import LLMMessage
from marvin.models import TaskEnvelope
from marvin.runner import AgentRunner

_SERVICE = "agent-worker"
_ENVIRONMENT = os.getenv("ENVIRONMENT", "production")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
            "service": _SERVICE,
            "environment": _ENVIRONMENT,
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


logger = logging.getLogger(__name__)


def _parse_envelope(raw_json: str) -> TaskEnvelope:
    logger.info("step=parse status=start")
    try:
        envelope = TaskEnvelope.model_validate(json.loads(raw_json))
    except (json.JSONDecodeError, ValidationError):
        logger.exception("step=parse status=failed")
        sys.exit(2)
    logger.info(
        "step=parse status=done task_id=%s customer_id=%s",
        envelope.task_id,
        envelope.customer_id,
    )
    return envelope


def _resolve_credentials(envelope: TaskEnvelope) -> None:
    logger.info("step=resolve-credentials status=start")
    try:
        CredentialResolver().resolve(envelope)
    except RuntimeError:
        logger.exception("step=resolve-credentials status=failed")
        sys.exit(2)
    logger.info("step=resolve-credentials status=done")


def _pull_context(envelope: TaskEnvelope) -> CustomerContextBundle:
    logger.info("step=pull-context status=start s3_prefix=%s", envelope.s3_context_prefix)
    try:
        bundle = ContextBundleService().pull(envelope.s3_context_prefix)
    except (ClientError, BotoCoreError, ValueError):
        logger.exception("step=pull-context status=failed")
        sys.exit(2)
    logger.info("step=pull-context status=done customer_id=%s", bundle.customer_id)
    return bundle


async def _run_agent(envelope: TaskEnvelope, bundle: CustomerContextBundle) -> str:
    logger.info("step=run-agent status=start session_id=%s", envelope.session_id)
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

    system_prompt = envelope.agent.system_prompt or bundle.claude_md or None
    runner = AgentRunner(agent=envelope.agent, session_id=envelope.session_id)
    response_text, _ = await runner.chat(
        envelope.user_message,
        conversation_history=messages,
        system_prompt=system_prompt,
        enable_tools=envelope.enable_tools,
    )
    logger.info("step=run-agent status=done chars=%d", len(response_text))
    return response_text


def main() -> None:
    _configure_logging()
    t0 = time.monotonic()

    raw_json = os.getenv("TASK_ENVELOPE_JSON", "")
    if not raw_json:
        logger.error("step=parse status=failed error=TASK_ENVELOPE_JSON not set")
        sys.exit(2)

    envelope = _parse_envelope(raw_json)
    _resolve_credentials(envelope)
    bundle = _pull_context(envelope)

    try:
        asyncio.run(_run_agent(envelope, bundle))
    except Exception:  # process boundary: any agent failure → exit 1
        logger.exception("step=run-agent status=failed")
        sys.exit(1)

    # Memory writes are performed by the agent via S3MemoryWriteTool during the
    # run-agent step above; no explicit post-run flush is required here.
    logger.info("step=write-memory status=done note=delegated-to-agent")

    logger.info(
        "step=exit status=success task_id=%s elapsed_s=%.2f",
        envelope.task_id,
        time.monotonic() - t0,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
