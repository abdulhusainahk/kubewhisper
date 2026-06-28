"""Multi-cluster registry + error-context routing.

A `clusters.json` (path via KUBEWHISPER_CLUSTERS, default ./clusters.json) maps
named clusters to a kubeconfig context, a Prometheus URL, and routing hints.
Given an alert/error string, `route()` picks the most likely cluster so the
agent can target the right environment automatically.

If no clusters.json is present, the registry falls back to a single implicit
cluster using the current kubeconfig context and the PROMETHEUS_URL env var.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

CLUSTERS_PATH = os.environ.get("KUBEWHISPER_CLUSTERS", "clusters.json")
DEFAULT_PROM = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")


@lru_cache(maxsize=1)
def _config() -> dict:
    if os.path.exists(CLUSTERS_PATH):
        with open(CLUSTERS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    # Fallback: single implicit cluster = current kube context.
    return {
        "default_cluster": "current-context",
        "clusters": [
            {
                "name": "current-context",
                "kube_context": None,
                "prometheus_url": DEFAULT_PROM,
                "match": {"keywords": [], "namespaces": []},
            }
        ],
    }


def all_clusters() -> list[dict]:
    return _config()["clusters"]


def default_name() -> str:
    return _config().get("default_cluster") or all_clusters()[0]["name"]


def get(name: str | None) -> dict:
    """Resolve a cluster by name (or the default when name is empty/None)."""
    name = name or default_name()
    for c in all_clusters():
        if c["name"] == name:
            return c
    raise KeyError(f"Unknown cluster '{name}'. Known: {[c['name'] for c in all_clusters()]}")


def route(alert_text: str) -> dict:
    """Pick the best-matching cluster for a free-text alert/error.

    Scores each cluster by how many of its keyword/namespace hints appear in the
    text (case-insensitive, substring). Returns the winning cluster dict plus a
    `_score` and `_why`. Ties fall back to the default cluster.
    """
    text = (alert_text or "").lower()
    best, best_score, best_hits = None, -1, []
    for c in all_clusters():
        m = c.get("match", {})
        hints = [str(h).lower() for h in (m.get("keywords", []) + m.get("namespaces", []))]
        hits = [h for h in hints if h and h in text]
        score = len(hits)
        if score > best_score:
            best, best_score, best_hits = c, score, hits
    if best is None or best_score <= 0:
        best = get(default_name())
        best_hits = []
    return {**best, "_score": max(best_score, 0), "_why": best_hits}
