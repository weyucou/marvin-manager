"""End-to-end pipeline tests: SQS → agent → GitHub issue → S3 memory.

Each scenario enqueues a `TaskEnvelope` on a moto-backed SQS queue, routes it the
way wyc6k-task-manager does, then runs the real one-shot runtime (`entrypoint.py`
/ `marvin.worker`) against moto-backed S3, ECS and Secrets Manager. Only two
boundaries are stubbed: the model (a scripted OpenAI-compatible endpoint the real
`OpenAIClient` talks to) and the `gh` CLI (a recording stand-in on PATH).
"""

import datetime
import json
from typing import Any
from urllib.parse import urlparse

import pytest

import entrypoint
from marvin import worker
from marvin.functions import get_s3_client
from marvin.models import TaskEnvelope
from tests.integration.conftest import (
    CUSTOMER_ID,
    DLQ_NAME,
    GITHUB_TOKEN_VALUE,
    MAX_RECEIVE_COUNT,
    SANDBOX_ISSUE_URL,
    SANDBOX_PR_URL,
    Pipeline,
    make_envelope,
)
from tests.integration.control_plane import (
    AGENT_WORKER_CONTAINER_NAME,
    ComputeTarget,
    container_environment,
    dispatch_to_fargate,
    select_compute_target,
)
from tests.integration.llm_stub import text_turn, tool_call_turn

SHORT_TASK_DURATION_SECONDS = 300
MEDIUM_TASK_DURATION_SECONDS = 3600

pytestmark = pytest.mark.integration


def _receive_one(sqs: Any, queue_url: str, *, visibility_timeout: int = 30) -> dict[str, Any] | None:
    """Receive a single message without long polling."""
    response = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=0,
        VisibilityTimeout=visibility_timeout,
    )
    messages = response.get("Messages", [])
    return messages[0] if messages else None


def _run_inline(monkeypatch, envelope_json: str) -> int:
    """Execute one envelope in-process, as the Lambda and the container both do."""
    monkeypatch.setenv("TASK_ENVELOPE_JSON", envelope_json)
    return entrypoint.main()


def _read_daily_memory(s3_context_prefix: str) -> str:
    """Read today's memory file written by the runtime."""
    parsed = urlparse(s3_context_prefix)
    today = datetime.datetime.now(tz=datetime.UTC).date()
    key = f"{parsed.path.strip('/')}/memory/{today.year}/{today.isoformat()}.md"
    body = get_s3_client().get_object(Bucket=parsed.netloc, Key=key)["Body"].read()
    return body.decode("utf-8")


def _system_prompt(request_payload: dict[str, Any]) -> str:
    """Extract the system prompt the runtime sent to the model."""
    for message in request_payload["messages"]:
        if message["role"] == "system":
            return message["content"]
    return ""


