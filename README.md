# 🗣️ KubeWhisper

**An AI-DevOps MCP server — talk to your infrastructure in plain English, safely.**

KubeWhisper is a [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that
gives an LLM like **Claude** *read-only-by-default* access to a live environment, so you can ask:

> *"Which pods are crash-looping in `staging`?"*
> *"What's the p95 latency right now?"*
> *"Did the last GitHub Actions run on `main` pass?"*
> *"Scale the `web` deployment to 5 — but show me the preview first."*

…and the model answers grounded in your **actual** cluster, metrics, and CI — not generic guesses.

MCP crossed **~97M monthly SDK downloads in March 2026** and is now adopted by Anthropic, OpenAI,
Google, Microsoft and Amazon. Plenty of people *use* MCP servers; far fewer have *built* one for
real DevOps work. This is a small, honest, safety-first example of how.

---

## ✨ Tools

| Tool | Mode | What it does |
|------|------|--------------|
| `list_clusters` | 🟢 read-only | list configured clusters + their routing hints |
| `resolve_cluster` | 🟢 read-only | decide which cluster an alert/error belongs to |
| `k8s_get_resources` | 🟢 read-only | pods / deployments / services / nodes / events, with status |
| `prometheus_query` | 🟢 read-only | run an instant PromQL query |
| `github_actions_runs` | 🟢 read-only | list recent workflow runs for a repo |
| `scale_deployment` | 🔴 write | scale a Deployment — **disabled by default**, previews first, every call audited |
| `send_slack_message` | 🟡 outbound | post an incident/remediation note to Slack |

## 🌐 Multi-cluster + error-context routing

Drop a `clusters.json` next to the server (or point `KUBEWHISPER_CLUSTERS` at it).
Each cluster maps to a kubeconfig context, a Prometheus URL, and routing hints:

```json
{
  "default_cluster": "prod-mumbai",
  "clusters": [
    { "name": "prod-mumbai", "kube_context": "gke_proj_asia-south1_prod",
      "prometheus_url": "http://prometheus.prod-mumbai:9090",
      "match": { "keywords": ["prod","mumbai","live","p1"], "namespaces": ["streaming","ingest"] } },
    { "name": "staging", "kube_context": "gke_proj_asia-south1_staging",
      "prometheus_url": "http://prometheus.staging:9090",
      "match": { "keywords": ["staging","qa"], "namespaces": ["streaming-staging"] } }
  ]
}
```

Now the agent can route by the **error context**: given an alert like
*"P1: transcode pods OOMing on the Mumbai live stream"*, it calls `resolve_cluster`,
matches `mumbai` / `live` / `transcode` → `prod-mumbai`, and targets that cluster's
kube context + Prometheus automatically. Every cluster-scoped tool also accepts an
explicit `cluster` argument. With no `clusters.json`, it falls back to your current
kube context. See [`clusters.example.json`](clusters.example.json).

## 💬 Slack bot

Two ways to use Slack:

1. **Outbound notes from any MCP client** — the `send_slack_message` tool (set
   `SLACK_BOT_TOKEN` or `SLACK_WEBHOOK_URL`).
2. **A full Slack bot** ([`slack_bridge.py`](slack_bridge.py)) — @mention it in a
   channel (*"@KubeWhisper which pods are failing in prod?"*) and it runs a Claude
   tool-use loop over these tools, routes to the right cluster, and replies in-thread.

```bash
pip install -r requirements-slack.txt
export ANTHROPIC_API_KEY=...      # console.anthropic.com
export SLACK_BOT_TOKEN=xoxb-...   # scopes: app_mentions:read, chat:write
export SLACK_APP_TOKEN=xapp-...   # Socket Mode
python slack_bridge.py
```

The bot is read-only by default — `scale_deployment` stays preview-only unless an
operator sets `KUBEWHISPER_ALLOW_WRITES=true` on the bot process. Even then, the model
never executes a write: it posts **Approve / Deny** buttons in the thread, and the change
runs only when a human approves.

- `KUBEWHISPER_APPROVERS=U0123,U0456` limits who may approve (Slack user IDs). Clicks from
  anyone else are rejected without consuming the request.
- Pending approvals are stored in `KUBEWHISPER_APPROVALS` (default
  `kubewhisper-approvals.json`), so a bot restart doesn't lose them.
- Each approval is single-use and expires after `KUBEWHISPER_APPROVAL_TTL` seconds
  (default 3600).

## 🔐 Safety model (the whole point)

1. **Read-only by default.** The single write tool is off unless `KUBEWHISPER_ALLOW_WRITES=true`.
2. **Preview, then confirm.** A write with `confirm=False` only *describes* the change.
3. **Audit trail.** Every write intent — refused, previewed, or applied — is appended to a JSONL log.
4. **No destructive verbs.** `delete` / `drain` / `cordon` are deliberately not implemented in v0.

See [`docs/architecture.md`](docs/architecture.md) for the diagram.

---

## 🚀 Quickstart

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

# 2. Point at a cluster + Prometheus (a local `kind` cluster is perfect)
export PROMETHEUS_URL=http://localhost:9090
#   KUBEWHISPER_ALLOW_WRITES stays unset → read-only

# 3. Run it
python -m kubewhisper.server
```

### Use it from Claude Desktop

Copy the block in [`config.example.json`](config.example.json) into your
`claude_desktop_config.json`, fix the `cwd` path, restart Claude Desktop, and ask it about your cluster.

---

## 🧪 Tests

```bash
pip install pytest
pytest -q          # guardrail + approval-flow tests; no cluster or Slack needed
```

The guardrail tests prove writes are refused unless explicitly enabled and confirmed; the
approval tests prove a write runs only after a live, authorized, single-use approval.

CI runs lint + import check + tests on every push (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

---

## 🗺️ Roadmap

- **v0.1** — read-only tools + guarded scale + audit *(this release)*
- **v0.2** — Terraform `plan` (read-only) tool + deploy "what-changed" diff
- **v0.3** — eval suite proving destructive-op refusal + OPA policy layer over writes
- **v0.4** — one-command `docker run`, publish to an MCP registry

---

## ⚠️ Disclaimer

A learning / portfolio project. Run it against clusters you own. Keep it in read-only mode unless
you fully understand the write path. Built independently, on personal infrastructure.

## 📄 License

MIT — see [LICENSE](LICENSE).

---

Built by **Abdulhussain Kanchwala** · [Portfolio](https://abdulhussaink.netlify.app) ·
[LinkedIn](https://www.linkedin.com/in/abdulhussain-kanchwala-3b50b0188) ·
[GitHub](https://github.com/abdulhusainahk)
