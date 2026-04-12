from marvin.adapters.base import BaseComputeAdapter, DispatchResult
from marvin.models import TaskEnvelope


class LambdaAdapter(BaseComputeAdapter):
    """Runs the task inline; used when Lambda IS the compute entry point."""

    async def dispatch(self, envelope: TaskEnvelope) -> DispatchResult:
        return DispatchResult(
            adapter="lambda",
            job_id=None,
            status="inline_complete",
            output=None,
        )
