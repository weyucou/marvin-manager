"""Fixtures for the end-to-end pipeline integration tests.

Every AWS service is backed by **moto** (`mock_aws`) — in-process, no Docker
daemon, no network (Testing Policy → AWS Service Mocking). GitHub operations run
through a recording `gh` stub placed on `PATH`, so no live repository, token or
network access is needed either.
"""

import json
import os
import sys
from typing import TYPE_CHECKING, Any

import boto3
import pytest
import structlog
from moto import mock_aws
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from marvin.awsclients import get_secrets_manager_client
from marvin.functions import get_s3_client
from marvin.models import AgentConfig, LLMProvider, TaskEnvelope
from tests.integration.control_plane import AGENT_WORKER_CONTAINER_NAME
from tests.integration.llm_stub import ScriptedLLMServer

AWS_REGION = "us-west-2"
STAGE = "dev"

CUSTOMER_ID = "sandbox-customer"
PROJECT_REPO = "wyc6k-sandbox"
CONTEXT_BUCKET = "wyc6k-agent-context-dev"
CUSTOMER_PREFIX = f"customers/{CUSTOMER_ID}"
S3_CONTEXT_PREFIX = f"s3://{CONTEXT_BUCKET}/{CUSTOMER_PREFIX}/projects/{PROJECT_REPO}"

TASK_QUEUE_NAME = f"wyc6k-{CUSTOMER_ID}-tasks-{STAGE}"
DLQ_NAME = f"wyc6k-agent-tasks-dlq-{STAGE}"
DLQ_ALARM_NAME = f"wyc6k-agent-tasks-dlq-depth-{STAGE}"
MAX_RECEIVE_COUNT = 3

ECS_CLUSTER_NAME = f"wyc6k-agent-worker-{STAGE}"
TASK_DEFINITION_FAMILY = f"wyc6k-agent-worker-{STAGE}"
AGENT_WORKER_IMAGE = "123456789012.dkr.ecr.us-west-2.amazonaws.com/weyucou/agent-worker:latest"

SANDBOX_ISSUE_URL = "https://github.com/weyucou/wyc6k-sandbox/issues/42"
SANDBOX_PR_URL = "https://github.com/weyucou/wyc6k-sandbox/pull/123"
GITHUB_TOKEN_SECRET_ID = f"wyc6k/{STAGE}/{CUSTOMER_ID}/github-token"
GITHUB_TOKEN_VALUE = "sandbox-github-token-not-a-real-credential"

CUSTOMER_CLAUDE_MD = "# CLAUDE.md\n\nAlways comment on the issue before finishing.\n"
CUSTOMER_SOP = "# Issue Lifecycle\n\nMove the issue to in-review once a PR exists.\n"
PROJECT_GOALS = "# wyc6k-sandbox\n\nSandbox project used by the pipeline integration tests.\n"

# `gh` stand-in: records every invocation — arguments plus the token it was
# handed — as one JSON object per line, and answers the read-back commands the
# agent tools rely on.
_GH_STUB_SOURCE = '''#!{python}
"""Recording stand-in for the `gh` CLI."""

import json
import os
import pathlib
import sys

argv = sys.argv[1:]
record = json.dumps(dict(argv=argv, github_token=os.environ.get("GITHUB_TOKEN", "")))
with pathlib.Path({record_path!r}).open("a", encoding="utf-8") as handle:
    handle.write(record + "\\n")

if argv[:2] == ["pr", "create"]:
    sys.stdout.write({pr_url!r} + "\\n")
elif argv[:2] == ["issue", "view"]:
    sys.stdout.write("title:\\tSandbox task\\n")
'''


class TaskQueues(BaseModel):
    """Per-customer task queue and the shared dead-letter queue behind it."""

    url: str
    arn: str
    dlq_url: str
    dlq_arn: str


class FargateEnvironment(BaseModel):
    """Registered agent-worker cluster, task definition and network placement."""

    cluster_name: str
    task_definition_arn: str
    subnets: list[str]
    security_groups: list[str]


class GhInvocation(BaseModel):
    """One recorded `gh` call: its arguments and the token it ran with."""

    argv: list[str]
    github_token: str


