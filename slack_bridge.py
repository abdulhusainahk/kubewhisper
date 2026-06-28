"""KubeWhisper Slack bot — ask your infra questions in Slack.

@mention the bot in any channel ("@KubeWhisper which pods are failing in prod?")
and it runs a Claude tool-use loop over KubeWhisper's read-only tools, routes to
the right cluster, and replies in-thread.

This is a separate process from the MCP server (server.py). The MCP server is
driven by Claude Desktop; this bridge drives Claude itself (Anthropic SDK) so
Slack users who don't run Claude Desktop can still use the tools.

Run it:
    pip install -r requirements-slack.txt
    export ANTHROPIC_API_KEY=...        # https://console.anthropic.com
    export SLACK_BOT_TOKEN=xoxb-...      # chat:write, app_mentions:read
    export SLACK_APP_TOKEN=xapp-...      # Socket Mode token
    python slack_bridge.py
"""
from __future__ import annotations

import os

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import kubewhisper.server as kw

MODEL = "claude-opus-4-8"
MAX_TOOL_ITERATIONS = 8

SYSTEM = (
    "You are KubeWhisper, an SRE assistant operating over read-only DevOps tools. "
    "When a question mentions an environment, alert, or error, call resolve_cluster "
    "first to pick the right cluster, then pass that cluster name to the other tools. "
    "Be concise: lead with the answer, then the evidence. You cannot take destructive "
    "actions — scale_deployment is preview-only unless an operator has enabled writes."
)


# Resolve the underlying callables (the MCP decorator may wrap them).
def _fn(name: str):
    obj = getattr(kw, name)
    return getattr(obj, "fn", obj)


TOOLS = [
    {"name": "list_clusters", "description": "List configured clusters and routing hints.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "resolve_cluster", "description": "Pick the cluster an alert/error belongs to.",
     "input_schema": {"type": "object",
                      "properties": {"alert_text": {"type": "string"}},
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
     "description": "Scale a deployment (preview-only unless operator enabled writes).",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "replicas": {"type": "integer"},
         "namespace": {"type": "string"}, "cluster": {"type": "string"},
         "confirm": {"type": "boolean"}}, "required": ["name", "replicas"]}},
]


def _dispatch(name: str, args: dict) -> str:
    try:
        return str(_fn(name)(**args))
    except Exception as exc:  # surface tool errors back to the model
        return f"ERROR running {name}: {exc}"


def ask_claude(question: str) -> str:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    messages = [{"role": "user", "content": question}]
    for _ in range(MAX_TOOL_ITERATIONS):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text") or "(no answer)"
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": _dispatch(b.name, b.input)})
        messages.append({"role": "user", "content": results})
    return "Stopped after too many tool calls — please narrow the question."


app = App(token=os.environ["SLACK_BOT_TOKEN"])


@app.event("app_mention")
def on_mention(event, say):
    text = event.get("text", "")
    # strip the leading "<@BOTID>" mention
    question = text.split(">", 1)[1].strip() if ">" in text else text
    say(text=":hourglass_flowing_sand: checking…", thread_ts=event.get("ts"))
    try:
        answer = ask_claude(question)
    except Exception as exc:
        answer = f":warning: {exc}"
    say(text=answer, thread_ts=event.get("ts"))


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
