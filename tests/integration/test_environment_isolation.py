"""The integration fixtures must not leak a resolved GITHUB_TOKEN into the session.

`CredentialResolver.resolve()` writes the token it fetched from Secrets Manager
straight into `os.environ`, which `monkeypatch` never recorded and therefore
cannot undo. Without an explicit teardown the stub token outlives this package
and every later test in a full-suite run sees it.
"""

import os

import pytest

from tests.integration.conftest import GITHUB_TOKEN_VALUE, github_token_isolation

pytestmark = pytest.mark.integration

AMBIENT_TOKEN = "ambient-github-token-not-a-real-credential"


def test_resolved_token_does_not_outlive_the_block(monkeypatch) -> None:
    """A token written inside the block is gone once it exits (the CI case: none set)."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.MonkeyPatch.context() as inner, github_token_isolation(inner):
        os.environ["GITHUB_TOKEN"] = GITHUB_TOKEN_VALUE

    assert "GITHUB_TOKEN" not in os.environ


def test_ambient_token_is_restored_after_the_block(monkeypatch) -> None:
    """A developer's own token is hidden during the block and restored after it."""
    monkeypatch.setenv("GITHUB_TOKEN", AMBIENT_TOKEN)

    with pytest.MonkeyPatch.context() as inner, github_token_isolation(inner):
        assert "GITHUB_TOKEN" not in os.environ
        os.environ["GITHUB_TOKEN"] = GITHUB_TOKEN_VALUE

    assert os.environ["GITHUB_TOKEN"] == AMBIENT_TOKEN
