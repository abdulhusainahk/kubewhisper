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
| `k8s_get_resources` | 🟢 read-only | pods / deployments / services / nodes / events, with status |
| `prometheus_query` | 🟢 read-only | run an instant PromQL query |
| `github_actions_runs` | 🟢 read-only | list recent workflow runs for a repo |
| `scale_deployment` | 🔴 write | scale a Deployment — **disabled by default**, previews first, every call audited |

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
pytest -q          # guardrail tests prove writes are refused unless explicitly enabled
```

CI runs lint + import check + guardrail tests on every push (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

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
