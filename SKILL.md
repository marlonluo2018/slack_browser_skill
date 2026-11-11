---
name: slack_browser_skill
description: Slack through a real browser session — send DMs/messages, read a conversation, delete a message you just sent, from any agent harness (ZCode, Claude Code, DeepSeek, plain shell) via the bundled Playwright driver. Use when the user asks to send or check something on IBM Slack, "Slack 提醒一下", "发个 Slack", "在 Slack 上说". Not the official Slack API/app skill — this one drives ibm.enterprise.slack.com with the user's own logged-in session.
triggers: [
  "send a slack", "slack message", "slack dm", "slack 一下", "slack 提醒", "发 slack",
  "在 slack 上说", "slack nudge", "slack follow up", "slack browser"
]
operations: ["send-dm", "send-channel", "find-person", "read-thread", "delete-message"]
---

# Slack (browser)

Drives Slack web (`ibm.enterprise.slack.com`, which redirects into `app.slack.com`) in a real browser using the user's own SSO session. BrainClaw has no Slack API integration and no app token — a logged-in browser is the only send channel, which is why this skill is named **browser** and lives apart from anything that talks to the Slack API.

One implementation does the work: `scripts/slack_driver.py`. An in-app-browser fallback is documented at the end, for when the driver cannot run.

## Approval

An outgoing Slack message is an outgoing message: same rule as email — **never send without the user approving the exact message text first**. Present the message, get approval, then type and send.

## One path: `slack_driver.py`

**Use `assistant_brain/skills/slack_browser_skill/scripts/slack_driver.py` for every Slack send, read and delete — in every harness.** One implementation, one set of behaviours, one place to fix. It needs only `py -3` + Playwright and its own signed-in profile; it makes no assumption about the host's browser tooling, so Claude Code, DeepSeek and ZCode run the identical code.

The in-app browser (section "Fallback: ZCode in-app browser" at the end) exists only for the case where the driver cannot run — Python/Playwright missing, profile locked by someone else, or Slack refusing the driver's Chromium. Do not reach for it first, and do not keep two live paths "just in case": two paths is how the same message gets sent twice or typed into a public channel.

## Driver usage

Requires `playwright` (`py -3 -m pip install playwright`) and a Chromium build (`py -3 -m playwright install chromium`).

```bash
D=assistant_brain/skills/slack_browser_skill/scripts/slack_driver.py

# one-time: opens a visible window, sign in with IBM SSO, session is kept in the profile
py -3 $D login

# is the stored session alive? (exit 0 = yes)
py -3 $D whoami

# send an approved DM (body from a file — never inline a long body in the shell)
py -3 $D send --email someone@cn.ibm.com --text-file outbox/slack-body.txt

# send several approved DMs in ONE browser session (one Slack boot for the whole batch)
py -3 $D send-batch --file outbox/slack-batch.json

# delete messages you just sent (verification / cleanup)
py -3 assistant_brain/skills/slack_browser_skill/scripts/slack_driver.py delete \
  --email someone@cn.ibm.com --match "zcode test"

# resident mode: one browser, many commands (see below)
py -3 $D serve

# read the tail of a conversation for context
py -3 $D read --email someone@cn.ibm.com --limit 10
```

### Resident mode (`serve`)

Cold starts are the driver's weak point: each one pays Slack's ~15 s boot. For any session that will send/read more than once, start the resident mode instead:

```bash
py -3 $D serve        # prints {"action":"serve","ready":true,...} then reads JSON lines
```

One JSON object per line in, one JSON object per line out, in order:

```text
{"cmd":"whoami"}
{"cmd":"send","email":"someone@cn.ibm.com","text":"..."}
{"cmd":"read","email":"someone@cn.ibm.com","limit":5}
{"cmd":"delete","email":"someone@cn.ibm.com","match":"keyword"}
{"cmd":"reload"}
{"cmd":"quit"}
```

A failing command answers `{"action":"<cmd>","error":"..."}` and the session stays alive, so a long batch survives one bad recipient. Approval still happens in the conversation before the JSON line is written — the same rule as every other send path.

Behaviour that matters:

