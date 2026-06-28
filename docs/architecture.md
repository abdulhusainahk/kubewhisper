# KubeWhisper — Architecture

```
                +-------------------+
                |   Claude (LLM)    |
                | Desktop / Code    |
                +---------+---------+
                          | MCP (stdio / JSON-RPC)
                          v
                +-------------------+
                | KubeWhisper MCP   |
                |     server.py     |
                +----+----+----+----+
        read-only |    |    |    | guarded write
         +--------+    |    |    +-----------+
         v             v    v                v
   +-----------+ +----------+ +-----------+ +------------------+
   | Kubernetes| |Prometheus| |  GitHub   | | scale_deployment |
   |  API (RO) | | HTTP API | | Actions   | | (approval gate + |
   |           | |          | |  API (RO) | |   audit log)     |
   +-----------+ +----------+ +-----------+ +------------------+
```

## Tools

| Tool | Mode | What it does |
|------|------|--------------|
| `k8s_get_resources` | read-only | pods / deployments / services / nodes / events with status |
| `prometheus_query` | read-only | instant PromQL query |
| `github_actions_runs` | read-only | recent workflow runs for a repo |
| `scale_deployment` | **write** | scales a Deployment — disabled unless opted in, previews by default, audited |

## Safety model

1. **Read-only by default.** Writes require `KUBEWHISPER_ALLOW_WRITES=true`.
2. **Preview-then-confirm.** A write call with `confirm=False` only describes the change.
3. **Audit trail.** Every write *intent* (refused, previewed, applied) is appended to a JSONL audit log.
4. **No destructive verbs in v0.** delete / drain / cordon are intentionally not implemented.

## Roadmap

- v0.1 — read-only tools + guarded scale + audit (this repo)
- v0.2 — Terraform `plan` (read-only) tool, deploy "what-changed" diff
- v0.3 — eval suite proving destructive-op refusal; OPA policy layer over writes
- v0.4 — package as `docker run`, publish to an MCP registry
