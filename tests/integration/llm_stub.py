"""Scripted OpenAI-compatible chat-completions server for the integration tests.

The agent under test reaches this server through the real `OpenAIClient`
(`LLMProvider.VLLM`), so tool serialisation, the tool-call loop and tool
execution all run production code — only the model's replies are canned.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

_SHUTDOWN_TIMEOUT_SECONDS = 5


def text_turn(content: str) -> dict[str, Any]:
    """Script an assistant reply that ends the turn."""
    return {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}


def tool_call_turn(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Script an assistant reply that invokes a single tool."""
    return {
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        "finish_reason": "tool_calls",
    }


class _ScriptedServer(ThreadingHTTPServer):
    """HTTP server carrying the scripted turns and the requests it has served."""

    turns: list[dict[str, Any]]
    requests: list[dict[str, Any]]


class _ChatCompletionsHandler(BaseHTTPRequestHandler):
    """Serves one scripted turn per request, repeating the final turn."""

    server: _ScriptedServer

    def do_POST(self) -> None:
        if self.path != CHAT_COMPLETIONS_PATH:
            self.send_error(404, "only chat completions are stubbed")
            return

        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(payload)

        if not self.server.turns:
            self._respond(500, {"error": {"message": "no scripted turns configured"}})
            return

        index = min(len(self.server.requests) - 1, len(self.server.turns) - 1)
        self._respond(
            200,
            {
                "id": f"chatcmpl-stub-{index}",
                "object": "chat.completion",
                "created": 1700000000,
                "model": payload.get("model", "stub-model"),
                "choices": [{"index": 0, **self.server.turns[index]}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence the default stderr access log."""


class ScriptedLLMServer:
    """A localhost chat-completions endpoint whose replies the test scripts."""

    def __init__(self) -> None:
        self._server = _ScriptedServer(("127.0.0.1", 0), _ChatCompletionsHandler)
        self._server.turns = []
        self._server.requests = []
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        """Base URL to hand to `AgentConfig.base_url`."""
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    @property
    def requests(self) -> list[dict[str, Any]]:
        """Request payloads received so far, in order."""
        return self._server.requests

    def script(self, turns: list[dict[str, Any]]) -> None:
        """Replace the scripted turns; the last turn repeats once exhausted."""
        self._server.turns[:] = turns

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
