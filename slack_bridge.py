"""KubeWhisper Slack bot — ask infra questions, and approve fixes from Slack.

Permission model (three layers):
  1. Read tools (list/resolve/get/query/runs) run automatically — investigate &
     answer, no approval needed.
  2. Write tools (scale_deployment) are NEVER executed by the model. The bot
     instead posts an Approve / Deny message to Slack. Nothing changes until a
     human clicks Approve.
  3. A write only actually runs if BOTH KUBEWHISPER_ALLOW_WRITES=true on this
     process AND an authorized operator approves it in Slack (optional allowlist
     via KUBEWHISPER_APPROVERS=U123,U456 of Slack user IDs).

How it calls Claude: the Anthropic SDK `messages.create()` in a manual tool-use
loop (model: claude-opus-4-8, adaptive thinking). Read tools feed results back to
the model; write tools short-circuit into the Slack approval flow.

Run it:
    pip install -r requirements-slack.txt
    export ANTHROPIC_API_KEY=...        # console.anthropic.com
    export SLACK_BOT_TOKEN=xoxb-...      # scopes: app_mentions:read, chat:write
    export SLACK_APP_TOKEN=xapp-...      # Socket Mode (connections:write)
    export KUBEWHISPER_ALLOW_WRITES=true # omit/false = propose-only, never executes
    export KUBEWHISPER_APPROVERS=U0123   # optional: only these users may approve
    python slack_bridge.py
"""
from __future__ import annotations

import os

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import kubewhisper.server as kw
from kubewhisper.approvals import ApprovalStore, decide

MODEL = "claude-opus-4-8"
MAX_TOOL_ITERATIONS = 8

WRITE_TOOLS = {"scale_deployment"}          # require Slack approval, never auto-run
WRITES_ENABLED = os.environ.get("KUBEWHISPER_ALLOW_WRITES", "false").lower() == "true"
APPROVERS = {u.strip() for u in os.environ.get("KUBEWHISPER_APPROVERS", "").split(",") if u.strip()}

APPROVALS = ApprovalStore()

SYSTEM = (
    "You are KubeWhisper, an SRE assistant operating over DevOps tools. "
    "When a message describes an alert/error, call resolve_cluster first to pick the "
    "right cluster, then use the read tools to investigate and explain what's wrong. "
    "If a remediation is warranted, you may PROPOSE it by calling scale_deployment — "
    "this does NOT execute; it posts an approval request to a human operator in Slack. "
    "Never claim you executed a change yourself. Lead with the answer, then the evidence."
)


def _fn(name: str):
    obj = getattr(kw, name)
    return getattr(obj, "fn", obj)


TOOLS = [
    {"name": "list_clusters", "description": "List configured clusters and routing hints.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "resolve_cluster", "description": "Pick the cluster an alert/error belongs to.",
     "input_schema": {"type": "object", "properties": {"alert_text": {"type": "string"}},
                      "required": ["alert_text"]}},
    {"name": "k8s_get_resources",
     "description": "List pods|deployments|services|nodes|events with status, for a cluster.",
     "input_schema": {"type": "object", "properties": {
         "resource_type": {"type": "string"}, "namespace": {"type": "string"},
         "cluster": {"type": "string"}}, "required": []}},
    {"name": "prometheus_query", "description": "Run an instant PromQL query against a cluster.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"}, "cluster": {"type": "string"}}, "required": ["query"]}},
    {"name": "github_actions_runs", "description": "Recent GitHub Actions runs for owner/name.",
     "input_schema": {"type": "object", "properties": {
         "repo": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["repo"]}},
    {"name": "scale_deployment",
     "description": "PROPOSE scaling a deployment. Posts a Slack approval request; a human "
                    "must approve before it runs. Does not execute on its own.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "replicas": {"type": "integer"},
         "namespace": {"type": "string"}, "cluster": {"type": "string"}}, "required": ["name", "replicas"]}},
]

app = App(token=os.environ["SLACK_BOT_TOKEN"])


# --------------------------------------------------------------------------- #
# Tool execution                                                              #
# --------------------------------------------------------------------------- #
def _run(name: str, args: dict) -> str:
    try:
        return str(_fn(name)(**args))
    except Exception as exc:
        return f"ERROR running {name}: {exc}"


