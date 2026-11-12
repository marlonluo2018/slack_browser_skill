#!/usr/bin/env python3
"""Slack web driver that does not depend on any agent harness.

Portable backend for the slack_browser_skill: works with Claude Code, DeepSeek or any
harness that can run `py -3` / `python3`. It drives app.slack.com with
Playwright using a persistent browser profile, so the IBM SSO session is
reused after a one-time `login`.

Commands
    login                     open a visible browser and wait for the Slack session
    whoami                    report whether the stored profile is signed in
    send --email A --text T   send a DM (or --text-file F / --text-stdin)
    send-batch --file J       send several approved DMs in one browser session
    delete --email A --match M  delete messages containing M in that DM
    read --email A [--limit N]  print the last N messages of that DM

The behaviours encoded here come from measured Slack-web quirks: the sidebar
box only filters existing conversations, display names collide (use the email),
Enter inserts a newline instead of sending, and Send now stays disabled while a
recipient is unresolved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:  # pragma: no cover - environment problem, reported to the user
    sys.exit("playwright is not installed: py -3 -m pip install playwright && py -3 -m playwright install chromium")

SLACK = "https://app.slack.com/client"
DEFAULT_TIMEOUT_MS = 20_000
# --- settings: environment variable > config file > built-in default -------
# This driver is its own repo and owns its own state: nothing here is named
# after the harness that happens to call it. One JSON file carries the
# per-machine settings across shells — an env var dies with the shell that set
# it, which is useless for a second terminal or a scheduled run — and an env
# var still wins, so a one-off override never needs a file edit.
#
#   %LOCALAPPDATA%\slack-browser-skill\config.json   (Windows)
#   ~/.local/share/slack-browser-skill/config.json   (elsewhere)
#
# SLACK_SKILL_* is the current env prefix; the first spelling, BRAINCLAW_SLACK_*,
# is still accepted as an alias so an existing setup keeps working.
HOME_NAME = "slack-browser-skill"
# The skill's own directory -- the root for a relative state override.
SKILL_ROOT = Path(__file__).resolve().parents[1]


def _data_base(platform: str, env) -> Path:
    """The per-user data directory this platform actually uses.

    Three conventions, not two. Windows keeps per-user app data in
    %LOCALAPPDATA%, macOS in ~/Library/Application Support, and Linux/BSD
    follow XDG. Treating "not Windows" as "Linux" runs on a Mac but scatters
    state into ~/.local/share, which is not where a Mac user, a backup tool or
    a cleaner looks.
    """
    if platform == "nt":
        return Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _resolve(value: str) -> Path:
    """An override path: absolute as given, relative to the skill directory.

    Relative is the portable form -- `SLACK_SKILL_HOME=.state` keeps every byte
    inside the checkout (self-contained copy, easy to move between machines),
    and it is the same string on Windows and macOS.
    """
    path = Path(value).expanduser()
    return path if path.is_absolute() else (SKILL_ROOT / path)


PORTABLE_DIR = ".state"


def skill_home() -> Path:
    """Where this skill keeps its own state: config, profile, diagnostics.

    Three ways to answer, in order: the SLACK_SKILL_HOME override; a `.state/`
    directory next to the script, which means portable mode; and otherwise the
    platform data directory. The middle one exists because the alternative —
    requiring an env var to keep state visible inside the checkout — reset the
    very problem the config file was added to solve, since an env var dies with
    the shell. A directory that is simply *there* needs no shell setup, and it
    is gitignored, so nothing hidden and nothing committed.
    """
    override = os.environ.get("SLACK_SKILL_HOME") or os.environ.get("BRAINCLAW_SLACK_HOME")
    if override:
        return _resolve(override)
    portable = SKILL_ROOT / PORTABLE_DIR
    if portable.is_dir():
        return portable
    return _data_base(os.name, os.environ) / HOME_NAME


def config_path() -> Path:
    override = os.environ.get("SLACK_SKILL_CONFIG") or os.environ.get("BRAINCLAW_SLACK_CONFIG")
    return _resolve(override) if override else skill_home() / "config.json"


def state_report() -> dict:
    """Where the state really is — the answer to "why is my config ignored?".

    Printed by `whoami`: with three homes possible (override, portable
    `.state/`, platform directory) a user should never have to guess which one
    is in play, and `config_found: false` says plainly that the defaults are
    the ones running.
    """
    return {
        "home": str(skill_home()),
        "config": str(config_path()),
        "config_found": config_path().exists(),
        "portable": skill_home() == SKILL_ROOT / PORTABLE_DIR,
    }


_CONFIG: dict | None = None


def config() -> dict:
    """The JSON config file, or {} when it is absent or unreadable.

    A broken file must not brick sending: it is reported on stderr and
    ignored, because a working default beats a driver that refuses to start.
    """
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    path = config_path()
    data: dict = {}
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(loaded, dict):
                raise ValueError("expected a JSON object")
            data = loaded
    except Exception as exc:  # any parse/IO problem is the same to us
        print(f"ignoring {path}: {exc}", file=sys.stderr)
        data = {}
    _CONFIG = data
    return data


def setting(env_names, key: str, default):
    """One setting: env var (any alias), else the config file, else the default."""
    names = (env_names,) if isinstance(env_names, str) else tuple(env_names)
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    value = config().get(key)
    if value not in (None, ""):
        return value
    return default


# The workspace id (the `/client/<TEAM>` segment). Defaults to the IBM workspace
# ibm-gbs-lk; another workspace sets `team` in the config file.
TEAM = setting(("SLACK_SKILL_TEAM", "BRAINCLAW_SLACK_TEAM"), "team", "E27SFGS2W")
# Where `login` starts: the workspace entry that redirects into app.slack.com
# through SSO. IBM's is the enterprise entry; a standalone workspace signs in on
# its own host (https://<workspace>.slack.com/), so this is configurable.
LOGIN_URL = setting(("SLACK_SKILL_LOGIN_URL", "BRAINCLAW_SLACK_LOGIN_URL"), "login_url",
                    "https://ibm.enterprise.slack.com/")
# The Slack SPA needs ~15s to render its sidebar in Playwright's Chromium;
# 8s probes returned an empty <body> and looked like a blocked session.
BOOT_MS = int(setting(("SLACK_SKILL_BOOT_MS", "BRAINCLAW_SLACK_BOOT_MS"), "boot_ms", 15000))


def _legacy_profile_dir() -> Path:
    """Where the profile lived when a harness owned the path (BrainClaw).

    Deliberately not routed through _data_base: this must reproduce the old
    logic exactly, including the mistake of using the Linux path on a Mac, or
    an existing profile would not be found and the session would look dead.
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "BrainClaw" / "slack-profile"
    return Path.home() / ".local" / "share" / "brainclaw" / "slack-profile"


