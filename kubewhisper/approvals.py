"""Human-in-the-loop approvals for write tools, persisted across bot restarts.

Pending proposals live in a JSON file (path via KUBEWHISPER_APPROVALS) so an
Approve click still works after the Slack bot is restarted. Proposals older
than KUBEWHISPER_APPROVAL_TTL seconds (default 1h) expire unrun.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

APPROVALS_PATH = os.environ.get("KUBEWHISPER_APPROVALS", "kubewhisper-approvals.json")
APPROVAL_TTL = int(os.environ.get("KUBEWHISPER_APPROVAL_TTL", "3600"))


class ApprovalStore:
    def __init__(self, path: str = APPROVALS_PATH, ttl: int = APPROVAL_TTL):
        self.path = path
        self.ttl = ttl
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, pending: dict[str, dict]) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(pending, fh)
        os.replace(tmp, self.path)

    def _is_live(self, proposal: dict) -> bool:
        return time.time() - proposal["created"] < self.ttl

    def propose(self, name: str, args: dict, ctx: dict) -> str:
        pid = uuid4().hex[:8]
        with self._lock:
            pending = {k: v for k, v in self._load().items() if self._is_live(v)}
            pending[pid] = {"name": name, "args": args, "created": time.time(), **ctx}
            self._save(pending)
        return pid

    def take(self, pid: str) -> dict | None:
        """Remove and return a live proposal; None if unknown, handled, or expired."""
        with self._lock:
            pending = self._load()
            proposal = pending.pop(pid, None)
            self._save(pending)
        if proposal and self._is_live(proposal):
            return proposal
        return None


@dataclass
class Decision:
    outcome: str                # unauthorized | expired | denied | applied
    proposal: dict | None = None
    result: str = ""


def decide(store: ApprovalStore, pid: str, user: str, approve: bool,
           approvers: set[str], run: Callable[[str, dict], str]) -> Decision:
    """Resolve an Approve/Deny click. Only an approved, live proposal is executed."""
    if approvers and user not in approvers:
        return Decision("unauthorized")
    proposal = store.take(pid)
    if not proposal:
        return Decision("expired")
    if not approve:
        return Decision("denied", proposal)
    result = run(proposal["name"], {**proposal["args"], "confirm": True})
    return Decision("applied", proposal, result)