class TestShortTaskInlinePath:
    """action=prepare, duration_hint=300s — the Lambda handles the task inline."""

    def test_inline_path_comments_on_issue_and_writes_memory(self, monkeypatch, pipeline: Pipeline) -> None:
        pipeline.llm.script(
            [
                tool_call_turn(
                    "call-comment",
                    "github_issue",
                    {
                        "action": "comment",
                        "issue_url": SANDBOX_ISSUE_URL,
                        "body": "Acceptance criteria are testable; ready for development.",
                    },
                ),
                text_turn("Prepared issue 42 — acceptance criteria rewritten."),
            ]
        )
        envelope = make_envelope(
            pipeline.llm,
            task_id="task-prepare-001",
            user_message="Prepare issue 42 for development.",
            duration_hint_seconds=SHORT_TASK_DURATION_SECONDS,
            github_token_secret_id=pipeline.github_token_secret_id,
        )
        pipeline.sqs.send_message(QueueUrl=pipeline.queues.url, MessageBody=envelope.model_dump_json())

        message = _receive_one(pipeline.sqs, pipeline.queues.url)
        assert message is not None
        delivered = TaskEnvelope.model_validate_json(message["Body"])
        assert select_compute_target(delivered) is ComputeTarget.LAMBDA_INLINE

        assert _run_inline(monkeypatch, message["Body"]) == 0
        pipeline.sqs.delete_message(QueueUrl=pipeline.queues.url, ReceiptHandle=message["ReceiptHandle"])

        comments = pipeline.gh.find("issue", "comment")
        assert len(comments) == 1
        assert SANDBOX_ISSUE_URL in comments[0].argv
        assert "Acceptance criteria are testable" in comments[0].argv[-1]
        # The token `gh` ran with came from Secrets Manager, not the ambient env.
        assert comments[0].github_token == GITHUB_TOKEN_VALUE

        memory = _read_daily_memory(pipeline.s3_context_prefix)
        assert "task-prepare-001" in memory
        assert "acceptance criteria rewritten" in memory

        assert _receive_one(pipeline.sqs, pipeline.queues.url) is None

    def test_inline_path_spawns_no_ecs_task(self, monkeypatch, pipeline: Pipeline) -> None:
        pipeline.llm.script([text_turn("Nothing to change.")])
        envelope = make_envelope(
            pipeline.llm,
            task_id="task-prepare-002",
            user_message="Prepare issue 42 for development.",
            duration_hint_seconds=SHORT_TASK_DURATION_SECONDS,
            github_token_secret_id=pipeline.github_token_secret_id,
        )
        pipeline.sqs.send_message(QueueUrl=pipeline.queues.url, MessageBody=envelope.model_dump_json())
        message = _receive_one(pipeline.sqs, pipeline.queues.url)
        assert message is not None

        assert _run_inline(monkeypatch, message["Body"]) == 0

        assert pipeline.ecs.list_tasks(cluster=pipeline.fargate.cluster_name)["taskArns"] == []

    def test_inline_path_feeds_s3_context_to_the_model(self, monkeypatch, pipeline: Pipeline) -> None:
        pipeline.llm.script([text_turn("Understood.")])
        envelope = make_envelope(
            pipeline.llm,
            task_id="task-prepare-003",
            user_message="Summarise the project goals.",
            duration_hint_seconds=SHORT_TASK_DURATION_SECONDS,
            github_token_secret_id=pipeline.github_token_secret_id,
        )
        pipeline.sqs.send_message(QueueUrl=pipeline.queues.url, MessageBody=envelope.model_dump_json())
        message = _receive_one(pipeline.sqs, pipeline.queues.url)
        assert message is not None

        assert _run_inline(monkeypatch, message["Body"]) == 0

        system_prompt = _system_prompt(pipeline.llm.requests[0])
        assert "Always comment on the issue before finishing." in system_prompt
        assert "Move the issue to in-review once a PR exists." in system_prompt
        assert "Sandbox project used by the pipeline integration tests." in system_prompt


class TestMediumTaskFargatePath:
    """action=develop, duration_hint=3600s — the Lambda hands off to Fargate."""

    def _dispatch(self, pipeline: Pipeline, envelope: TaskEnvelope) -> dict[str, Any]:
        """Enqueue, route and launch the envelope; return the recorded ECS task."""
        pipeline.sqs.send_message(QueueUrl=pipeline.queues.url, MessageBody=envelope.model_dump_json())
        message = _receive_one(pipeline.sqs, pipeline.queues.url)
        assert message is not None
        delivered = TaskEnvelope.model_validate_json(message["Body"])
        assert select_compute_target(delivered) is ComputeTarget.FARGATE

        task_arn = dispatch_to_fargate(
            pipeline.ecs,
            cluster=pipeline.fargate.cluster_name,
            task_definition=pipeline.fargate.task_definition_arn,
            subnets=pipeline.fargate.subnets,
            security_groups=pipeline.fargate.security_groups,
            envelope_json=message["Body"],
            customer_id=delivered.customer_id,
        )
        pipeline.sqs.delete_message(QueueUrl=pipeline.queues.url, ReceiptHandle=message["ReceiptHandle"])
        described = pipeline.ecs.describe_tasks(cluster=pipeline.fargate.cluster_name, tasks=[task_arn])
        return described["tasks"][0]

    def _envelope(self, pipeline: Pipeline, task_id: str) -> TaskEnvelope:
        return make_envelope(
            pipeline.llm,
            task_id=task_id,
            user_message="Implement issue 42 and open a pull request.",
            duration_hint_seconds=MEDIUM_TASK_DURATION_SECONDS,
            github_token_secret_id=pipeline.github_token_secret_id,
        )

    def test_run_task_uses_the_agent_worker_task_definition(self, pipeline: Pipeline) -> None:
        pipeline.llm.script([text_turn("Done.")])
        envelope = self._envelope(pipeline, "task-develop-001")

        task = self._dispatch(pipeline, envelope)

        assert task["taskDefinitionArn"] == pipeline.fargate.task_definition_arn
        assert task["launchType"] == "FARGATE"
        overrides = container_environment(task, AGENT_WORKER_CONTAINER_NAME)
        assert overrides["CUSTOMER_ID"] == CUSTOMER_ID
        assert TaskEnvelope.model_validate_json(overrides["TASK_ENVELOPE_JSON"]) == envelope

    def test_launched_container_updates_issue_with_pr_link_and_memory(self, monkeypatch, pipeline: Pipeline) -> None:
        pipeline.llm.script(
            [
                tool_call_turn(
                    "call-pr",
                    "github_pr",
                    {
                        "action": "create",
                        "repo": "weyucou/wyc6k-sandbox",
                        "title": "feat: implement issue 42",
                        "body": "Closes #42",
                        "base": "main",
                        "head": "feature/42-implement",
                    },
                ),
                tool_call_turn(
                    "call-comment",
                    "github_issue",
                    {
                        "action": "comment",
                        "issue_url": SANDBOX_ISSUE_URL,
                        "body": f"Implementation is up for review: {SANDBOX_PR_URL}",
                    },
                ),
                text_turn(f"Implemented issue 42 and opened {SANDBOX_PR_URL}."),
            ]
        )
        envelope = self._envelope(pipeline, "task-develop-002")
        task = self._dispatch(pipeline, envelope)
        overrides = container_environment(task, AGENT_WORKER_CONTAINER_NAME)

        # The container starts with exactly the environment RunTask recorded.
        assert _run_inline(monkeypatch, overrides["TASK_ENVELOPE_JSON"]) == 0

        assert len(pipeline.gh.find("pr", "create")) == 1
        comments = pipeline.gh.find("issue", "comment")
        assert len(comments) == 1
        assert SANDBOX_PR_URL in comments[0].argv[-1]

        memory = _read_daily_memory(pipeline.s3_context_prefix)
        assert "task-develop-002" in memory
        assert SANDBOX_PR_URL in memory


