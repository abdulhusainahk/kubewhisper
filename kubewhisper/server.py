"""KubeWhisper — an AI-DevOps MCP server.

Exposes a small set of *read-only-by-default* DevOps tools over the Model
Context Protocol (MCP) so an LLM (e.g. Claude) can inspect a live environment
in natural language — "which pods are failing?", "what's the p95 latency?",
"did the last GitHub Actions run pass?" — without ever taking a destructive
action unless a human explicitly opts in.

Design principles
-----------------
1. Read-only by default. The only write tool (`scale_deployment`) is disabled
   unless KUBEWHISPER_ALLOW_WRITES=true, previews by default, and requires an
   explicit `confirm=True` to actually mutate state.
2. Every write attempt — allowed, previewed, or refused — is appended to a JSON
   audit log (see audit.py).
3. Destructive verbs (delete/drain/cordon) are not implemented at all in v0.

Run it:  python -m kubewhisper.server
"""
from __future__ import annotations

import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .audit import record

mcp = FastMCP("kubewhisper")

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
ALLOW_WRITES = os.environ.get("KUBEWHISPER_ALLOW_WRITES", "false").lower() == "true"


# --------------------------------------------------------------------------- #
# Lazy clients — nothing connects at import time, only when a tool is called.  #
# --------------------------------------------------------------------------- #
def _k8s():
    from kubernetes import client, config

    try:
        config.load_kube_config()
    except Exception:
        config.load_incluster_config()
    return client.CoreV1Api(), client.AppsV1Api()


# --------------------------------------------------------------------------- #
# READ-ONLY TOOLS                                                             #
# --------------------------------------------------------------------------- #
@mcp.tool()
def k8s_get_resources(resource_type: str = "pods", namespace: str = "default") -> str:
    """List Kubernetes resources with a concise status summary (READ-ONLY).

    Args:
        resource_type: one of pods | deployments | services | nodes | events.
        namespace: target namespace (ignored for cluster-scoped 'nodes').

    Returns a human-readable summary the model can reason over.
    """
    core, apps = _k8s()
    rt = resource_type.lower().rstrip("s") + "s"
    lines: list[str] = []

    if rt == "pods":
        for p in core.list_namespaced_pod(namespace).items:
            phase = p.status.phase
            restarts = sum((c.restart_count or 0) for c in (p.status.container_statuses or []))
            ready = sum(1 for c in (p.status.container_statuses or []) if c.ready)
            total = len(p.status.container_statuses or [])
            lines.append(f"{p.metadata.name}  {phase}  ready={ready}/{total}  restarts={restarts}")
    elif rt == "deployments":
        for d in apps.list_namespaced_deployment(namespace).items:
            s = d.status
            lines.append(
                f"{d.metadata.name}  ready={s.ready_replicas or 0}/{s.replicas or 0}  "
                f"updated={s.updated_replicas or 0}  available={s.available_replicas or 0}"
            )
    elif rt == "services":
        for s in core.list_namespaced_service(namespace).items:
            lines.append(f"{s.metadata.name}  type={s.spec.type}  clusterIP={s.spec.cluster_ip}")
    elif rt == "nodes":
        for n in core.list_node().items:
            conds = {c.type: c.status for c in (n.status.conditions or [])}
            lines.append(f"{n.metadata.name}  Ready={conds.get('Ready', '?')}")
    elif rt == "events":
        for e in core.list_namespaced_event(namespace).items:
            lines.append(f"{e.last_timestamp}  {e.type}  {e.reason}  {e.message}")
    else:
        return f"Unsupported resource_type '{resource_type}'. Use pods|deployments|services|nodes|events."

    if not lines:
        return f"No {rt} found in namespace '{namespace}'."
    return f"{rt} in '{namespace}' ({len(lines)}):\n" + "\n".join(lines)


@mcp.tool()
def prometheus_query(query: str) -> str:
    """Run an instant PromQL query against Prometheus (READ-ONLY).

    Args:
        query: a PromQL expression, e.g. 'sum(rate(http_requests_total[5m]))'.
    """
    import requests

    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        return f"Prometheus error: {data}"
    results = data["data"]["result"]
    if not results:
        return f"Query '{query}' returned no series."
    out = []
    for r in results[:50]:
        metric = r.get("metric", {})
        label = metric.get("__name__", "") + str({k: v for k, v in metric.items() if k != "__name__"})
        value = r.get("value", ["", ""])[1]
        out.append(f"{label} = {value}")
    return "\n".join(out)


@mcp.tool()
def github_actions_runs(repo: str, limit: int = 5) -> str:
    """List the most recent GitHub Actions workflow runs for a repo (READ-ONLY).

    Args:
        repo: 'owner/name', e.g. 'abdulhusainahk/kubewhisper'.
        limit: how many recent runs to return (max 20).

    Requires a GITHUB_TOKEN env var for private repos / higher rate limits.
    """
    import requests

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/actions/runs",
        params={"per_page": min(max(limit, 1), 20)},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    runs = resp.json().get("workflow_runs", [])
    if not runs:
        return f"No workflow runs found for {repo}."
    out = [f"Recent runs for {repo}:"]
    for r in runs:
        out.append(
            f"#{r['run_number']}  {r['name']}  {r['status']}/{r.get('conclusion')}  "
            f"({r['head_branch']})  {r['created_at']}"
        )
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# WRITE TOOL — guarded, previewed, audited                                    #
# --------------------------------------------------------------------------- #
@mcp.tool()
def scale_deployment(
    name: str,
    replicas: int,
    namespace: str = "default",
    confirm: bool = False,
) -> str:
    """Scale a Deployment's replica count (WRITE — human approval required).

    Safety model:
      * Disabled entirely unless KUBEWHISPER_ALLOW_WRITES=true.
      * With confirm=False (default) it only PREVIEWS the change.
      * With confirm=True it applies the change.
      * Every call is recorded to the audit log regardless of outcome.

    Args:
        name: deployment name.
        replicas: desired replica count (0-100).
        namespace: target namespace.
        confirm: must be True to actually apply the change.
    """
    intent = {"tool": "scale_deployment", "name": name, "namespace": namespace,
              "replicas": replicas, "confirm": confirm}

    if not 0 <= replicas <= 100:
        record({**intent, "outcome": "rejected", "reason": "replicas out of range"})
        return "Refused: replicas must be between 0 and 100."

    if not ALLOW_WRITES:
        record({**intent, "outcome": "refused", "reason": "writes disabled"})
        return ("Refused: writes are disabled. This server is running in read-only mode. "
                "Set KUBEWHISPER_ALLOW_WRITES=true to enable approved writes.")

    if not confirm:
        record({**intent, "outcome": "preview"})
        return (f"PREVIEW ONLY — would scale deployment '{name}' in '{namespace}' to {replicas} "
                f"replicas. Re-call with confirm=True to apply.")

    _, apps = _k8s()
    apps.patch_namespaced_deployment_scale(
        name=name, namespace=namespace, body={"spec": {"replicas": replicas}}
    )
    record({**intent, "outcome": "applied"})
    return f"Applied: deployment '{name}' in '{namespace}' scaled to {replicas} replicas (audited)."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