- **No window needs to stay open.** The SSO session lives in the on-disk profile (`%LOCALAPPDATA%\BrainClaw\slack-profile`), not in a live browser process. Every `send` / `delete` / `read` launches its own Chromium from that profile, reuses the cookies, does the work, then closes it. Closing the browser after `login` does **not** lose the session; the next command works as long as the cookies are not expired (IBM SSO silent re-auth usually refreshes them when `whoami` shows signed in).
- **One browser per profile, ever.** A persistent Chromium profile can only be opened by one instance at a time — this is also why `serve` must be the only Slack browser running while it lives. A browser left running by an earlier command (or a crashed run) makes every later command fail with `Opening in existing browser session… profile is already in use`. Before blaming the network, list and clear the strays — they are Playwright's own Chromium, not the user's Chrome:
  ```powershell
  Get-CimInstance Win32_Process -Filter "name='chrome.exe'" |
    Where-Object { $_.ExecutablePath -like '*ms-playwright*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  ```
  The `--hide-crash-restore-bubble` launch arg prevents the crash-restore bubble from keeping Chromium alive after a force-kill.
- **Cost:** a one-shot command pays Slack's boot (~15 s); `serve` pays it once, and `send-batch` pays it once for a whole batch. Prefer the latter two for anything repeated.
- The `login` command opens `https://ibm.enterprise.slack.com/` (the enterprise entry redirects into `app.slack.com` through IBM SSO in one hop). Opening `app.slack.com` directly shows the generic sign-in form instead.
- `--headless` works only after a visible `login`; the first run must be headed. Once the profile holds the SSO session, headless is verified working (2026-11-11 `whoami`, `send`): the earlier "headless is unreliable" impression was the overlay bug below, not a headless limitation.
- Every command prints one JSON object on stdout and exits non-zero on failure, so a harness can parse the result instead of scraping text. `emit()` forces UTF-8 on stdout — Windows consoles default to cp1252 and any CJK body would otherwise crash the JSON print *after* the send already succeeded (the message is sent; only the report dies).
- `send` waits for `Message to …` (recipient resolved), fills the body once, refuses to click a disabled **Send now**, then verifies the text appears in the conversation.
- `send-batch` sends a JSON list `[{"email": "...", "text": "..."}, ...]` (path via `--file`) in one browser session, so the whole batch costs one Slack boot instead of one per message. The batch file itself is the approval record — every body must have been shown to the user before it is written. A failed recipient does not abort the remaining sends; the JSON report lists each item (`sent` / `total`), and the command exits non-zero if any item failed. Optional `--delay` (ms, default 1000) paces the sends.
- `delete` hovers the matching row, opens **More actions → Delete message…**, confirms the dialog and reports `{matched, deleted, remaining, reason}`. **The hover toolbar is flaky**: a single `hover()` + 600 ms wait regularly produced `More actions` count 0 and a silent `deleted: 0` (2026-11-10), while an identical manual hover worked. The driver therefore retries the hover up to 4 times (~1.2 s each, offset `position={x:40,y:10}` inside the row) before giving up. `reason` says where it stopped — `deleted-all`, `hover-toolbar-never-appeared`, `delete-menuitem-missing`, `confirm-dialog-missing`, `message-still-present-after-confirm` — so a failure is diagnosable from the JSON alone.
- **"Signed out" almost always means a stray overlay, not a dead session.** Symptom: every command exits with `not signed in`, yet the profile worked minutes earlier; a manual look shows the app on a DM with a full sidebar. Cause (verified 2026-11-11, screenshot + HTTP log): an open **Ctrl+K quick switcher** or **New message pane** marks the rest of the app `aria-hidden`, so `get_by_role("treeitem")` / `searchbox` / `New message` all return **0** while the UI is visibly fine. `dismiss_overlays()` presses `Escape` (up to twice) before every sign-in check and at the start of `open_dm`, and that turned the false negative into `signed_in: true`.
- **Do not call it throttling without evidence.** The same symptom was first blamed on Slack rate-limiting. `diagnose` disproved it in 45 s: `http_errors: []`, 98 API calls at normal status, `bodyLen: 3751`, a correct page title — a perfectly healthy page with a modal over it. Run `diagnose` before inventing a server-side explanation.
- **`diagnose` is the tool for "Slack looks broken".** `py -3 $D diagnose --seconds 45` prints HTTP statuses ≥400, the count of API calls, console errors, and a render timeline (title / body length / sidebar counts every 5 s), and writes a screenshot to `%LOCALAPPDATA%\BrainClaw\slack-diagnose.png`. Read the screenshot before touching the code.
- **Do not launch the visible browser from a sandboxed/background shell.** A headed Chromium started that way dies immediately and every Playwright call raises `TargetClosedError: Target has been closed`; identical code run with desktop/foreground execution opens the window and works (both reproductions on 2026-11-10). Same rule as Outlook COM in `ENVIRONMENT.md`: run `login` in the interactive desktop session.
- The driver resolves people **by email address**. Display names collide in this workspace (two "Wei Wang"), which leaves **Send now** permanently disabled.