class TestFailedTaskDlqPath:
    """A malformed envelope is retried, then parked on the shared DLQ."""

    MALFORMED_BODY = '{"task_id": "task-broken-001", "customer_id":'

    def _consume(self, monkeypatch, pipeline: Pipeline, attempts: int) -> None:
        """Run the real SQS consumer, which never deletes a message it cannot parse."""
        monkeypatch.setattr(worker, "SQS_QUEUE_URL", pipeline.queues.url)
        monkeypatch.setattr(worker, "VISIBILITY_TIMEOUT", 0)
        monkeypatch.setattr(worker, "WAIT_TIME_SECONDS", 0)
        for _ in range(attempts):
            worker.poll_once(pipeline.sqs)

    def test_message_reaches_dlq_after_three_retries(self, monkeypatch, pipeline: Pipeline) -> None:
        pipeline.sqs.send_message(QueueUrl=pipeline.queues.url, MessageBody=self.MALFORMED_BODY)

        self._consume(monkeypatch, pipeline, MAX_RECEIVE_COUNT)
        assert _receive_one(pipeline.sqs, pipeline.queues.dlq_url) is None

        # The delivery after maxReceiveCount is what moves the message across.
        self._consume(monkeypatch, pipeline, 1)

        parked = _receive_one(pipeline.sqs, pipeline.queues.dlq_url)
        assert parked is not None
        assert parked["Body"] == self.MALFORMED_BODY
        assert _receive_one(pipeline.sqs, pipeline.queues.url) is None

    def test_poisoned_message_never_reaches_the_agent(self, monkeypatch, pipeline: Pipeline) -> None:
        pipeline.sqs.send_message(QueueUrl=pipeline.queues.url, MessageBody=self.MALFORMED_BODY)

        self._consume(monkeypatch, pipeline, MAX_RECEIVE_COUNT + 1)

        assert pipeline.gh.invocations == []
        assert pipeline.llm.requests == []
        assert pipeline.ecs.list_tasks(cluster=pipeline.fargate.cluster_name)["taskArns"] == []

    def test_dlq_depth_alarm_watches_the_shared_queue(self, pipeline: Pipeline) -> None:
        alarms = pipeline.cloudwatch.describe_alarms(AlarmNames=[pipeline.dlq_alarm_name])["MetricAlarms"]

        assert len(alarms) == 1
        alarm = alarms[0]
        assert alarm["Namespace"] == "AWS/SQS"
        assert alarm["MetricName"] == "ApproximateNumberOfMessagesVisible"
        assert alarm["Dimensions"] == [{"Name": "QueueName", "Value": DLQ_NAME}]
        assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
        assert alarm["Threshold"] == 0


class TestRedrivePolicy:
    """The queue provisioning the scenarios rely on matches wyc6k-infra."""

    def test_task_queue_redrives_to_the_shared_dlq(self, pipeline: Pipeline) -> None:
        attributes = pipeline.sqs.get_queue_attributes(
            QueueUrl=pipeline.queues.url,
            AttributeNames=["RedrivePolicy"],
        )["Attributes"]
        redrive_policy = json.loads(attributes["RedrivePolicy"])

        assert redrive_policy["deadLetterTargetArn"] == pipeline.queues.dlq_arn
        assert redrive_policy["maxReceiveCount"] == MAX_RECEIVE_COUNT