class GhRecorder:
    """Reads back the invocations captured by the `gh` stub."""

    def __init__(self, record_path: Path) -> None:
        self._record_path = record_path

    @property
    def invocations(self) -> list[GhInvocation]:
        """Every `gh` invocation, in order."""
        if not self._record_path.exists():
            return []
        lines = self._record_path.read_text(encoding="utf-8").splitlines()
        return [GhInvocation.model_validate_json(line) for line in lines if line]

    def find(self, *prefix: str) -> list[GhInvocation]:
        """Invocations starting with the given subcommand, e.g. `find("issue", "comment")`."""
        return [call for call in self.invocations if call.argv[: len(prefix)] == list(prefix)]


@pytest.fixture(autouse=True)
def aws(monkeypatch) -> Iterator[None]:
    """Run every test in this package against in-process AWS doubles."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    with mock_aws():
        yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def s3_context() -> str:
    """Seed the customer context bundle in S3 and return its project prefix."""
    s3 = get_s3_client()
    s3.create_bucket(
        Bucket=CONTEXT_BUCKET,
        CreateBucketConfiguration={"LocationConstraint": AWS_REGION},
    )
    project_key = f"{CUSTOMER_PREFIX}/projects/{PROJECT_REPO}"
    objects = {
        f"{CUSTOMER_PREFIX}/CLAUDE.md": CUSTOMER_CLAUDE_MD,
        f"{CUSTOMER_PREFIX}/sops/issue-lifecycle.md": CUSTOMER_SOP,
        f"{project_key}/README.md": PROJECT_GOALS,
        f"{project_key}/MEMORY.md": "# Memory index\n",
    }
    for key, body in objects.items():
        s3.put_object(Bucket=CONTEXT_BUCKET, Key=key, Body=body.encode("utf-8"))
    return S3_CONTEXT_PREFIX


@pytest.fixture
def github_token_secret() -> str:
    """Store the customer GitHub token in Secrets Manager and return its ID."""
    secrets = get_secrets_manager_client()
    secrets.create_secret(Name=GITHUB_TOKEN_SECRET_ID, SecretString=GITHUB_TOKEN_VALUE)
    return GITHUB_TOKEN_SECRET_ID


@pytest.fixture
def sqs_client() -> Any:
    return boto3.client("sqs", region_name=AWS_REGION)


@pytest.fixture
def task_queues(sqs_client) -> TaskQueues:
    """Provision the customer task queue with a redrive policy to the shared DLQ."""
    dlq_url = sqs_client.create_queue(QueueName=DLQ_NAME)["QueueUrl"]
    dlq_arn = sqs_client.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    redrive_policy = json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEIVE_COUNT})
    queue_url = sqs_client.create_queue(
        QueueName=TASK_QUEUE_NAME,
        Attributes={"RedrivePolicy": redrive_policy},
    )["QueueUrl"]
    queue_arn = sqs_client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    return TaskQueues(url=queue_url, arn=queue_arn, dlq_url=dlq_url, dlq_arn=dlq_arn)


@pytest.fixture
def cloudwatch_client() -> Any:
    return boto3.client("cloudwatch", region_name=AWS_REGION)


@pytest.fixture
def dlq_depth_alarm(cloudwatch_client) -> str:
    """Create the DLQ depth alarm defined by wyc6k-infra stacks/shared-dlq.yaml."""
    cloudwatch_client.put_metric_alarm(
        AlarmName=DLQ_ALARM_NAME,
        AlarmDescription="Shared agent task DLQ has messages — investigate processing failures",
        Namespace="AWS/SQS",
        MetricName="ApproximateNumberOfMessagesVisible",
        Dimensions=[{"Name": "QueueName", "Value": DLQ_NAME}],
        Statistic="Sum",
        Period=60,
        EvaluationPeriods=1,
        Threshold=0,
        ComparisonOperator="GreaterThanThreshold",
    )
    return DLQ_ALARM_NAME


@pytest.fixture
def ecs_client() -> Any:
    return boto3.client("ecs", region_name=AWS_REGION)


@pytest.fixture
def fargate(ecs_client) -> FargateEnvironment:
    """Register the agent-worker cluster, task definition and network placement."""
    ec2 = boto3.client("ec2", region_name=AWS_REGION)
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    # moto only assigns a private DNS name to the task ENI when the VPC has it enabled.
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    subnet_id = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
    security_group_id = ec2.create_security_group(
        GroupName="agent-worker",
        Description="agent worker tasks",
        VpcId=vpc_id,
    )["GroupId"]

    ecs_client.create_cluster(clusterName=ECS_CLUSTER_NAME)
    task_definition = ecs_client.register_task_definition(
        family=TASK_DEFINITION_FAMILY,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="1024",
        memory="2048",
        containerDefinitions=[
            {
                "name": AGENT_WORKER_CONTAINER_NAME,
                "image": AGENT_WORKER_IMAGE,
                "essential": True,
                # Task-level cpu/memory is what Fargate uses; moto additionally
                # requires them per container to size the task.
                "cpu": 1024,
                "memory": 2048,
            }
        ],
    )
    return FargateEnvironment(
        cluster_name=ECS_CLUSTER_NAME,
        task_definition_arn=task_definition["taskDefinition"]["taskDefinitionArn"],
        subnets=[subnet_id],
        security_groups=[security_group_id],
    )


@pytest.fixture
def gh_cli(tmp_path, monkeypatch) -> GhRecorder:
    """Put a recording `gh` stand-in at the front of PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record_path = tmp_path / "gh-invocations.jsonl"
    stub = bin_dir / "gh"
    stub.write_text(
        _GH_STUB_SOURCE.format(
            python=sys.executable,
            record_path=str(record_path),
            pr_url=SANDBOX_PR_URL,
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # Clear any ambient token so Secrets Manager is the only source the runtime
    # can resolve one from; monkeypatch restores the original at teardown even
    # though the credential resolver writes to os.environ itself.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return GhRecorder(record_path)


@pytest.fixture
def llm() -> Iterator[ScriptedLLMServer]:
    """Serve scripted model replies over an OpenAI-compatible endpoint."""
    server = ScriptedLLMServer()
    server.start()
    yield server
    server.stop()


class Pipeline:
    """The provisioned pipeline a task envelope travels through."""

    def __init__(
        self,
        *,
        sqs: Any,
        ecs: Any,
        cloudwatch: Any,
        queues: TaskQueues,
        fargate: FargateEnvironment,
        gh: GhRecorder,
        llm: ScriptedLLMServer,
        s3_context_prefix: str,
        github_token_secret_id: str,
        dlq_alarm_name: str,
    ) -> None:
        self.sqs = sqs
        self.ecs = ecs
        self.cloudwatch = cloudwatch
        self.queues = queues
        self.fargate = fargate
        self.gh = gh
        self.llm = llm
        self.s3_context_prefix = s3_context_prefix
        self.github_token_secret_id = github_token_secret_id
        self.dlq_alarm_name = dlq_alarm_name


@pytest.fixture
def pipeline(
    s3_context,
    github_token_secret,
    sqs_client,
    task_queues,
    ecs_client,
    fargate,
    cloudwatch_client,
    dlq_depth_alarm,
    gh_cli,
    llm,
) -> Pipeline:
    """Provision queues, cluster, S3 context, credentials and the two stubs."""
    return Pipeline(
        sqs=sqs_client,
        ecs=ecs_client,
        cloudwatch=cloudwatch_client,
        queues=task_queues,
        fargate=fargate,
        gh=gh_cli,
        llm=llm,
        s3_context_prefix=s3_context,
        github_token_secret_id=github_token_secret,
        dlq_alarm_name=dlq_depth_alarm,
    )


def make_envelope(
    llm_server: ScriptedLLMServer,
    *,
    task_id: str,
    user_message: str,
    duration_hint_seconds: int,
    github_token_secret_id: str | None = None,
) -> TaskEnvelope:
    """Build the envelope wyc6k-task-manager would enqueue for this customer."""
    agent = AgentConfig(
        name="sandbox-agent",
        provider=LLMProvider.VLLM,
        model_name="stub-model",
        base_url=llm_server.base_url,
        api_key="stub-key",
        system_prompt="You are the wyc6k sandbox agent.",
    )
    return TaskEnvelope(
        task_id=task_id,
        customer_id=CUSTOMER_ID,
        session_id=f"session-{task_id}",
        agent=agent,
        s3_context_prefix=S3_CONTEXT_PREFIX,
        user_message=user_message,
        duration_hint_seconds=duration_hint_seconds,
        github_token_secret_id=github_token_secret_id,
    )