## When the session expires

Measured from the stored profile on 2026-11-11 (cookie names and expiries only, no values):

| Cookie | Role | Expires |
|---|---|---|
| `d` | Slack auth credential | **2027-12-16** (~399 days) |
| `b` | secondary identity | 2027-12-15 |
| `lc` | last-login info | 2026-12-10 |
| `d-s` | per-browser session marker | session (browser close) |

So the stored profile is good for roughly **13 months** unattended, and closing the browser does not shorten it. Expect earlier death anyway — a password change, MFA re-enrolment, an admin revoking sessions, or IBM re-evaluating device trust all invalidate it immediately.

**Detection.** Every command already gates on the session. A real expiry is reported as exit code **3** with:

```json
{"action":"auth","signed_in":false,"hint":"...","hint_zh":"...","command":"py -3 .../slack_driver.py login"}
```

Exit code 3 means **"a human must sign in"** — distinct from 1 (usage/data problem) and 2 (timeout/selector), so a harness can branch on it without reading text.

**What the agent does on exit 3:** nothing automatic. Tell the user, in their language, that the Slack login has expired and ask them to run the login command; the browser window that opens is where they type the credentials. Never ask for, type, or store the password — the same rule as every other credential in this repo.

Suggested wording: “Slack 登录已失效，需要你重新登录一次：运行 `py -3 assistant_brain/skills/slack_browser_skill/scripts/slack_driver.py login`，会弹出浏览器窗口，你在窗口里用 IBM SSO 登录即可（登完窗口可以直接关，会话会保存）。”

**Before relaying an expiry, rule out the false alarm.** A single failed probe is not evidence: `signed_in()` already retries once, and a leftover overlay produces the identical symptom. If the user insists the session should be valid, run `diagnose --seconds 45` and look at the screenshot — an open quick-switcher means the session is fine.

## Fallback: ZCode in-app browser (only if the driver cannot run)

Use this when `slack_driver.py` is unavailable — no Python/Playwright, profile locked, or Slack refusing the driver's Chromium. Same rules apply (email lookup, `fill` not `type`, click "Send now", verify); only the mechanics differ, and the ZCode-specific pitfalls below still hold.

1. Bootstrap the browser runtime per the `control-browser` skill (ZCODE_PLUGIN_ROOT → `browser-client.mjs`), select the backend with `agent.browsers.getForUrl("https://ibm.enterprise.slack.com/")`.
2. Open `https://ibm.enterprise.slack.com/` in a tab. If the session is alive it lands directly on the workspace; if it redirects to a login page, **stop and ask the user to log in in the visible browser pane** — never handle credentials.
3. The workspace may bounce to `app.slack.com/client/<workspace>/<channel>` — that is the same app; keep using the tab.

## Sending a DM (backend-independent rules)

