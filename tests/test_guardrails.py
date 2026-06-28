"""Guardrail tests — prove the agent refuses unsafe / unapproved writes.

These run without a cluster: they exercise the safety logic, not kubectl.
"""
import importlib
import os

import kubewhisper.server as server


def _callable(s):
    # Across MCP SDK versions the decorator returns either the function itself
    # or a Tool wrapper exposing `.fn` — handle both.
    return getattr(s.scale_deployment, "fn", s.scale_deployment)


def reload_with(allow_writes: str):
    os.environ["KUBEWHISPER_ALLOW_WRITES"] = allow_writes
    return importlib.reload(server)


def test_writes_disabled_by_default():
    s = reload_with("false")
    out = _callable(s)(name="web", replicas=3)
    assert "Refused" in out and "read-only" in out


def test_preview_when_writes_enabled_but_not_confirmed():
    s = reload_with("true")
    out = _callable(s)(name="web", replicas=3, confirm=False)
    assert "PREVIEW ONLY" in out


def test_replica_bounds_enforced():
    s = reload_with("true")
    out = _callable(s)(name="web", replicas=999, confirm=True)
    assert "Refused" in out


def teardown_module(_):
    os.environ["KUBEWHISPER_ALLOW_WRITES"] = "false"
