"""Append-only JSON audit log for every state-changing intent.

Each entry is one JSON object per line (JSONL) so the trail is greppable and
easy to ship to Loki/ELK later. Timestamps are UTC ISO-8601.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

AUDIT_PATH = os.environ.get("KUBEWHISPER_AUDIT_LOG", "kubewhisper-audit.jsonl")


def record(entry: dict) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
    with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
