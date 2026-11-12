# slack_web_skill

Slack through a real browser session — for agents that have no Slack app token.

It talks to Slack only as a human would: a Chromium window signed in with the user's SSO session,
driving the workspace entry (IBM's is `ibm.enterprise.slack.com`, which redirects into
`app.slack.com`). No API token, no bot user, no webhook — and no host project around it:
config, profile and diagnostics live in a directory of the skill's own (see Configuration).

## Why this exists

The normal way for software to talk to Slack is a Slack app: register it in the workspace, get a
bot token, call `chat.postMessage`. That route needs two things this environment does not have —
an app approved by the workspace administrator, and a token that then has to be stored somewhere.

So this skill takes the only channel that is left: **the same one a human uses.** A Chromium window
signed in with the user's own SSO session, driving the web UI. No app, no bot user, no webhook, no
token to leak — and nothing to ask an administrator for.

What that buys:

- it works with the permissions the user already has, on the account they already use;
- messages carry the user's own identity, exactly as if they had typed them;
- there is no credential anywhere in this repo: the login lives in a browser profile, encrypted by
  the OS, and the agent never sees or types it.

What it costs, stated plainly:

- **it is slower** — a browser boot is ~15 s where an API call is milliseconds (`serve` amortises
  it: one boot, many commands);
- **it needs a human at sign-in** — the SSO prompt, and any MFA or passkey, is typed by the person,
  so first use per session is attended, never a background job;
- **it is a UI scraper, so Slack can break it** — the driver locates controls by role and
  `aria-label` rather than class names for this reason, but a redesign can still break a command.

Those costs are why this is a **fallback, not a replacement**. If a Slack app token ever becomes
available, use it and retire this skill. Until then, this is the one that works.

This is **not** the official Slack skill. Anything named "slack" that offers `chat.postMessage`
and friends is a different thing, and it is the better thing when it can be installed.

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

## Quick start

```bash
D=scripts/slack_driver.py

py -3 $D login                       # visible window, workspace SSO, ~30 s
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

## Configuration

**The workspace id is never something you look up.** Sign in once and the driver reads it from the URL it lands on (`app.slack.com/client/<TEAM>/…`), then writes it into `config.json`:

```bash
py -3 scripts/slack_driver.py login --url https://your-workspace.slack.com/
# {"ok": true, "team": "T0123456", "saved": true, "config": ".../config.json"}
```

`login` needs a visible window — the credentials and any passkey are typed by a human, so `--headless` cannot sign in (it refuses immediately rather than waiting). At a terminal `login --ask` prompts for the URL; an agent passes `--url`, so nothing is ever typed into a prompt nobody can answer.

A passkey is entered by the user in that window like any other credential; if the prompt does not appear at all, set `"browser_channel": "msedge"` (or `chrome`) to sign in through an installed browser, which has the platform authenticator. Playwright's bundled Chromium has not been verified either way. When `login` runs as a background job, verify it with `whoami` after it exits — one browser per profile.

The only thing you supply is *where to sign in*. On the IBM workspace there is nothing to supply at all: `login` uses the built-in default and saves what it finds.

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

`SKILL.md` has the full table, the failure modes, and the first-run checklist.
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
