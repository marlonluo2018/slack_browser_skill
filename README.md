# slack_browser_skill

Slack through a real browser session — for agents that have no Slack app token.

It talks to Slack only as a human would: a Chromium window signed in with the user's SSO
session, driving the workspace entry (IBM's is `ibm.enterprise.slack.com`, which redirects into
`app.slack.com`). There is no API token, no bot user, no webhook. The skill keeps its own state
— config, profile — under a per-user directory of its own (`%LOCALAPPDATA%\slack-browser-skill\`
on Windows, `~/Library/Application Support/slack-browser-skill/` on macOS,
`$XDG_DATA_HOME/slack-browser-skill/` on Linux), so it runs standalone and needs no host project
around it.

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

py -3 $D login                       # visible window, workspace SSO, ~30 s
py -3 $D whoami                      # {"action":"whoami","signed_in":true,...}
py -3 $D send --email someone@cn.ibm.com --text-file body.txt
py -3 $D serve                       # one boot, then JSON commands on stdin
```

## Configuration

The defaults are the IBM workspace. On another workspace, copy the template to the state directory and edit it:

```bash
mkdir -p ~/.local/share/slack-browser-skill          # or the equivalent on your OS
cp config.example.json ~/.local/share/slack-browser-skill/config.json
```

```json
{
  "team": "your-workspace",
  "login_url": "https://your-workspace.slack.com/"
}
```

`team` is the id in `app.slack.com/client/<TEAM>`; `login_url` is the page the sign-in starts on.

Prefer the file to live **in the checkout**, visible next to the code? Create a `.state/` directory next to `scripts/` and put `config.json` in it — the driver detects that directory and uses it, no environment variable needed:

```bash
mkdir .state && cp config.example.json .state/config.json
py -3 scripts/slack_driver.py whoami    # prints home/config/portable/found
```

`whoami` always reports which home won and whether a config file was actually found, so "why is my setting ignored" is one command away. `.state/`, `config.json` and `profile/` are gitignored in both modes.

For a standalone workspace both values are the subdomain; on an Enterprise Grid org the login host is `<org>.enterprise.slack.com` while the team id stays the client id. Every key is optional and every key has an env-var twin (`SLACK_SKILL_TEAM`, `SLACK_SKILL_LOGIN_URL`, …), with **env var > config file > default**; `SLACK_SKILL_HOME` moves the whole state directory, and a *relative* value keeps it inside the checkout (`SLACK_SKILL_HOME=.state`) — `config.json`, `.state/` and `profile/` are gitignored, so that never publishes a machine's settings or its cookies.

`SKILL.md` has the full table, the failure modes, and the first-run checklist.
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
