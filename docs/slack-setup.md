# Slack + Claude setup

## How it calls Claude

`slack_bridge.py` uses the **Anthropic SDK** (`anthropic.Anthropic().messages.create`)
in a manual **tool-use loop**:

1. The Slack message text becomes the `user` message.
2. Claude (`claude-opus-4-8`, adaptive thinking) is given the KubeWhisper tool
   schemas and decides which to call.
3. **Read** tools run immediately and their output is fed back to Claude.
4. **Write** tools (`scale_deployment`) are *not* executed — they post a Slack
   **Approve / Deny** message and return "pending approval" to Claude.
5. The loop ends when Claude returns a normal answer (`stop_reason != "tool_use"`).

```
Slack mention ──▶ ask_claude() ──▶ messages.create(tools=…) ──┐
       ▲                                                       │ stop_reason
       │   read tool result  ◀── _run(tool)  ◀── read tool ◀──┤ == tool_use
       │                                                       │
       └── final answer ◀── (stop_reason == end_turn)          │
                                                               │
   write tool ──▶ post Approve/Deny buttons ──▶ (human clicks) ─┘ executes out-of-band
```

## Permission layers

| Layer | Controls |
|---|---|
| Read tools | Always allowed — investigate & answer |
| `KUBEWHISPER_ALLOW_WRITES` | Master switch. `false` (default) → writes are proposal-only, buttons aren't even offered |
| Slack Approve button | Per-action human gate — nothing runs until clicked |
| `KUBEWHISPER_APPROVERS` | Optional allowlist of Slack user IDs who may approve |
| Server `scale_deployment` | Still re-checks `ALLOW_WRITES` + audits every execution |

## One-time Slack app setup (~5 min)

1. **Create the app** → https://api.slack.com/apps → *Create New App* → *From scratch*.
2. **Enable Socket Mode** (Settings → Socket Mode → on). This generates an
   **App-Level Token** (`xapp-…`) with `connections:write` → that's `SLACK_APP_TOKEN`.
   (Socket Mode means no public URL is needed — events *and* button clicks come over
   the socket.)
3. **Bot scopes** (OAuth & Permissions → Bot Token Scopes): add `app_mentions:read`
   and `chat:write`.
4. **Event subscriptions** (Event Subscriptions → on → Subscribe to bot events): add
   `app_mention`.
5. **Interactivity** (Interactivity & Shortcuts → on) — required for the Approve/Deny
   buttons. With Socket Mode you do *not* need a Request URL.
6. **Install to workspace** → copy the **Bot User OAuth Token** (`xoxb-…`) → `SLACK_BOT_TOKEN`.
7. Invite the bot to a channel: `/invite @KubeWhisper`.

## Run

```bash
pip install -r requirements-slack.txt
export ANTHROPIC_API_KEY=...
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APP_TOKEN=xapp-...
export KUBEWHISPER_ALLOW_WRITES=true        # omit for propose-only
export KUBEWHISPER_APPROVERS=U012ABC,U345DEF # optional approver allowlist
export KUBEWHISPER_CLUSTERS=$PWD/clusters.json
python slack_bridge.py
```

## Try it

- *"@KubeWhisper which pods are crash-looping in prod?"* → read-only, answers.
- *"@KubeWhisper the mumbai live stream is dropping — scale transcode to 6"* →
  Claude investigates, then posts an **Approve & run / Deny** card. Click Approve
  (as an allowlisted user) → it executes `scale_deployment(confirm=True)` and posts
  the audited result in-thread.
