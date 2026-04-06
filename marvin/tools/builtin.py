"""Built-in tools for the agent system."""

import datetime
import logging
from typing import Any

import httpx

from marvin.settings import ZAATAR_SEARCH_API_URL
from marvin.tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class DateTimeTool(BaseTool):
    """Tool for getting current date and time information."""

    name = "get_datetime"
    description = "Get the current date and time, optionally in a specific timezone."
    parameters = [
        ToolParameter(
            name="timezone",
            type="string",
            description="Timezone name (e.g., 'UTC', 'America/New_York'). Defaults to UTC.",
            required=False,
            default="UTC",
        ),
        ToolParameter(
            name="output_format",
            type="string",
            description="Output format: 'iso' for ISO 8601, 'human' for human-readable.",
            required=False,
            default="iso",
            enum=["iso", "human"],
        ),
    ]

    async def execute(self, timezone: str = "UTC", output_format: str = "iso") -> ToolResult:
        """Get current datetime."""
        try:
            import zoneinfo  # noqa: PLC0415

            tz = zoneinfo.ZoneInfo(timezone)
        except (KeyError, zoneinfo.ZoneInfoNotFoundError):
            tz = datetime.UTC

        now = datetime.datetime.now(tz)

        if output_format == "human":
            output = now.strftime("%A, %B %d, %Y at %I:%M %p %Z")
        else:
            output = now.isoformat()

        return ToolResult.success(
            output=output,
            data={
                "timestamp": now.timestamp(),
                "timezone": str(tz),
                "year": now.year,
                "month": now.month,
                "day": now.day,
                "hour": now.hour,
                "minute": now.minute,
            },
        )


class CalculatorTool(BaseTool):
    """Tool for performing basic mathematical calculations."""

    name = "calculator"
    description = "Perform a mathematical calculation. Supports basic arithmetic operations."
    parameters = [
        ToolParameter(
            name="expression",
            type="string",
            description="Mathematical expression to evaluate (e.g., '2 + 2', '10 * 5', '100 / 4').",
            required=True,
        ),
    ]

    # Allowed operations for safety
    ALLOWED_CHARS = set("0123456789+-*/.() ")

    async def execute(self, expression: str) -> ToolResult:
        """Evaluate a mathematical expression."""
        # Security: Only allow safe characters
        if not all(c in self.ALLOWED_CHARS for c in expression):
            return ToolResult.from_error("Expression contains invalid characters")

        try:
            # Use eval with restricted builtins for safety
            result = eval(expression, {"__builtins__": {}}, {})  # noqa: S307
            return ToolResult.success(
                output=str(result),
                data={"expression": expression, "result": result},
            )
        except (SyntaxError, NameError, TypeError, ZeroDivisionError) as e:
            return ToolResult.from_error(f"Calculation error: {e}")


class WebSearchTool(BaseTool):
    """Tool for searching the web (placeholder implementation)."""

    name = "web_search"
    description = "Search the web for information on a given query."
    parameters = [
        ToolParameter(
            name="query",
            type="string",
            description="The search query.",
            required=True,
        ),
        ToolParameter(
            name="num_results",
            type="number",
            description="Number of results to return (1-10).",
            required=False,
            default=5,
        ),
    ]

    async def execute(self, query: str, num_results: int = 5) -> ToolResult:
        """Search the web using the configured zaatar-search-api instance."""
        if not ZAATAR_SEARCH_API_URL:
            return ToolResult.from_error("WebSearchTool not configured: set ZAATAR_SEARCH_API_URL env var")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{ZAATAR_SEARCH_API_URL.rstrip('/')}/web_search",
                    params={"query": query, "count": num_results},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            return ToolResult.from_error(f"Search API request failed: {exc}")

        raw_results = data.get("web", {}).get("results", [])
        results = [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in raw_results
        ]
        return ToolResult.success(
            output="\n".join(f"{r['title']}: {r['url']} — {r['snippet']}" for r in results),
            data={"query": query, "results": results, "count": len(results)},
        )


