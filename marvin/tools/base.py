"""Base classes for tool definitions and execution."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class ToolStatus(StrEnum):
    """Status of a tool execution result."""

    SUCCESS = "success"
    ERROR = "error"
    PENDING = "pending"
    APPROVAL_REQUIRED = "approval_required"


@dataclass
class ToolResult:
    """Result of a tool execution."""

    status: ToolStatus
    output: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for serialization."""
        result = {
            "status": self.status.value,
            "output": self.output,
        }
        if self.data:
            result["data"] = self.data
        if self.error:
            result["error"] = self.error
        return result

    @classmethod
    def success(cls, output: str, data: dict[str, Any] | None = None) -> ToolResult:
        """Create a successful result."""
        return cls(status=ToolStatus.SUCCESS, output=output, data=data or {})

    @classmethod
    def from_error(cls, error_msg: str, output: str = "") -> ToolResult:
        """Create an error result."""
        return cls(status=ToolStatus.ERROR, output=output, error=error_msg)


@dataclass
class ToolParameter:
    """Definition of a tool parameter."""

    name: str
    type: str  # "string", "number", "boolean", "array", "object"
    description: str
    required: bool = True
    default: Any = None
    enum: list[str] | None = None

    def to_json_schema(self) -> dict[str, Any]:
        """Convert to JSON Schema format."""
        schema: dict[str, Any] = {
            "type": self.type,
            "description": self.description,
        }
        if self.enum:
            schema["enum"] = self.enum
        if self.default is not None:
            schema["default"] = self.default
        return schema


class BaseTool(ABC):
    """Abstract base class for all tools.

    Tools must implement:
    - name: Unique identifier for the tool
    - description: Human-readable description for the LLM
    - parameters: List of ToolParameter definitions
    - execute(): Async method that performs the tool action
    """

    # Tool metadata
    name: str
    description: str
    parameters: list[ToolParameter]

    # Security settings
    require_approval: bool = False
    allow_in_sandbox: bool = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the tool with optional configuration."""
        self.config = config or {}

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with the given parameters.

        Args:
            **kwargs: Tool-specific parameters matching the schema.

        Returns:
            ToolResult with the execution outcome.
        """

    def get_schema(self) -> dict[str, Any]:
        """Get the JSON Schema for this tool's parameters."""
        properties = {}
        required = []

        for param in self.parameters:
            properties[param.name] = param.to_json_schema()
            if param.required:
                required.append(param.name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def to_anthropic_format(self) -> dict[str, Any]:
        """Convert tool definition to Anthropic's tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.get_schema(),
        }

    def to_openai_format(self) -> dict[str, Any]:
        """Convert tool definition to OpenAI's function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.get_schema(),
            },
        }

    def to_gemini_format(self) -> dict[str, Any]:
        """Convert tool definition to Google Gemini's format."""
        # Gemini uses a similar format to OpenAI
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.get_schema(),
        }

    def validate_params(self, params: dict[str, Any]) -> tuple[bool, str | None]:
        """Validate parameters against the schema.

        Returns:
            Tuple of (is_valid, error_message).
        """
        schema = self.get_schema()
        required_params = schema.get("required", [])
        properties = schema.get("properties", {})

        # Check required parameters
        for req in required_params:
            if req not in params:
                return False, f"Missing required parameter: {req}"

        # Check parameter types (basic validation)
        for name, value in params.items():
            if name not in properties:
                continue  # Allow extra params

            expected_type = properties[name].get("type")
            if expected_type == "string" and not isinstance(value, str):
                return False, f"Parameter '{name}' must be a string"
            if expected_type == "number" and not isinstance(value, int | float):
                return False, f"Parameter '{name}' must be a number"
            if expected_type == "boolean" and not isinstance(value, bool):
                return False, f"Parameter '{name}' must be a boolean"
            if expected_type == "array" and not isinstance(value, list):
                return False, f"Parameter '{name}' must be an array"
            if expected_type == "object" and not isinstance(value, dict):
                return False, f"Parameter '{name}' must be an object"

            # Check enum values
            if "enum" in properties[name] and value not in properties[name]["enum"]:
                return False, f"Parameter '{name}' must be one of: {properties[name]['enum']}"

        return True, None
