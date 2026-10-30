"""KubeWhisper — an AI-DevOps MCP server (multi-cluster, read-only by default).

Exposes read-only DevOps tools over the Model Context Protocol (MCP) so an LLM
(e.g. Claude) can inspect live environments in natural language — across
multiple clusters — plus a guarded, audited write tool and a Slack notifier.

Routing: every cluster-scoped tool takes an optional `cluster` name. The agent
can call `list_clusters` to see what's available and `resolve_cluster` to pick
the right one from an alert/error string, then target it explicitly.

Run it:  python -m kubewhisper.server
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import clusters
from .audit import record

mcp = FastMCP("kubewhisper")

ALLOW_WRITES = os.environ.get("KUBEWHISPER_ALLOW_WRITES", "false").lower() == "true"


# --------------------------------------------------------------------------- #
# Lazy clients — nothing connects at import time, only when a tool is called.  #
# --------------------------------------------------------------------------- #
def _k8s(context: str | None):
    from kubernetes import client, config

    try:
        config.load_kube_config(context=context)
    except config.ConfigException:
        config.load_incluster_config()
    return client.CoreV1Api(), client.AppsV1Api()


# --------------------------------------------------------------------------- #
# CLUSTER ROUTING TOOLS                                                        #
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_clusters() -> str:
    """List the configured clusters and their routing hints (READ-ONLY).

    Call this first when you're unsure which cluster to target.
    """
    lines = [f"default: {clusters.default_name()}", ""]
    for c in clusters.all_clusters():
        m = c.get("match", {})
        lines.append(
            f"- {c['name']}  (context={c.get('kube_context')}, prom={c.get('prometheus_url')})\n"
            f"    keywords: {', '.join(m.get('keywords', [])) or '-'}\n"
            f"    namespaces: {', '.join(m.get('namespaces', [])) or '-'}"
        )
    return "\n".join(lines)


@mcp.tool()
def resolve_cluster(alert_text: str) -> str:
    """Decide which cluster an alert/error belongs to (READ-ONLY).

    Pass the alert text, error message, or incident description. Returns the
    best-matching cluster name plus why it matched, so you can then target it.

    Args:
        alert_text: the alert/error/incident text to route.
    """
    r = clusters.route(alert_text)
    why = ", ".join(r["_why"]) if r["_why"] else "no hint matched — fell back to default"
    return (f"cluster: {r['name']}\nscore: {r['_score']}\nmatched on: {why}\n"
            f"context: {r.get('kube_context')}\nprometheus: {r.get('prometheus_url')}")


# --------------------------------------------------------------------------- #
# READ-ONLY TOOLS (cluster-aware)                                             #
# --------------------------------------------------------------------------- #
@mcp.tool()
def k8s_get_resources(resource_type: str = "pods", namespace: str = "default",
                      cluster: str = "") -> str:
    """List Kubernetes resources with a concise status summary (READ-ONLY).

    Args:
        resource_type: one of pods | deployments | services | nodes | events.
        namespace: target namespace (ignored for cluster-scoped 'nodes').
        cluster: which configured cluster to target (default cluster if empty).
    """
    c = clusters.get(cluster)
    core, apps = _k8s(c.get("kube_context"))
    rt = resource_type.lower().rstrip("s") + "s"
    lines: list[str] = []

    if rt == "pods":
        for p in core.list_namespaced_pod(namespace).items:
            restarts = sum((cs.restart_count or 0) for cs in (p.status.container_statuses or []))
            ready = sum(1 for cs in (p.status.container_statuses or []) if cs.ready)
            total = len(p.status.container_statuses or [])
            lines.append(f"{p.metadata.name}  {p.status.phase}  ready={ready}/{total}  restarts={restarts}")
    elif rt == "deployments":
        for d in apps.list_namespaced_deployment(namespace).items:
            s = d.status
            lines.append(f"{d.metadata.name}  ready={s.ready_replicas or 0}/{s.replicas or 0}  "
                         f"available={s.available_replicas or 0}")
    elif rt == "services":
        for s in core.list_namespaced_service(namespace).items:
            lines.append(f"{s.metadata.name}  type={s.spec.type}  clusterIP={s.spec.cluster_ip}")
    elif rt == "nodes":
        for n in core.list_node().items:
            conds = {cd.type: cd.status for cd in (n.status.conditions or [])}
            lines.append(f"{n.metadata.name}  Ready={conds.get('Ready', '?')}")
    elif rt == "events":
        for e in core.list_namespaced_event(namespace).items:
            lines.append(f"{e.last_timestamp}  {e.type}  {e.reason}  {e.message}")
    else:
        return f"Unsupported resource_type '{resource_type}'. Use pods|deployments|services|nodes|events."

    if not lines:
        return f"[{c['name']}] No {rt} found in namespace '{namespace}'."
    return f"[{c['name']}] {rt} in '{namespace}' ({len(lines)}):\n" + "\n".join(lines)


@mcp.tool()
def prometheus_query(query: str, cluster: str = "") -> str:
    """Run an instant PromQL query against a cluster's Prometheus (READ-ONLY).

    Args:
        query: a PromQL expression, e.g. 'sum(rate(http_requests_total[5m]))'.
        cluster: which configured cluster's Prometheus to query (default if empty).
    """
    import requests

    c = clusters.get(cluster)
    base = c.get("prometheus_url") or clusters.DEFAULT_PROM
    resp = requests.get(f"{base}/api/v1/query", params={"query": query}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        return f"[{c['name']}] Prometheus error: {data}"
    results = data["data"]["result"]
    if not results:
        return f"[{c['name']}] Query '{query}' returned no series."
    out = [f"[{c['name']}] {query}"]
    for r in results[:50]:
        metric = r.get("metric", {})
        label = metric.get("__name__", "") + str({k: v for k, v in metric.items() if k != "__name__"})
        out.append(f"{label} = {r.get('value', ['', ''])[1]}")
    return "\n".join(out)


@mcp.tool()
def github_actions_runs(repo: str, limit: int = 5) -> str:
    """List the most recent GitHub Actions workflow runs for a repo (READ-ONLY).

    Args:
        repo: 'owner/name', e.g. 'abdulhusainahk/kubewhisper'.
        limit: how many recent runs to return (max 20).

    Requires GITHUB_TOKEN for private repos / higher rate limits.
    """
    import requests

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(f"https://api.github.com/repos/{repo}/actions/runs",
                        params={"per_page": min(max(limit, 1), 20)}, headers=headers, timeout=15)
    resp.raise_for_status()
    runs = resp.json().get("workflow_runs", [])
    if not runs:
        return f"No workflow runs found for {repo}."
    out = [f"Recent runs for {repo}:"]
    for r in runs:
        out.append(f"#{r['run_number']}  {r['name']}  {r['status']}/{r.get('conclusion')}  "
                   f"({r['head_branch']})  {r['created_at']}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# WRITE TOOL — guarded, previewed, audited                                    #
# --------------------------------------------------------------------------- #
@mcp.tool()
def scale_deployment(name: str, replicas: int, namespace: str = "default",
                     cluster: str = "", confirm: bool = False) -> str:
    """Scale a Deployment's replica count (WRITE — human approval required).

    Safety model: disabled unless KUBEWHISPER_ALLOW_WRITES=true; previews unless
    confirm=True; every call is audited (with the target cluster).

    Args:
        name: deployment name.
        replicas: desired replica count (0-100).
        namespace: target namespace.
        cluster: which configured cluster to target (default if empty).
        confirm: must be True to actually apply the change.
    """
    c = clusters.get(cluster)
    intent = {"tool": "scale_deployment", "cluster": c["name"], "name": name,
              "namespace": namespace, "replicas": replicas, "confirm": confirm}

    if not 0 <= replicas <= 100:
        record({**intent, "outcome": "rejected", "reason": "replicas out of range"})
        return "Refused: replicas must be between 0 and 100."
    if not ALLOW_WRITES:
        record({**intent, "outcome": "refused", "reason": "writes disabled"})
        return ("Refused: writes are disabled (read-only mode). "
                "Set KUBEWHISPER_ALLOW_WRITES=true to enable approved writes.")
    if not confirm:
        record({**intent, "outcome": "preview"})
        return (f"PREVIEW ONLY — would scale '{name}' in [{c['name']}]/'{namespace}' to "
                f"{replicas} replicas. Re-call with confirm=True to apply.")

    _, apps = _k8s(c.get("kube_context"))
    apps.patch_namespaced_deployment_scale(name=name, namespace=namespace,
                                            body={"spec": {"replicas": replicas}})
    record({**intent, "outcome": "applied"})
    return f"Applied: '{name}' in [{c['name']}]/'{namespace}' scaled to {replicas} replicas (audited)."


# --------------------------------------------------------------------------- #
# SLACK NOTIFIER (outbound)                                                    #
# --------------------------------------------------------------------------- #
@mcp.tool()
def send_slack_message(text: str, channel: str = "") -> str:
    """Post a message to Slack (e.g. an incident summary or remediation note).

    Uses SLACK_BOT_TOKEN (chat.postMessage) if set, else SLACK_WEBHOOK_URL.

    Args:
        text: the message body (Slack mrkdwn supported).
        channel: target channel id/name (bot-token mode); ignored for webhooks.
    """
    import requests

    token = os.environ.get("SLACK_BOT_TOKEN")
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if token:
        ch = channel or os.environ.get("SLACK_DEFAULT_CHANNEL", "#alerts")
        resp = requests.post("https://slack.com/api/chat.postMessage",
                             headers={"Authorization": f"Bearer {token}"},
                             json={"channel": ch, "text": text}, timeout=15)
        ok = resp.json().get("ok")
        return f"Slack: posted to {ch} (ok={ok})." if ok else f"Slack error: {resp.json()}"
    if webhook:
        resp = requests.post(webhook, json={"text": text}, timeout=15)
        return f"Slack webhook: HTTP {resp.status_code}."
    return "Slack not configured. Set SLACK_BOT_TOKEN or SLACK_WEBHOOK_URL."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
