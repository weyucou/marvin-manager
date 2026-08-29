from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    VLLM = "vllm"


class ToolProfile(StrEnum):
    MINIMAL = "minimal"
    CODING = "coding"
    MESSAGING = "messaging"
    FULL = "full"


class AgentConfig(BaseModel):
    """Pure-Python replacement for the Django Agent ORM model."""

    name: str
    description: str = ""
    provider: LLMProvider = LLMProvider.ANTHROPIC
    model_name: str = "claude-sonnet-4-20250514"
    base_url: str = ""
    system_prompt: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    is_active: bool = True
    rate_limit_enabled: bool = False
    rate_limit_rpm: int = 60
    tool_profile: ToolProfile = ToolProfile.FULL
    tools_allow: list[str] = Field(default_factory=list)
    tools_deny: list[str] = Field(default_factory=list)
    memory_search_enabled: bool = True
    memory_search_config: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    api_key: str | None = None  # Provider API key

    def get_allowed_tools(self, available_tools: list[str]) -> list[str]:
        """Calculate which tools this agent can use."""
        profile_tools = self._get_profile_tools(available_tools)
        allowed = set(profile_tools)
        if self.tools_allow:
            allowed.update(self.tools_allow)
        if self.tools_deny:
            allowed -= set(self.tools_deny)
        return [t for t in available_tools if t in allowed]

    def _get_profile_tools(self, available_tools: list[str]) -> list[str]:
        if self.tool_profile == ToolProfile.MINIMAL:
            return []
        if self.tool_profile == ToolProfile.CODING:
            coding_tools = {
                "read",
                "write",
                "edit",
                "apply_patch",
                "exec",
                "process",
                "web_fetch",
                "web_search",
                "browser_fetch",
                "sessions_spawn",
                "sessions_send",
                "image",
                "memory_store",
                "memory_retrieve",
                "memory_search",
                "github_issue",
                "github_pr",
                "askcc_run",
            }
            return [t for t in available_tools if t in coding_tools]
        if self.tool_profile == ToolProfile.MESSAGING:
            messaging_prefixes = ("send", "message", "notify", "email", "slack", "telegram")
            return [t for t in available_tools if any(t.startswith(p) for p in messaging_prefixes)]
        return available_tools  # FULL


class ProjectContextConfig(BaseModel):
    """Per-customer project goals and SOPs (sourced from S3, not DB)."""

    customer_id: str
    project_id: str
    goals_markdown: str = ""
    sops_snapshot: dict[str, str] = Field(default_factory=dict)
    s3_prefix: str


class TaskEnvelope(BaseModel):
    """SQS message payload for task delivery to the worker."""

    task_id: str
    customer_id: str
    session_id: str
    agent: AgentConfig
    s3_context_prefix: str
    user_message: str
    conversation_history: list[dict[str, Any]] = Field(default_factory=list)
    enable_tools: bool = True
    max_tool_iterations: int = 10
    # Estimated runtime, used by the control plane (wyc6k-task-manager) to pick a
    # compute backend. The runtime itself ignores it.
    duration_hint_seconds: int = 300
    github_token_secret_id: str | None = None
    anthropic_api_key_secret_id: str | None = None
