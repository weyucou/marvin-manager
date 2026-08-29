"""Control-plane stand-in used only by the integration tests.

Routing a `TaskEnvelope` from SQS to Lambda / Fargate / Batch belongs to
**wyc6k-task-manager** (Lambda handler + compute adapters). wyc6k-task-runner is
a pure runtime: it receives a pre-built envelope, runs the agent and exits, with
no knowledge of which compute target was selected. See the Repository
Responsibility Boundary in weyucou/wyc6k-spec, and weyucou/wyc6k-task-runner#10
(closed — the adapters moved to weyucou/wyc6k-task-manager#14).

The helpers below therefore live in `tests/`, never in `marvin/`. They mirror the
documented control-plane contract closely enough to drive the runtime the way
production does: pick a compute target from `duration_hint_seconds`, and for the
Fargate target issue the `ecs:RunTask` call that injects `TASK_ENVELOPE_JSON`
into the agent-worker container.
"""

from enum import StrEnum
from typing import Any

from marvin.models import TaskEnvelope

# Routing thresholds (weyucou/wyc6k-task-manager#14).
LAMBDA_MAX_DURATION_SECONDS = 900  # Lambda hard timeout
FARGATE_MAX_DURATION_SECONDS = 21600  # 6 hr; above this AWS Batch takes over

# Container name in the agent-worker task definition
# (wyc6k-infra stacks/ecs-agent-worker.yaml).
AGENT_WORKER_CONTAINER_NAME = "agent-worker"

# Environment overrides passed at run_task() time rather than baked into the
# task definition, so each invocation receives its own envelope.
TASK_ENVELOPE_ENV_VAR = "TASK_ENVELOPE_JSON"
CUSTOMER_ID_ENV_VAR = "CUSTOMER_ID"


class ComputeTarget(StrEnum):
    """Compute backend the control plane routes a task envelope to."""

    LAMBDA_INLINE = "lambda-inline"
    FARGATE = "fargate"
    BATCH = "batch"


def select_compute_target(envelope: TaskEnvelope) -> ComputeTarget:
    """Route an envelope to a compute backend by its duration hint."""
    if envelope.duration_hint_seconds <= LAMBDA_MAX_DURATION_SECONDS:
        return ComputeTarget.LAMBDA_INLINE
    if envelope.duration_hint_seconds <= FARGATE_MAX_DURATION_SECONDS:
        return ComputeTarget.FARGATE
    return ComputeTarget.BATCH


def dispatch_to_fargate(
    ecs_client: Any,
    *,
    cluster: str,
    task_definition: str,
    subnets: list[str],
    security_groups: list[str],
    envelope_json: str,
    customer_id: str,
) -> str:
    """Launch one agent-worker task, injecting the envelope as an env override.

    Returns the ARN of the launched task.
    """
    response = ecs_client.run_task(
        cluster=cluster,
        taskDefinition=task_definition,
        launchType="FARGATE",
        count=1,
        networkConfiguration={"awsvpcConfiguration": {"subnets": subnets, "securityGroups": security_groups}},
        overrides={
            "containerOverrides": [
                {
                    "name": AGENT_WORKER_CONTAINER_NAME,
                    "environment": [
                        {"name": TASK_ENVELOPE_ENV_VAR, "value": envelope_json},
                        {"name": CUSTOMER_ID_ENV_VAR, "value": customer_id},
                    ],
                }
            ]
        },
    )
    return response["tasks"][0]["taskArn"]


def container_environment(task: dict[str, Any], container_name: str) -> dict[str, str]:
    """Read the environment overrides recorded on a launched ECS task."""
    for override in task["overrides"]["containerOverrides"]:
        if override["name"] == container_name:
            return {entry["name"]: entry["value"] for entry in override.get("environment", [])}
    return {}