def profile_dir() -> Path:
    """The persistent Chromium profile (cookies — this is the session).

    A profile left at the old harness-owned path is moved into this skill's
    home once: the cookies are what keep the user signed in, so a path change
    must not read as "logged out". Same-volume rename, and if it fails the old
    path is used in place rather than losing the session.
    """
    override = setting(("SLACK_SKILL_PROFILE", "BRAINCLAW_SLACK_PROFILE"), "profile", "")
    if override:
        return _resolve(str(override))
    target = skill_home() / "profile"
    if not target.exists():
        legacy = _legacy_profile_dir()
        if legacy.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(legacy, target)
                print(f"moved the Slack profile: {legacy} -> {target}", file=sys.stderr)
            except OSError as exc:
                print(f"could not move {legacy} ({exc}); using it in place", file=sys.stderr)
                return legacy
    return target


def emit(payload: dict) -> None:
    # Windows consoles default to cp1252; CJK message bodies crash the print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


AUTH_EXIT = 3


def fail_auth() -> int:
    """Report an expired/absent session in a shape a harness can act on.

    Exit code 3 is reserved for "needs a human sign-in", so callers can tell it
    apart from a broken selector or a Slack error. The agent's job here is only
    to relay this to the user: the credentials are typed by the human in the
    browser window, never by the agent.
    """
    emit({
        "action": "auth",
        "signed_in": False,
        "profile": str(profile_dir()),
        "hint": "Slack session expired. Ask the user to run the login command in a visible "
                "browser and sign in with the workspace SSO; the agent must never handle the credentials.",
        "hint_zh": "Slack 登录已失效。请运行 `slack_driver.py login`，会弹出浏览器窗口，"
                   "由用户在窗口里用工作区 SSO 登录（凭据只在窗口内输入，不要交给 agent）。",
        "command": f'py -3 "{Path(__file__).resolve()}" login',
    })
    return AUTH_EXIT


def open_context(playwright, *, headless: bool):
    path = profile_dir()
    path.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(path),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        args=["--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble"],
    )


def dismiss_overlays(page, attempts: int = 2) -> bool:
    """Close stray Slack overlays before touching the page.

    An open quick-switcher / message pane marks the rest of the app aria-hidden,
    so every `get_by_role(...)` on the sidebar returns 0 while the UI is visibly
    fine. That false "0 treeitems" once read as a dead Slack session and made
    every command refuse to run with "not signed in" (2026-11-11). Escape is the
    cheap universal closer; the driver never needs an overlay left open.
    """
    closed = False
    for _ in range(attempts):
        if page.get_by_role("combobox", name="Query").count() == 0 and \
           page.get_by_role("combobox", name="To:").count() == 0:
            return closed
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
        closed = True
    return closed


def signed_in(page) -> bool:
    """Land on the IBM workspace and confirm the app actually rendered.

    Detect the rendered app by its sidebar, not by the `New message` button:
    that button only exists on the Home view, so a session that reopens on the
    last DM looked signed-out and every command refused to run (2026-11-10).
    Poll, because the first paint races the cookie check.
    """
    page.goto(f"{SLACK}/{TEAM}", wait_until="domcontentloaded")
    deadline = time.time() + (BOOT_MS + 20_000) / 1000
    while time.time() < deadline:
        # An overlay makes the sidebar invisible to role queries; clear it first.
        dismiss_overlays(page)
        if page.get_by_role("treeitem").count() > 0:
            return True
        if page.get_by_role("searchbox", name="Channel or user name").count() > 0:
            return True
        if page.get_by_role("button", name="New message").count() > 0:
            return True
        # Workspace picker also means the session is alive.
        if page.get_by_role("button", name=re.compile("Switch workspaces", re.I)).count() > 0:
            return True
        page.wait_for_timeout(2000)
    return False


