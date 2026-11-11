# slack_browser_skill

Slack through a real browser session — for agents that have no Slack app token.

BrainClaw talks to IBM Slack only as a human would: a Chromium window signed in with the user's
IBM SSO session, driving `ibm.enterprise.slack.com` (which redirects into `app.slack.com`).
There is no API token, no bot user, no webhook.

This is **not** the official Slack skill. Anything named "slack" that offers ``chat.postMessage``
and friends is a different thing; this one exists because that path is not available here.

## What it does

| Command | Purpose |
|---|---|
| `login` | one-time SSO sign-in in a visible window; the session is stored in a profile |
| `whoami` | is the stored session still valid? (exit 3 = needs a human sign-in) |
| `serve` | resident mode: one browser, many commands, JSON lines over stdin/stdout |
| `send` | send one DM (`--email`, body from `--text-file` / `--text`) |
| `send-batch` | several approved DMs in one browser session |
| `read` | read the tail of a conversation |
| `delete` | delete messages you just sent (verification/cleanup) |
| `diagnose` | HTTP statuses, console errors, render timeline, screenshot |

## Requirements

* Python 3.10+
* `py -3 -m pip install playwright`
* `py -3 -m playwright install chromium`

## Quick start

```bash
D=scripts/slack_driver.py

py -3 $D login                       # visible window, IBM SSO, ~30 s
py -3 $D whoami                      # {"action":"whoami","signed_in":true,...}
py -3 $D send --email someone@cn.ibm.com --text-file body.txt
py -3 $D serve                       # one boot, then JSON commands on stdin
```

`serve` accepts one JSON object per line:

```text
{"cmd":"whoami"}
{"cmd":"send","email":"someone@cn.ibm.com","text":"..."}
{"cmd":"read","email":"someone@cn.ibm.com","limit":5}
{"cmd":"delete","email":"someone@cn.ibm.com","match":"keyword"}
{"cmd":"reload"}
{"cmd":"quit"}
```

## Design notes

* **Email, not display name.** Two people can share a display name in a big workspace; the
  address resolves to exactly one. A name collision leaves Slack's "Send now" disabled forever.
* **`fill()` the body, then click "Send now".** Enter inserts a newline in this workspace;
  `type()` appends to whatever draft was already there (that produced a garbled message once).
* **One browser per profile.** A persistent profile can only be opened by one process at a time;
  stray Playwright Chromium processes must be killed before retrying.
* **`serve` exists because boots are expensive** (~15 s each) and a session usually has more than
  one thing to do.
* **Never handle credentials.** `login` opens a window; the human types. Exit code 3 means "ask
  the user to sign in", nothing is retried automatically.

`SKILL.md` carries the full operating manual, including the pitfalls measured in practice
(aria-hidden overlays that fake a signed-out state, hover-only message menus, the New-message
pane restoring stale drafts, and so on).