class BrowserTool(BaseTool):
    """Tool for fetching web page content."""

    name = "browser_fetch"
    description = "Fetch the content of a web page by URL."
    parameters = [
        ToolParameter(
            name="url",
            type="string",
            description="The URL of the web page to fetch.",
            required=True,
        ),
    ]

    async def execute(self, url: str) -> ToolResult:
        """Fetch content from a URL."""
        import urllib.request  # noqa: PLC0415

        try:
            with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
                content = response.read().decode("utf-8", errors="replace")
            return ToolResult.success(
                output=content,
                data={"url": url, "length": len(content)},
            )
        except Exception as exc:
            return ToolResult.from_error(f"Failed to fetch URL: {exc}")


class MemoryStoreTool(BaseTool):
    """Tool for storing information in the conversation memory."""

    name = "memory_store"
    description = "Store a piece of information in memory for later retrieval."
    parameters = [
        ToolParameter(
            name="key",
            type="string",
            description="A unique key to identify this information.",
            required=True,
        ),
        ToolParameter(
            name="value",
            type="string",
            description="The information to store.",
            required=True,
        ),
    ]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with a memory store."""
        super().__init__(config)
        self._memory: dict[str, str] = {}

    async def execute(self, key: str, value: str) -> ToolResult:
        """Store a value in memory."""
        self._memory[key] = value
        return ToolResult.success(
            output=f"Stored '{key}' in memory.",
            data={"key": key, "stored": True},
        )


class MemoryRetrieveTool(BaseTool):
    """Tool for retrieving information from conversation memory."""

    name = "memory_retrieve"
    description = "Retrieve a previously stored piece of information from memory."
    parameters = [
        ToolParameter(
            name="key",
            type="string",
            description="The key of the information to retrieve.",
            required=True,
        ),
    ]

    def __init__(self, config: dict[str, Any] | None = None, memory: dict[str, str] | None = None) -> None:
        """Initialize with a shared memory store."""
        super().__init__(config)
        self._memory = memory or {}

    async def execute(self, key: str) -> ToolResult:
        """Retrieve a value from memory."""
        if key not in self._memory:
            return ToolResult.from_error(f"Key '{key}' not found in memory.")

        value = self._memory[key]
        return ToolResult.success(
            output=value,
            data={"key": key, "found": True},
        )


class MemorySearchTool(BaseTool):
    """Tool for searching conversation memory using vector + text hybrid search.

    In stateless mode (no database), this tool returns an empty result.
    """

    name = "memory_search"
    description = "Search through past conversation history and summaries to find relevant information."
    parameters = [
        ToolParameter(
            name="query",
            type="string",
            description="The search query to find relevant memories.",
            required=True,
        ),
        ToolParameter(
            name="search_type",
            type="string",
            description="Type of search: 'hybrid' (combines vector and text), 'vector' (semantic), 'text' (keyword).",
            required=False,
            default="hybrid",
            enum=["hybrid", "vector", "text"],
        ),
        ToolParameter(
            name="max_results",
            type="number",
            description="Maximum number of results to return (1-10).",
            required=False,
            default=5,
        ),
    ]

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        """Initialize with optional session context.

        Args:
            config: Tool configuration.
            session_id: Optional session ID (unused in stateless mode).
        """
        super().__init__(config)
        self.session_id = session_id

    async def execute(
        self,
        query: str,
        search_type: str = "hybrid",
        max_results: int = 5,
    ) -> ToolResult:
        """Search conversation memory.

        In stateless mode, returns an empty result without accessing any database.
        """
        return ToolResult.success(
            output="Memory search not available in stateless mode.",
            data={"query": query, "results": [], "count": 0},
        )


def register_builtin_tools(
    registry: Any,
    session_id: str | None = None,
) -> None:
    """Register all built-in tools with the given registry.

    Registers core tools plus all coding tools (file I/O, shell execution,
    web access, image analysis, browser automation, and sub-agent sessions).

    Args:
        registry: The ToolRegistry instance.
        session_id: Optional session ID for memory search context.
    """
    from marvin.tools.coding import register_coding_tools  # noqa: PLC0415

    core_tools = [
        DateTimeTool(),
        CalculatorTool(),
        WebSearchTool(),
        BrowserTool(),
        MemoryStoreTool(),
        MemoryRetrieveTool(),
        MemorySearchTool(session_id=session_id),
    ]

    for tool in core_tools:
        try:
            registry.register(tool)
        except ValueError:
            logger.debug("Tool %s already registered", tool.name)

    register_coding_tools(registry)
