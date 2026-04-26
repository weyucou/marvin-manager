"""Tests for entrypoint.py — one-shot Fargate runner."""

import json
from unittest.mock import AsyncMock, patch

import entrypoint
from marvin.context import CustomerContextBundle, MemoryEntry


def _make_envelope_dict(**kwargs) -> dict:
    defaults = {
        "task_id": "task-001",
        "customer_id": "cust-001",
        "session_id": "sess-001",
        "agent": {
            "name": "test-agent",
            "provider": "anthropic",
            "model_name": "claude-sonnet-4-20250514",
        },
        "s3_context_prefix": "s3://bucket/customers/cust-001/projects/repo",
        "user_message": "Hello, agent!",
    }
    defaults.update(kwargs)
    return defaults


def _make_context_bundle() -> CustomerContextBundle:
    return CustomerContextBundle(
        customer_id="cust-001",
        claude_md="# CLAUDE.md",
        sops={},
        project_goals="# Goals",
        memory_index="",
        daily_memories=[],
    )


class TestMainExitCodes:
    def test_missing_env_var_returns_2(self, monkeypatch) -> None:
        monkeypatch.delenv("TASK_ENVELOPE_JSON", raising=False)
        assert entrypoint.main() == 2

    def test_malformed_json_returns_2(self, monkeypatch) -> None:
        monkeypatch.setenv("TASK_ENVELOPE_JSON", "not-valid-json{{{")
        assert entrypoint.main() == 2

    def test_invalid_envelope_schema_returns_2(self, monkeypatch) -> None:
        monkeypatch.setenv("TASK_ENVELOPE_JSON", json.dumps({"task_id": "x"}))
        assert entrypoint.main() == 2

    def test_credential_resolve_failure_returns_2(self, monkeypatch) -> None:
        monkeypatch.setenv("TASK_ENVELOPE_JSON", json.dumps(_make_envelope_dict()))
        with patch("entrypoint.CredentialResolver") as mock_resolver_cls:
            mock_resolver_cls.return_value.resolve.side_effect = RuntimeError("Secret fetch failed")
            assert entrypoint.main() == 2

    def test_context_pull_failure_returns_2(self, monkeypatch) -> None:
        monkeypatch.setenv("TASK_ENVELOPE_JSON", json.dumps(_make_envelope_dict()))
        with (
            patch("entrypoint.CredentialResolver"),
            patch("entrypoint.ContextBundleService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.pull.side_effect = Exception("S3 unreachable")
            assert entrypoint.main() == 2

    def test_agent_run_failure_returns_1(self, monkeypatch) -> None:
        monkeypatch.setenv("TASK_ENVELOPE_JSON", json.dumps(_make_envelope_dict()))
        with (
            patch("entrypoint.CredentialResolver"),
            patch("entrypoint.ContextBundleService") as mock_svc_cls,
            patch("entrypoint.AgentRunner") as mock_runner_cls,
        ):
            mock_svc_cls.return_value.pull.return_value = _make_context_bundle()
            mock_runner_cls.return_value.chat = AsyncMock(side_effect=RuntimeError("LLM timeout"))
            assert entrypoint.main() == 1

    def test_success_returns_0(self, monkeypatch) -> None:
        monkeypatch.setenv("TASK_ENVELOPE_JSON", json.dumps(_make_envelope_dict()))
        with (
            patch("entrypoint.CredentialResolver"),
            patch("entrypoint.ContextBundleService") as mock_svc_cls,
            patch("entrypoint.AgentRunner") as mock_runner_cls,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.pull.return_value = _make_context_bundle()
            mock_svc.push_memory.return_value = None
            mock_runner_cls.return_value.chat = AsyncMock(return_value=("Agent reply", []))
            assert entrypoint.main() == 0

    def test_memory_write_failure_still_returns_0(self, monkeypatch) -> None:
        monkeypatch.setenv("TASK_ENVELOPE_JSON", json.dumps(_make_envelope_dict()))
        with (
            patch("entrypoint.CredentialResolver"),
            patch("entrypoint.ContextBundleService") as mock_svc_cls,
            patch("entrypoint.AgentRunner") as mock_runner_cls,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.pull.return_value = _make_context_bundle()
            mock_svc.push_memory.side_effect = Exception("S3 write failed")
            mock_runner_cls.return_value.chat = AsyncMock(return_value=("Agent reply", []))
            assert entrypoint.main() == 0


class TestMainContextBundle:
    def test_context_bundle_passed_to_runner(self, monkeypatch) -> None:
        monkeypatch.setenv("TASK_ENVELOPE_JSON", json.dumps(_make_envelope_dict()))
        bundle = _make_context_bundle()
        with (
            patch("entrypoint.CredentialResolver"),
            patch("entrypoint.ContextBundleService") as mock_svc_cls,
            patch("entrypoint.AgentRunner") as mock_runner_cls,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.pull.return_value = bundle
            mock_svc.push_memory.return_value = None
            mock_runner_cls.return_value.chat = AsyncMock(return_value=("reply", []))
            entrypoint.main()
            _, kwargs = mock_runner_cls.call_args
            assert kwargs["context_bundle"] is bundle

    def test_memory_entry_written_with_task_id(self, monkeypatch) -> None:
        envelope_dict = _make_envelope_dict(task_id="task-abc")
        monkeypatch.setenv("TASK_ENVELOPE_JSON", json.dumps(envelope_dict))
        with (
            patch("entrypoint.CredentialResolver"),
            patch("entrypoint.ContextBundleService") as mock_svc_cls,
            patch("entrypoint.AgentRunner") as mock_runner_cls,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.pull.return_value = _make_context_bundle()
            mock_svc.push_memory.return_value = None
            mock_runner_cls.return_value.chat = AsyncMock(return_value=("summary text", []))
            entrypoint.main()
            assert mock_svc.push_memory.call_count == 1
            _, kwargs = mock_svc.push_memory.call_args
            entry: MemoryEntry = mock_svc.push_memory.call_args[0][1]
            assert "task-abc" in entry.content


class TestScrubSensitiveFields:
    def test_scrubs_known_sensitive_keys(self) -> None:
        event_dict = {"event": "test", "token": "secret-value", "password": "hunter2"}
        result = entrypoint._scrub_sensitive_fields(None, "info", event_dict)
        assert result["token"] == "[REDACTED]"
        assert result["password"] == "[REDACTED]"

    def test_preserves_non_sensitive_keys(self) -> None:
        event_dict = {"event": "test", "step": "parse", "status": "ok"}
        result = entrypoint._scrub_sensitive_fields(None, "info", event_dict)
        assert result["step"] == "parse"
        assert result["status"] == "ok"