1. **Resolve the person by email.** Look the address up in contacts.md (falling back to Bluepage). Then, in order of preference:
   - **Ctrl+K quick switcher (preferred, verified 2026-11-10).** Press `Ctrl+K`; a dialog opens with a `combobox` whose accessible name is **`Query`** (it is *not* the sidebar's "Find a conversation…" box, and it is not a `searchbox` role). `fill(<address>)` into it and wait ~2 s: the first option is the person (`Wei Wang · @王玮`, with an `Enter` hint). **Click that option** — it opens the existing DM directly. This is safe and exact and is what the driver uses first.
   - **New message pane (fallback — only when there is no DM yet).** Click **New message**, `fill(<address>)` into the `To:` combobox, **wait for the option list to filter down**, then **click the person row**. Never press Enter here (see the trap below).
2. **Open the DM**: click the option/treeitem; the URL changes to `/client/<workspace>/D0XXXXXXXX` and the composer reads `Message to <Name>`. If the quick switcher found no person, the New message pane lands on the person instead.
3. **Write the body with `fill`, not `type`**: `composer.fill(text)` replaces whatever was in the composer; `type()` inserts at the cursor and will concatenate onto a leftover draft. Then confirm `await composer.evaluate(el => el.innerText)` equals the intended text before sending.
4. **Click "Send now"** — never Enter. In this workspace Enter is bound to "add a new line" (a hint next to the composer says so): a test on 2026-11-10 left `"enter-probe-1110\n\n\n"` in the composer with nothing sent. The button is disabled until text registers, so `isEnabled()` is the "text landed" signal.
5. **Verify** the message in a fresh snapshot before reporting success.

### The New-message `To:` trap (verified 2026-11-10)

Two behaviours make the New-message pane unreliable, and both were hit in one session:

1. **The pane restores half-finished draft state from the profile.** After a crashed or interrupted run the "New message" pane is re-opened with the `To:` box (or even a whole saved draft) already on screen. In that state clicking the toolbar's **New message** button times out (the open pane intercepts the click), even though the button reports `visible=True, enabled=True`. Before clicking, check `get_by_role("combobox", name="To:").count()`: if it is already 1, use it directly.
2. **Enter in the `To:` box selects the pre-highlighted option — which can be a channel.** Right after `fill(<email>)` the dropdown still holds the unfiltered list (channels first, e.g. `#help-ica20` highlighted). Pressing Enter before the list finishes filtering took the highlighted *channel*: the composer became `Message #help-ica20` and the typed body was one click away from being posted to a public channel. Wait until the option list has filtered (few options, first one a person, not `#`-prefixed), then **click the person row**.

A sent-message check that only looks for `Message to` will not catch the channel mix-up; the composer name (`Message #…`) and the URL are the signals that the wrong target opened.

## What Slack's editors do NOT honour (verified 2026-11-10, self-DM test)

These four behaviours explain nearly every "it typed but didn't send / sent the wrong text" failure. They were verified by direct experiment, not inferred:

1. **Enter does not send.** It inserts a newline; the message stays a draft. Always click "Send now".
2. **Synthetic `Control+a` is ignored.** `Control+a` then `Backspace` on the search box took `probeXYZ` → `probeXY` — one character deleted, no selection. Never assume a select-all cleared anything.
3. **`fill("")` does not clear.** On the sidebar search `<input>`, `fill("")` left `value` at `probeXYZ`, while `fill("YYY")` correctly replaced `XXX`. Empty-string fill is a no-op everywhere; to clear, use the search box's **"Clear text" button**, or press `Backspace` repeatedly (n ≥ current length).
4. **`innerText` cannot read a search `<input>`.** Its `innerText` is always `""` even when `value` is populated — an "is it empty?" check on innerText always answers "empty". Read it with `searchbox.evaluate(el => el.value)`. The composer genuinely is a contenteditable, so `innerText` there is valid (`<p><br></p>` / `"\n"` means empty).

The consequence of 2+3 in combination is a **garbled send**: leftover draft text is silently prepended to the new message (observed: `"enter-probe-1110name probe: Hi Sarah, ..."`). Always verify the composer content before clicking "Send now".

## Intermittent failures (Backend A / `node_repl` only)

- **Sidebar search click can time out** even when `count()/isVisible()/isEnabled()` all report fine (2 of 3 attempts on 2026-11-10). Prefer `Ctrl+K` + `fill`; if a click times out, switch to the keyboard path rather than retrying the same locator.
- **Transient transport errors** (`Unexpected token '}'`) still occur; on 2026-11-10 a call containing a literal person name executed fine, so do **not** treat names as the cause nor obfuscate them — treat the error as transient: pause, re-list tabs, retry once with the same plain code.
- Never machine-gun retries; do the most work per successful call (open DM + fill + send within one verified batch).

## Finding someone you have never messaged (verified 2026-11-10)

The sidebar box named "Channel or user name" (and the visible "Find a conversation…" input) **filters existing conversations only** — it is not a directory search. Typing a colleague you have no DM with returns `No conversations to show…`, which reads like "person not found" but is not.

Do not confuse it with the **Ctrl+K quick switcher**. That dialog ("Search across people, channels, files, workflows, and more", combobox name `Query`) searches much more broadly: typing an address there resolved the existing DM to `Wei Wang · @王玮` as the first option (2026-11-10). Whether it also resolves a person with *no* existing DM was not verified; `slack_driver.py` still falls back to the New message pane when the switcher yields no person row.

Use the **New message** toolbar button instead (the button with accessible name `New message` in the Actions toolbar): it opens a pane with a `combobox` named `To:`, a live option list, and a composer.

**Search by email address, not by display name.** The `To:` field accepts an address (`wangwei@cn.ibm.com`) and resolves it to the exact person; typing a display name is ambiguous in a shared workspace — two different "Wei Wang" exist — and Slack then shows `1 entry has multiple matches. Select entry to resolve.` with **"Send now" disabled**. Take the address from contacts.md or Bluepage and paste it into `To:`.

Recovery when the field is already ambiguous: press `Backspace` repeatedly in the `To:` combobox until it is empty — Slack then opens that person's existing DM pane directly with a usable `Message to <Name>` composer.

Therefore: **check `Send now`.isEnabled() after the body is filled, before clicking it.** An empty composer also leaves the button disabled, so only read it once `fill()` has put text in. Clicking a disabled button blocks for the full timeout with `waiting for click actionability (disabled)` and sends nothing; with text present, `isEnabled() === false` means the recipient is unresolved.

## Deleting a message you just sent — Backend A mechanics (verified 2026-11-10)

Backend B does this with `slack_driver.py delete`; the notes below are for the in-app browser, where `PlaywrightLocator` has no `hover()`.

Deleting is how the send path gets tested without leaving noise behind. Slack renders message actions only on hover, so:

1. Get the row's centre with `tab.playwright.evaluate` walking `[role="listitem"]` for the message text, calling `scrollIntoView({ block: "center" })`, then returning `getBoundingClientRect()` — **measure in the same call as the move**: sending or scrolling shifts the list, and hovering stale coordinates silently shows nothing (`More actions` count 0, no error);
2. `await tab.cua.move({ x, y })` — this triggers the hover toolbar;
3. `getByRole("button", { name: "More actions" })` (one per hovered row) → menu → `menuitem` named `Delete message…`;
4. the app shows a `dialog "Delete message"` with `Cancel` / `Delete` — click the dialog-scoped `Delete`;
5. verify: the text is gone from a fresh snapshot.

Do **not** click the unnamed first button inside the message row — that is the avatar/name and opens the sender's profile dialog. CUA right-click is unsupported (`Unsupported CUA mouse button: right`), so the context menu is not an alternative. Check the deletion worked before reporting it; a message deleted in Slack still fired its push notification.

## Reading a thread (for context before replying)

Backend B: `slack_driver.py read --email <addr> --limit N`. Backend A: open the DM/channel as above; `domSnapshot()` renders the message list (sender buttons, timestamps, reaction groups). Messages load as you scroll — the snapshot shows what is currently rendered.

## After sending

1. Verify: the sent message appears in the DM's message list (fresh snapshot), and the composer is empty.
2. Record in the task file timeline as `[slack-out]` (same convention as `[email-out]`): who, what, and any decision the message carried.

## Field notes

- **Find people by email, not by name.** `Ctrl+K` + `fill(<address>)` into the `Query` combobox is the preferred path for anyone you already have a DM with; the New message `To:` field is the fallback and also takes an address. Display names collide ("Wei Wang" ×2) and disable Send. Get the address from contacts.md / Bluepage first.
- **The session has no visible half-life.** After `login`, closing the browser is fine; `whoami` only says `signed_in: false` when the cookies are actually gone. Give the SPA time before concluding that: the `/client` → `/client/<workspace>` redirect measured ~6 s on 2026-11-10, so check for the `New message` button after a full boot (`BOOT_MS`, default 15 s), not just the URL at 3 s.
- **Poll for the sidebar, never trust one fixed wait.** A single `wait_for_timeout(BOOT_MS)` produced a false `not signed in` on a healthy session (2026-11-10, right after `whoami` had returned `true`). `signed_in()` now polls for the sidebar for up to `BOOT_MS + 20 s` **and clears overlays on every pass**. If a command reports "not signed in" right after a successful `whoami`, check for a leftover overlay (or run `diagnose`) before asking the user to log in again.
- **`whoami` is the cheap pre-flight** (boot + render, no interaction). Run it before a batch of sends rather than debugging a failed send.
- Channels open the same way: search the channel name, click the treeitem.
- The composer is a `textbox` role (`Message to <Name>`); the sidebar search box is a `searchbox` role — don't confuse them.
- Message text containing person names, CJK or other text types sends fine (`中文发送测试` verified 2026-11-10); the transport is not the obstacle.
- Slack sessions persist like the EPD session; reuse an existing Slack tab (`tabs.list()` → match URL) before creating a new one.
- Common names collide: two "Wei Wang" exist (`wangwei@cn.ibm.com` — FNC Hybrid Cloud & Data; `Wei.Wh.Wang@ibm.com` — Application Developer). Always confirm the email before DMing a common name.
- Emoji survive `fill()` and render as `img "<name> emoji"` in the snapshot; `innerText` drops them, so do not use innerText equality as the send check when the message has emoji.
- Testing tip: the self-DM (`/client/<workspace>/D0…`, sidebar entry `Active <Your Name>you`) is the safe target for a dry run — nothing reaches a colleague.
