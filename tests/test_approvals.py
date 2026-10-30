"""Approval-flow tests — prove a write runs only after an authorized, live approval.

These run without Slack or a cluster: `run` is a recorder, not kubectl.
"""
import time

from kubewhisper.approvals import ApprovalStore, decide

CTX = {"channel": "C1", "thread_ts": "1.0"}


def _store(tmp_path, ttl=3600):
    return ApprovalStore(path=str(tmp_path / "approvals.json"), ttl=ttl)


def _recorder():
    calls = []
    return calls, lambda name, args: calls.append((name, args)) or "ok"


def test_proposal_survives_restart(tmp_path):
    pid = _store(tmp_path).propose("scale_deployment", {"name": "web", "replicas": 3}, CTX)
    calls, run = _recorder()
    d = decide(_store(tmp_path), pid, "U1", True, set(), run)
    assert d.outcome == "applied"
    assert calls == [("scale_deployment", {"name": "web", "replicas": 3, "confirm": True})]


def test_deny_never_runs(tmp_path):
    store = _store(tmp_path)
    pid = store.propose("scale_deployment", {"name": "web", "replicas": 3}, CTX)
    calls, run = _recorder()
    assert decide(store, pid, "U1", False, set(), run).outcome == "denied"
    assert calls == []


def test_approval_is_single_use(tmp_path):
    store = _store(tmp_path)
    pid = store.propose("scale_deployment", {"name": "web", "replicas": 3}, CTX)
    calls, run = _recorder()
    decide(store, pid, "U1", True, set(), run)
    assert decide(store, pid, "U1", True, set(), run).outcome == "expired"
    assert len(calls) == 1


def test_unauthorized_user_cannot_approve_or_consume(tmp_path):
    store = _store(tmp_path)
    pid = store.propose("scale_deployment", {"name": "web", "replicas": 3}, CTX)
    calls, run = _recorder()
    assert decide(store, pid, "U_INTRUDER", True, {"U_OPS"}, run).outcome == "unauthorized"
    assert calls == []
    assert decide(store, pid, "U_OPS", True, {"U_OPS"}, run).outcome == "applied"


def test_expired_proposal_does_not_run(tmp_path, monkeypatch):
    store = _store(tmp_path, ttl=60)
    pid = store.propose("scale_deployment", {"name": "web", "replicas": 3}, CTX)
    monkeypatch.setattr(time, "time", lambda: 10**12)
    calls, run = _recorder()
    assert decide(store, pid, "U1", True, set(), run).outcome == "expired"
    assert calls == []