def _first_person_option(page, timeout_ms: int, max_options: int = 5):
    """Wait for a filtered recipient list and return its first person entry.

    A stale, unfiltered list can pre-highlight a channel; pressing Enter before
    the list settles once opened #help-ica20 instead of the person (observed
    2026-11-10). Caller clicks the returned option instead of pressing Enter.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        options = page.get_by_role("option")
        n = options.count()
        if 0 < n <= max_options:
            for i in range(n):
                text = " ".join((options.nth(i).inner_text() or "").split())
                if text and not text.startswith("#") and not text.startswith("Show"):
                    return options.nth(i)
        page.wait_for_timeout(400)
    return None


def _channel_id(page) -> str:
    """Conversation id from the URL: `D…` is a 1:1 DM, `C…`/`G…` is a group."""
    match = re.search(r"/client/[^/]+/([A-Za-z0-9]+)", page.url)
    return match.group(1) if match else ""


def _quick_switch(page, query_text: str) -> list[dict]:
    """Ctrl+K, type the query, return the result rows as dicts.

    Slack tags every row with `data-type`: `member` is a person (opening it gives
    a 1:1 DM), `mpim` is a multi-person DM, `channel` is a channel. That tag —
    not the label, not the order — is what makes "person or group?" decidable
    before clicking. Measured 2026-11-11: an address search returned member
    "Jiang Yu Wu, Direct Message" first and two mpims ("Jiang Yu Wu, Wei Wang",
    "Fang Yu Wu, Jiang Yu Wu") after it, and the old code clicked the first row
    and landed in the wrong kind of conversation.
    """
    dismiss_overlays(page)
    page.keyboard.press("Control+k")
    page.wait_for_timeout(1200)
    query = page.get_by_role("combobox", name="Query")
    if query.count() == 0:
        raise SystemExit("quick switcher did not open (Ctrl+K)")
    query.first.fill(query_text)
    page.wait_for_timeout(1500)

    def _collect() -> list[dict]:
        """Rows from whichever surface Ctrl+K opened.

        In a conversation view Ctrl+K opens the search modal, whose rows carry
        `data-type` (member/mpim/channel). On Home it opens the New-message
        composer page instead, whose destination rows carry no `data-type` but
        can still open a person's DM — ignoring them made the same query resolve
        or not depending on which view the session happened to be on
        (2026-11-11).
        """
        collected: list[dict] = []
        usable = {"member", "mpim", "channel", "private"}
        found = page.locator('li[role="option"][data-type]')
        for idx in range(found.count()):
            node = found.nth(idx)
            kind = (node.get_attribute("data-type") or "").strip()
            # "queryUser"/"querySalesforce" are the "Search for: …" placeholders
            # Slack paints *before* the people results arrive. Treating them as
            # results made the poll exit early and the caller report "no person
            # matched" (2026-11-11).
            if kind not in usable:
                continue
            collected.append(
                {
                    "index": idx,
                    "type": kind,
                    "label": " ".join((node.get_attribute("aria-label") or "").split()),
                    "text": " ".join((node.inner_text() or "").split()),
                }
            )
        if not collected:
            dest = page.locator('[role="option"][data-qa="composer_page__destination_option"]')
            for idx in range(dest.count()):
                node = dest.nth(idx)
                text = " ".join((node.inner_text() or "").split())
                collected.append(
                    {
                        "index": idx,
                        "type": "destination",
                        "label": text,
                        "text": text,
                    }
                )
        return collected

    # Slow links widen the race between typing and Slack's debounced search: a
    # single fill + fixed wait returned an empty list for a person who resolves
    # (2026-11-11). Poll, then retype character by character once — keystrokes
    # reliably trigger the search, a bulk value assignment may not.
    rows = _collect()
    deadline = time.time() + 20
    while not rows and time.time() < deadline:
        page.wait_for_timeout(1000)
        rows = _collect()
    if not rows:
        query.first.fill("")
        query.first.type(query_text, delay=60)
        deadline = time.time() + 20
        while not rows and time.time() < deadline:
            page.wait_for_timeout(1000)
            rows = _collect()
    return rows


def _click_row(page, index: int, timeout_ms: int, label: str = "") -> None:
    """Click a search row.

    Prefer the row's accessible name: the result list re-renders while it loads,
    so an index captured a moment earlier can point at a different row and the
    click lands on the wrong one (or nowhere) — the composer then still shows the
    previous conversation (2026-11-11). Fall back to the index only when no label
    was captured, and retry once: the first click after a view change can also be
    swallowed by the transition.
    """
    row = (
        page.get_by_role("option", name=label).first
        if label
        else page.locator('li[role="option"][data-type]').nth(index)
    )
    try:
        row.click(timeout=timeout_ms)
    except PWTimeout:
        dismiss_overlays(page)
        page.wait_for_timeout(1000)
        row.click(timeout=timeout_ms, force=True)
    page.wait_for_timeout(2500)


def _person_name(label: str) -> str:
    """'Jiang Yu Wu, Direct Message' → 'Jiang Yu Wu'."""
    for suffix in (", Direct Message", ", DM"):
        if label.endswith(suffix):
            return label[: -len(suffix)].strip()
    return label.strip()


def find_rows(page, query_text: str) -> dict:
    """List what a search resolves to — for choosing a group before sending."""
    rows = _quick_switch(page, query_text)
    page.keyboard.press("Escape")
    return {
        "action": "find",
        "query": query_text,
        "rows": [
            {"type": row["type"], "label": row["label"]} for row in rows
            if row["type"] in ("member", "mpim", "channel", "private")
        ],
    }


def open_conversation(
    page,
    recipients: list[str],
    channel: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> str:
    """Open exactly the conversation the caller named; return its channel id.

    Identity is the recipient set or an explicit channel id — never "the first
    search result". One address may only open a `member` row (a 1:1 DM); two or
    more may only open an `mpim` row whose member list contains all of them. The
    channel id is checked again after the click, so a mis-click aborts before
    anything is typed.
    """
    addresses = [r.strip() for r in recipients if r and r.strip()]

    if channel:
        page.goto(f"{SLACK}/{TEAM}/{channel}", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        opened = _channel_id(page)
        # A Slack USER id (W…/U…) is NOT navigable: /client/<team>/<user> silently
        # keeps the previous conversation, so the landing id stays whatever was
        # open (measured 2026-11-11 — the URL never changed). Fail fast with the
        # right instruction instead of pretending it might redirect.
        if channel[:1] in ("W", "U"):
            raise SystemExit(
                f"{channel} looks like a Slack user id; /client/<team>/<user> is ignored by Slack. "
                "Resolve the person with --email instead, or pass the DM channel id (D…)"
            )
        if opened != channel:
            raise SystemExit(f"could not open channel {channel} (landed on {opened or page.url})")
        return opened

    if not addresses:
        raise SystemExit("no recipient: pass --email (repeatable) or --channel")

    if len(addresses) == 1:
        address = addresses[0]
        rows = _quick_switch(page, address)
        person = next((r for r in rows if r["type"] == "member"), None)
        if person is None:
            # Home view: the composer page lists destinations instead. Take the
            # first one (the list is already filtered by the address) and use its
            # own text as the expected name for the post-click verification.
            person = next((r for r in rows if r["type"] == "destination" and r["text"]), None)
        if person is None:
            raise SystemExit(f"no person matched {address} — Slack returned no member row")
        _click_row(page, person["index"], timeout_ms, person.get("label", ""))
        expected = _person_name(person["label"])
        # Slow links leave the previous conversation on screen for seconds and
        # the old URL is still a "D…" channel, so poll until the composer names
        # the intended person instead of sampling once (2026-11-11).
        # A DM switch on a slow link took ~15 s to land here (2026-11-11), so a
        # 15 s cap sat exactly on the boundary and reported a false mismatch.
        deadline = time.time() + 40
        composer_name = ""
        while time.time() < deadline:
            try:
                composer_name = page.get_by_role(
                    "textbox", name=re.compile(r"^Message to")
                ).first.get_attribute("aria-label") or ""
            except Exception:
                composer_name = ""
            if expected and expected.lower() in composer_name.lower():
                break
            page.wait_for_timeout(800)
        opened = _channel_id(page)
        composer_name = ""
        try:
            composer_name = page.get_by_role(
                "textbox", name=re.compile(r"^Message to")
            ).first.get_attribute("aria-label") or ""
        except Exception:
            composer_name = ""
        if not opened.startswith("D"):
            raise SystemExit(
                f"refusing to send: {address} opened {opened or 'an unknown view'}, "
                "which is not a 1:1 DM"
            )
        # A D-channel check cannot catch the self-DM or a swallowed navigation:
        # the click can fail silently and leave the previous DM open, which is
        # still a "D…" channel (2026-11-11 — a message meant for Gabriella
        # landed in Marlon's own DM and passed the check). Require the composer
        # to name the intended person.
        if expected and expected.lower() not in composer_name.lower():
            raise SystemExit(
                f"refusing to send: opened {opened} but the composer says "
                f"{composer_name or '(nothing)'!r}, not {expected!r} — the address did not resolve to that DM"
            )
        return opened

    # Group: the member set is the identity, so every address must resolve to a
    # person before any mpim is considered.
    names: list[str] = []
    rows = _quick_switch(page, addresses[0])
    first = next((r for r in rows if r["type"] == "member"), None)
    if first is None:
        raise SystemExit(f"no person matched {addresses[0]}")
    names.append(_person_name(first["label"]))
    for address in addresses[1:]:
        later = _quick_switch(page, address)
        person = next((r for r in later if r["type"] == "member"), None)
        if person is None:
            raise SystemExit(f"no person matched {address}")
        names.append(_person_name(person["label"]))

    rows = _quick_switch(page, addresses[0])
    candidates = [r for r in rows if r["type"] == "mpim"]
    target = next((r for r in candidates if all(name in r["label"] for name in names)), None)
    if target is None:
        seen = "; ".join(f"{r['label']}" for r in candidates) or "none"
        raise SystemExit(
            "no group DM holds exactly " + ", ".join(names)
            + f" (groups found: {seen}). Run `find --query <name>` and pass --channel <id>"
        )
    _click_row(page, target["index"], timeout_ms, target.get("label", ""))
    opened = _channel_id(page)
    if opened.startswith("D"):
        raise SystemExit(f"refusing to send: opened a 1:1 DM ({opened}) instead of the group")
    return opened


def open_dm(page, email: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    """Open the 1:1 DM for one address (used by read/delete)."""
    open_conversation(page, [email], timeout_ms=timeout_ms)


def composer(page):
    return page.get_by_role("textbox", name=re.compile(r"^Message to")).first


# What Slack changes on the way in: a typed "@Display Name" becomes a mention
# that carries the person's *display name*, and emoji become <img>. Neither
# keeps the characters the caller typed, so a literal probe of the body can
# miss a message that is plainly on screen (2026-11-12: a self-DM body starting
# "@Meng Ning Luo" rendered as "@Marlon Luo" and reported delivered=false).
_EMOJI_CLASS = (
    r"[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u23FF"
    r"\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)


def delivery_chunks(text: str, min_len: int) -> list[str]:
    """The body's pieces with mentions and emoji cut out.

    A mention is `@` plus one to three name tokens ("@Meng Ning Luo"), so every
    width is cut and the results pooled: the greedy cut is the right one for a
    two-token name, and the narrower cuts only add candidates that cannot
    match, which costs nothing because one hit is proof.
    """
    line = text.splitlines()[0].strip()
    out = []
    for width in range(3):
        pattern = re.compile(r"@\S+(?:\s+\S+){0,%d}|%s" % (width, _EMOJI_CLASS))
        for chunk in re.split(pattern, line):
            chunk = chunk.strip(" ,:;—-·。!?")
            if len(chunk) >= min_len:
                out.append(chunk[:40])
    return list(dict.fromkeys(out))


def delivery_probes(text: str) -> list[str]:
    """Strings to look for anywhere in the page to prove a send landed.

    The body itself comes first — exact, and enough for a plain message. When
    it holds a mention or an emoji that literal never appears in the DOM, so
    the surrounding chunks are tried too, at a length long enough not to match
    some other message by accident.
    """
    line = text.splitlines()[0].strip()
    return list(dict.fromkeys([line[:40]] + delivery_chunks(text, 10)))


def latest_message_confirms(page, text: str) -> bool:
    """Fallback proof for a short body: the newest message is the one just sent.

    "@Meng Ning Luo 你好" has no 10-character chunk left after the mention is
    cut, so the page-wide probes cannot see it. Scoped to the newest message a
    3-character chunk is safe: a stray match can only come from that message.
    """
    chunks = delivery_chunks(text, 3)
    if not chunks:
        return False
    try:
        body = page.locator("[data-qa='message-text']").last
        if body.count() == 0:
            return False
        rendered = " ".join((body.inner_text() or "").split())
    except Exception:
        return False
    return any(chunk in rendered for chunk in chunks)


def send_message(
    page,
    recipients: list[str],
    text: str,
    dry_run: bool = False,
    channel: str | None = None,
) -> dict:
    """Send to the conversation the caller named.

    `--email` once → that person's 1:1 DM; `--email` several times → the group DM
    holding exactly those people; `--channel` → that channel, by id. `dry_run`
    opens the conversation, reports its id and type, and types nothing.
    """
    addresses = [r.strip() for r in recipients if r and r.strip()]
    opened = open_conversation(page, addresses, channel=channel)
    kind = "channel" if channel else ("dm" if len(addresses) == 1 else "group")
    if dry_run:
        return {
            "action": "send",
            "emails": addresses,
            "kind": kind,
            "channel": opened,
            "dry_run": True,
            "url": page.url,
            "text": text,
        }
    box = composer(page)
    box.click()
    box.fill(text)                       # fill replaces leftovers; type() appends
    page.wait_for_timeout(800)

    send = page.get_by_role("button", name="Send now")
    if not send.is_enabled():
        raise SystemExit("Send now stayed disabled — the recipient is unresolved or the body is empty")
    send.click()
    page.wait_for_timeout(3000)

    # Poll for the sent text: on a slow link 3 s was not enough to paint the
    # message, so a delivered message was reported as delivered=false
    # (2026-11-11, Gabriella DM). Absence after the window is still "not seen".
    # The probe list matters as much as the window: a mention or an emoji in
    # the body changes what the DOM holds, so try every candidate and accept
    # the first that appears (2026-11-12).
    probes = delivery_probes(text)
    delivered = False
    deadline = time.time() + 20
    while not delivered and time.time() < deadline:
        for probe in probes:
            try:
                if page.get_by_text(probe, exact=False).count() > 0:
                    delivered = True
                    break
            except Exception:
                continue
        if not delivered:
            page.wait_for_timeout(1500)
    if not delivered:
        delivered = latest_message_confirms(page, text)
    return {
        "action": "send",
        "emails": addresses,
        "kind": kind,
        "channel": opened,
        "delivered": delivered,
        "url": page.url,
        "text": text,
    }


def send_dm(page, email: str, text: str, dry_run: bool = False) -> dict:
    """Backwards-compatible single-recipient wrapper."""
    return send_message(page, [email], text, dry_run=dry_run)


def send_batch(page, items: list[dict], delay_ms: int = 1000) -> dict:
    """Send several approved DMs in one browser session (one Slack boot).

    items: [{"email": str, "text": str}, ...] — this file IS the approval, so
    every body must have been presented to the user before writing it. One
    failed recipient does not abort the rest; each item is reported and the
    caller exits non-zero if any item failed.
    """
    results = []
    for item in items:
        if not isinstance(item, dict):
            results.append({"index": len(results), "emails": None, "delivered": False,
                            "error": "item is not an object"})
            continue
        recipients = item.get("emails") or item.get("email") or []
        if isinstance(recipients, str):
            recipients = [recipients]
        recipients = [str(r).strip() for r in recipients if str(r).strip()]
        text = (item.get("text") or "").strip()
        try:
            if not recipients or not text:
                raise SystemExit("item needs non-empty emails and text")
            result = send_message(page, recipients, text)
            results.append({"index": len(results), **result})
        except (SystemExit, PWTimeout) as exc:
            detail = str(exc).splitlines()[0][:200]
            results.append({"index": len(results), "emails": recipients, "delivered": False,
                            "error": detail})
        page.wait_for_timeout(delay_ms)
    sent = sum(1 for r in results if r.get("delivered"))
    return {"action": "send-batch", "total": len(items), "sent": sent, "results": results}


def message_rows(page, match: str):
    return page.get_by_role("listitem").filter(has_text=match)


def delete_dm(page, email: str, match: str) -> dict:
    open_dm(page, email)
    # Same slow-pane problem as read_dm: at 1.5 s nothing is rendered, so the
    # match count was 0 on a message that exists (2026-11-11). Poll for rows.
    deadline = time.time() + 20
    rows = message_rows(page, match)
    total = rows.count()
    while total == 0 and time.time() < deadline:
        page.wait_for_timeout(1500)
        rows = message_rows(page, match)
        total = rows.count()
        if page.get_by_role("listitem").count() > 1:
            # pane has rendered; one more pass is enough
            page.wait_for_timeout(1000)
            rows = message_rows(page, match)
            total = rows.count()
            break
    if total == 0:
        return {"action": "delete", "email": email, "matched": 0, "deleted": 0}

    deleted = 0
    reason = "deleted-all"
    for index in range(total):
        row = message_rows(page, match).first
        row.scroll_into_view_if_needed()
        more = page.get_by_role("button", name="More actions")
        # The hover toolbar is flaky: Slack needs a real pointer move over the
        # row and ~1s to paint it. Retry the hover instead of giving up.
        for attempt in range(4):
            try:
                row.hover(position={"x": 40, "y": 10})
            except Exception:
                row.hover()
            page.wait_for_timeout(1200)
            if more.count() > 0:
                break
        if more.count() == 0:
            reason = "hover-toolbar-never-appeared"
            break
        more.first.click()
        page.wait_for_timeout(800)
        item = page.get_by_role("menuitem", name=re.compile("Delete message", re.I))
        if item.count() == 0:
            page.keyboard.press("Escape")
            reason = "delete-menuitem-missing"
            break
        item.first.click()
        page.wait_for_timeout(1000)
        dialog = page.get_by_role("dialog", name="Delete message")
        if dialog.count() == 0:
            reason = "confirm-dialog-missing"
            break
        dialog.get_by_role("button", name="Delete", exact=True).click()
        page.wait_for_timeout(2000)
        if message_rows(page, match).count() >= total - deleted:
            reason = "message-still-present-after-confirm"
            break
        deleted += 1

    remaining = message_rows(page, match).count()
    return {
        "action": "delete",
        "email": email,
        "matched": total,
        "deleted": deleted,
        "remaining": remaining,
        "reason": reason,
    }


def read_dm(page, email: str, limit: int) -> dict:
    """Return the tail of a conversation.

    The pane loads asynchronously: 1.5 s after `open_dm` it still reads
    "Loading history…" and `role=listitem` holds a single empty shell, so a
    fixed sleep returned [] every time (measured 2026-11-11 — the pane needed
    ~8 s). Poll for real message rows instead, then scroll to the newest.
    """
    open_dm(page, email)

    def _rows() -> list[str]:
        items = page.get_by_role("listitem")
        out = []
        for i in range(items.count()):
            label = items.nth(i).get_attribute("aria-label") or items.nth(i).inner_text()
            text = " ".join((label or "").split())
            if text:
                out.append(text)
        return out

    deadline = time.time() + 20
    messages = _rows()
    while not messages and time.time() < deadline:
        page.wait_for_timeout(1500)
        messages = _rows()

    # Older history renders first; put the newest at the bottom before slicing.
    try:
        page.evaluate(
            "() => { const p = document.querySelector('.p-message_pane');"
            " const s = p && p.querySelector('[data-qa=\"message_list\"], .c-virtual_list__scroll_container');"
            " if (s) s.scrollTop = s.scrollHeight; else if (p) p.scrollTop = p.scrollHeight; }"
        )
        page.wait_for_timeout(1500)
        messages = _rows() or messages
    except Exception:
        pass

    return {"action": "read", "email": email, "messages": messages[-limit:]}


def diagnose(page, seconds: int = 45) -> int:
    """Record what the page is actually doing before blaming Slack.

    Prints a JSON summary: HTTP statuses for the document and API calls, console
    errors, and a timeline of render probes (title, body length, sidebar counts)
    sampled every few seconds. Also writes a screenshot next to the profile so a
    human can see whether the shell is blank, a spinner, a sign-in form or a
    challenge page.
    """
    events: list[dict] = []
    console: list[str] = []

    def on_response(resp):
        try:
            url = resp.url
            status = resp.status
            if status >= 400 or "/api/" in url:
                events.append({"kind": "http", "status": status, "url": url.split("?")[0][:120]})
        except Exception:
            pass

    def on_console(msg):
        try:
            if msg.type in ("error", "warning"):
                console.append(f"{msg.type}: {msg.text[:160]}")
        except Exception:
            pass

    page.on("response", on_response)
    page.on("console", on_console)

    shot = skill_home() / "slack-diagnose.png"
    timeline = []
    for step in range(max(1, seconds // 5)):
        page.wait_for_timeout(5000)
        try:
            timeline.append({
                "t": (step + 1) * 5,
                "url": page.url.split("/client/")[-1][:24],
                "title": page.title()[:60],
                "bodyLen": len(page.inner_text("body")),
                "treeitems": page.get_by_role("treeitem").count(),
                "searchbox": page.get_by_role("searchbox", name="Channel or user name").count(),
                "newmsg": page.get_by_role("button", name="New message").count(),
                "textboxes": page.get_by_role("textbox").count(),
            })
        except Exception as exc:
            timeline.append({"t": (step + 1) * 5, "error": str(exc).splitlines()[0][:120]})

    try:
        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot))
    except Exception:
        shot = None

    http_4xx = [e for e in events if e["status"] >= 400]
    emit({
        "action": "diagnose",
        "profile": str(profile_dir()),
        "screenshot": str(shot) if shot else None,
        "timeline": timeline,
        "http_errors": http_4xx[:12],
        "api_calls": len([e for e in events if e["kind"] == "http" and e["status"] < 400]),
        "console": console[:12],
    })
    return 0


def serve(page) -> int:
    """Resident mode: one browser, many commands, one JSON line per command.

    Each stdin line is a JSON object — {"cmd": "send", "email": ..., "text": ...},
    {"cmd": "read", "email": ..., "limit": 5}, {"cmd": "delete", "email": ...,
    "match": ...}, {"cmd": "whoami"}, {"cmd": "quit"}. Every command answers with
    exactly one JSON line on stdout.

    Why this exists: every cold start pays Slack's ~15 s boot, which is pure
    waste when a session needs several operations. One browser, one boot, then
    as many commands as the caller sends.
    """
    emit({"action": "serve", "ready": True, "url": page.url})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            emit({"action": "error", "error": f"bad json: {exc}"})
            continue

        cmd = (req.get("cmd") or "").lower()
        try:
            if cmd == "quit":
                emit({"action": "quit", "ok": True})
                return 0
            if cmd == "whoami":
                emit({"action": "whoami", "signed_in": signed_in(page), "url": page.url, **state_report()})
            elif cmd == "send":
                raw = req.get("emails") or req.get("email") or []
                if isinstance(raw, str):
                    raw = [raw]
                recipients = [str(r).strip() for r in raw if str(r).strip()]
                text = (req.get("text") or "").strip()
                if not recipients or not text:
                    emit({"action": "send", "delivered": False, "error": "emails and text are required"})
                else:
                    emit(send_message(page, recipients, text,
                                      dry_run=bool(req.get("dry_run")),
                                      channel=(req.get("channel") or None)))
            elif cmd == "delete":
                emit(delete_dm(page, (req.get("email") or "").strip(), (req.get("match") or "").strip()))
            elif cmd == "read":
                emit(read_dm(page, (req.get("email") or "").strip(), int(req.get("limit") or 10)))
            elif cmd == "reload":
                page.reload()
                page.wait_for_timeout(BOOT_MS)
                emit({"action": "reload", "url": page.url})
            else:
                emit({"action": "error", "error": f"unknown cmd {cmd!r}"})
        except (SystemExit, PWTimeout) as exc:
            emit({"action": cmd or "error", "error": str(exc).splitlines()[0][:200]})
        except Exception as exc:  # keep the resident session alive
            emit({"action": cmd or "error", "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}"})
        sys.stdout.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Harness-neutral Slack web driver")
    parser.add_argument("--headless", action="store_true", help="run without a visible window (profile must already be signed in)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="open a visible browser and wait for the Slack session")
    sub.add_parser("whoami", help="report whether the stored profile is signed in")
    sub.add_parser("serve", help="resident mode: read JSON commands from stdin, one JSON reply per command")
    p_diag = sub.add_parser("diagnose", help="record HTTP statuses, console errors and render timeline")
    p_diag.add_argument("--seconds", type=int, default=45, help="how long to watch (default 45)")

    p_send = sub.add_parser("send", help="send to a 1:1 DM (one --email) or a group DM (several)")
    p_send.add_argument(
        "--email",
        action="append",
        dest="email",
        help="recipient address; repeat to open the group DM holding exactly those people",
    )
    p_send.add_argument(
        "--channel",
        help="send to this channel/group id instead of resolving --email",
    )
    p_send.add_argument("--text")
    p_send.add_argument("--text-file")
    p_send.add_argument("--text-stdin", action="store_true")
    p_send.add_argument(
        "--dry-run",
        action="store_true",
        help="open the resolved conversation and report its id/type without typing anything",
    )

    p_batch = sub.add_parser("send-batch", help="send several approved DMs in one browser session")
    p_batch.add_argument("--file", required=True, help="JSON list: [{\"email\": ..., \"text\": ...}, ...]")
    p_batch.add_argument("--delay", type=int, default=1000, help="pause between sends, ms (default 1000)")

    p_del = sub.add_parser("delete", help="delete messages containing a string in a DM")
    p_del.add_argument("--email", required=True)
    p_del.add_argument("--match", required=True)

    p_find = sub.add_parser("find", help="list what a search resolves to (person / group / channel)")
    p_find.add_argument("--query", required=True, help="address or name")

    p_read = sub.add_parser("read", help="read the tail of a DM")
    p_read.add_argument("--email", required=True)
    p_read.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()

    with sync_playwright() as pw:
        context = open_context(pw, headless=args.headless)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            if args.command == "diagnose":
                # Deliberately skips the sign-in gate: the point is to record what
                # an apparently signed-out page is really doing.
                page.goto(f"{SLACK}/{TEAM}", wait_until="domcontentloaded")
                return diagnose(page, args.seconds)

            if args.command == "serve":
                if not signed_in(page) and not signed_in(page):
                    return fail_auth()
                return serve(page)

            if args.command == "login":
                # The workspace entry redirects into app.slack.com through SSO
                # (IBM by default); app.slack.com directly shows the generic
                # sign-in form instead, so never start there.
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
                print(f"login entry: {LOGIN_URL} (workspace {TEAM})", file=sys.stderr)
                wait_s = int(setting("BRAINCLAW_LOGIN_WAIT", "login_wait", 300))
                print(f"Sign in in the window that opened; waiting up to {wait_s}s…", file=sys.stderr)
                deadline = time.time() + wait_s
                while time.time() < deadline:
                    if "/client/" in page.url:
                        emit({"action": "login", "ok": True, "url": page.url, "profile": str(profile_dir())})
                        return 0
                    page.wait_for_timeout(2000)
                emit({"action": "login", "ok": False, "url": page.url, "profile": str(profile_dir())})
                return 1

            if args.command == "whoami":
                if signed_in(page) or signed_in(page):
                    emit({"action": "whoami", "signed_in": True, "url": page.url,
                          "profile": str(profile_dir()), **state_report()})
                    return 0
                return fail_auth()

            # The SPA's first paint races the cookie check, especially in
            # headless mode: a healthy session occasionally reports signed-out
            # on the first probe. One retry costs a boot, a false alarm costs
            # the user a re-login. Only a second failure is reported as an
            # expired session.
            if not signed_in(page) and not signed_in(page):
                return fail_auth()

            if args.command == "send":
                if args.text_file:
                    text = Path(args.text_file).read_text(encoding="utf-8").strip()
                elif args.text_stdin:
                    text = sys.stdin.read().strip()
                else:
                    text = (args.text or "").strip()
                if not text:
                    raise SystemExit("empty body")
                if not args.email and not args.channel:
                    raise SystemExit("pass --email (repeatable) or --channel")
                emit(
                    send_message(
                        page,
                        args.email or [],
                        text,
                        dry_run=args.dry_run,
                        channel=args.channel,
                    )
                )
                return 0

            if args.command == "send-batch":
                raw = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
                if not isinstance(raw, list) or not raw:
                    raise SystemExit("batch file must be a non-empty JSON list of {email, text}")
                report = send_batch(page, raw, delay_ms=args.delay)
                emit(report)
                return 0 if report["sent"] == report["total"] else 1

            if args.command == "delete":
                emit(delete_dm(page, args.email, args.match))
                return 0

            if args.command == "find":
                emit(find_rows(page, args.query))
                return 0

            if args.command == "read":
                emit(read_dm(page, args.email, args.limit))
                return 0

            raise SystemExit(f"unknown command {args.command}")
        except PWTimeout as exc:
            emit({"error": "timeout", "detail": str(exc).splitlines()[0][:200]})
            return 2
        finally:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
