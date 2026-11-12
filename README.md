# slack_web_skill

**Slack messages, where the official Slack API skill and MCP servers are not permitted.**

That path is closed: the official integration has to be authorised into the workspace and hold a
token, and where it is not permitted there is no token to obtain. Asking for one is not a step on the
way — it is the thing that is disallowed.

What is left is the web UI, which is what this drives: `app.slack.com` in Chromium, with the user's
own SSO session. It sends and reads as the user already does — no API token, no bot user, no webhook,
no app registration, nothing new to authorise.

Config, browser profile and diagnostics live in a directory of the skill's own, so it runs standalone
and needs no host project around it.

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

## Install

Python 3.10+ is the only prerequisite. Everything else is one command, and it is safe to re-run:

```bash
py -3 scripts/install.py        # Windows    (python3 scripts/install.py on macOS/Linux)
```

It installs Playwright, downloads its Chromium build (~130 MB, once), creates `config.json` from the template if there is none, and prints the two commands for the first sign-in. It never asks for credentials — signing in stays a human step in a visible window.

By hand, the same steps are `py -3 -m pip install playwright` and `py -3 -m playwright install chromium`.

## Who does what

**The agent runs everything except the sign-in. Your only job is to sign in once, in the window it
opens.**

1. The agent runs `login`. A Chromium window opens and waits while **you** enter the SSO details it
   asks for — password, MFA, passkey, whatever the workspace requires. The agent never sees or types
   those, and the window closes by itself once the app loads.
2. The session is now in the profile, so the agent runs `send`, `read`, `serve` and the rest
   **headless** — nothing appears on your screen.
3. When a run reports exit code 3 (`signed_in: false`), the session has expired and the agent asks
   you to sign in again. That is the only time it needs you.

The commands below are what the **agent** runs, shown so you can see what it is doing:

```bash
D=scripts/slack_driver.py

py -3 $D login                       # once, in a window: you sign in, the agent waits
py -3 $D send --email someone@cn.ibm.com --text-file body.txt
py -3 $D --headless whoami           # headless from here on, no window
py -3 $D --headless serve            # one boot, then JSON commands on stdin
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

## Configuration

`login` reads the workspace id from the URL it lands on (`app.slack.com/client/<TEAM>/…`) and writes it into `config.json`:

```bash
py -3 scripts/slack_driver.py login --url https://your-workspace.slack.com/
# {"ok": true, "team": "T0123456", "saved": true, "config": ".../config.json"}
```

`login` needs a visible window: the credentials and any passkey are typed by a human, so `--headless` cannot complete a sign-in (it refuses immediately rather than waiting). Everything else runs headless once the session exists. At a terminal `login --ask` prompts for the URL; an agent passes `--url`, so nothing is ever typed into a prompt nobody can answer.

A passkey is entered by the user in that window like any other credential; if the prompt does not appear at all, set `"browser_channel": "msedge"` (or `chrome`) to sign in through an installed browser, which has the platform authenticator. Playwright's bundled Chromium has not been verified either way. When `login` runs as a background job, verify it with `whoami` after it exits — one browser per profile.

The only thing you supply is *where to sign in*. If an entry is already configured there is nothing to supply: `login` uses it and saves what it finds.

What it writes is a plain file you can still edit by hand:

```json
{
  "team": "T0123456",
  "login_url": "https://your-workspace.slack.com/"
}
```

`team` is the id in `app.slack.com/client/<TEAM>`; `login_url` is the page sign-in starts on. For a standalone workspace both values are the subdomain; on an Enterprise Grid org the login host is `<org>.enterprise.slack.com` while the team id stays the client id.

`config.json` is gitignored: it sits in the checkout where you can see and edit it, but a machine's workspace never travels back into the repo. The browser profile — the session itself — lives in the per-user data directory (`%LOCALAPPDATA%\slack-web-skill\` on Windows, `~/Library/Application Support/slack-web-skill/` on macOS, `$XDG_DATA_HOME/slack-web-skill/` on Linux). For a self-contained copy (USB stick, throwaway VM) create `.state/` next to `scripts/` and put the config in there instead — portable mode needs no environment variable.

One command answers "which file am I actually using?":

```bash
py -3 scripts/slack_driver.py whoami    # home, config, config_found, portable
```

Every key is optional and every key has an env-var twin (`SLACK_SKILL_TEAM`, `SLACK_SKILL_LOGIN_URL`, …), with **env var > config file > default**.

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