def _post_approval(ctx: dict, pid: str, name: str, args: dict) -> None:
    summary = f"`{name}` → " + ", ".join(f"{k}={v}" for k, v in args.items())
    app.client.chat_postMessage(
        channel=ctx["channel"], thread_ts=ctx["thread_ts"],
        text=f"Approval needed: {summary}",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f":lock: *Approval required*\nProposed remediation:\n• {summary}"}},
            {"type": "actions", "elements": [
                {"type": "button", "style": "primary", "action_id": "kw_approve",
                 "text": {"type": "plain_text", "text": "Approve & run"}, "value": pid},
                {"type": "button", "style": "danger", "action_id": "kw_deny",
                 "text": {"type": "plain_text", "text": "Deny"}, "value": pid},
            ]},
        ],
    )


def _handle_tool(name: str, args: dict, ctx: dict) -> str:
    """Read tools run now; write tools become a Slack approval request."""
    if name in WRITE_TOOLS:
        if not WRITES_ENABLED:
            return ("Writes are disabled on this bot (KUBEWHISPER_ALLOW_WRITES=false). "
                    "I can only propose this change, not request execution.")
        pid = APPROVALS.propose(name, args, ctx)
        _post_approval(ctx, pid, name, args)
        return (f"Proposed {name}({args}). Posted an approval request to Slack — "
                "an authorized operator must click Approve before anything runs.")
    return _run(name, args)


def ask_claude(question: str, ctx: dict) -> str:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    messages = [{"role": "user", "content": question}]
    for _ in range(MAX_TOOL_ITERATIONS):
        resp = client.messages.create(
            model=MODEL, max_tokens=8192, system=SYSTEM,
            thinking={"type": "adaptive"}, tools=TOOLS, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text") or "(no answer)"
        messages.append({"role": "assistant", "content": resp.content})
        results = [{"type": "tool_result", "tool_use_id": b.id,
                    "content": _handle_tool(b.name, b.input, ctx)}
                   for b in resp.content if b.type == "tool_use"]
        messages.append({"role": "user", "content": results})
    return "Stopped after too many tool calls — please narrow the question."


# --------------------------------------------------------------------------- #
# Slack handlers                                                              #
# --------------------------------------------------------------------------- #
@app.event("app_mention")
def on_mention(event, say):
    text = event.get("text", "")
    question = text.split(">", 1)[1].strip() if ">" in text else text
    thread_ts = event.get("thread_ts") or event.get("ts")
    ctx = {"channel": event["channel"], "thread_ts": thread_ts}
    say(text=":hourglass_flowing_sand: checking…", thread_ts=thread_ts)
    try:
        answer = ask_claude(question, ctx)
    except Exception as exc:
        answer = f":warning: {exc}"
    say(text=answer, thread_ts=thread_ts)


def _resolve_approval(body, client, approve: bool):
    user = body["user"]["id"]
    pid = body["actions"][0]["value"]
    chan, ts = body["channel"]["id"], body["message"]["ts"]

    d = decide(APPROVALS, pid, user, approve, APPROVERS, _run)
    if d.outcome == "unauthorized":
        client.chat_postMessage(channel=chan, thread_ts=ts,
                                text=f":no_entry: <@{user}> is not authorized to approve.")
    elif d.outcome == "expired":
        client.chat_update(channel=chan, ts=ts, text="This approval has expired or was already handled.", blocks=[])
    elif d.outcome == "denied":
        client.chat_update(channel=chan, ts=ts, blocks=[],
                           text=f":x: Denied by <@{user}> — `{d.proposal['name']}` not run.")
    else:
        client.chat_update(channel=chan, ts=ts, blocks=[],
                           text=f":white_check_mark: Approved by <@{user}> — executed `{d.proposal['name']}`.")
        client.chat_postMessage(channel=chan, thread_ts=d.proposal["thread_ts"], text=f"```{d.result}```")


@app.action("kw_approve")
def on_approve(ack, body, client):
    ack()
    _resolve_approval(body, client, approve=True)


@app.action("kw_deny")
def on_deny(ack, body, client):
    ack()
    _resolve_approval(body, client, approve=False)


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
