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
# IBM workspace: ibm-gbs-lk (E27SFGS2W). Override with BRAINCLAW_SLACK_TEAM.
TEAM = os.environ.get("BRAINCLAW_SLACK_TEAM", "E27SFGS2W")
# The Slack SPA needs ~15s to render its sidebar in Playwright's Chromium;
# 8s probes returned an empty <body> and looked like a blocked session.
BOOT_MS = int(os.environ.get("BRAINCLAW_SLACK_BOOT_MS", "15000"))


def profile_dir() -> Path:
    override = os.environ.get("BRAINCLAW_SLACK_PROFILE")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "BrainClaw" / "slack-profile"
    return Path.home() / ".local" / "share" / "brainclaw" / "slack-profile"


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
                "browser and sign in with IBM SSO; the agent must never handle the credentials.",
        "hint_zh": "Slack 登录已失效。请运行 `slack_driver.py login`，会弹出浏览器窗口，"
                   "由用户在窗口里用 IBM SSO 登录（凭据只在窗口内输入，不要交给 agent）。",
        "command": f'py -3 assistant_brain/skills/slack_browser_skill/scripts/slack_driver.py login',
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


def open_dm(page, email: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    """Open the DM for an exact address. Email first; names collide.

    Preferred path: the Ctrl+K quick switcher finds an existing conversation by
    address and switches straight to it. The New-message panel is the fallback
    for people with no DM yet; its To: box also restores stale draft state
    (observed 2026-11-10), so fill, wait for the list to filter, then click the
    person row rather than pressing Enter.
    """
    dismiss_overlays(page)
    page.keyboard.press("Control+k")
    page.wait_for_timeout(1200)
    query = page.get_by_role("combobox", name="Query")
    if query.count() > 0:
        query.first.fill(email)
        person = _first_person_option(page, 10_000, max_options=6)
        if person is not None:
            person.click(timeout=timeout_ms)
            page.wait_for_timeout(2500)
        else:
            page.keyboard.press("Escape")
    if page.get_by_role("textbox", name=re.compile(r"^Message to")).count() > 0:
        return

    if page.get_by_role("combobox", name="To:").count() == 0:
        page.get_by_role("button", name="New message").first.click(timeout=timeout_ms)
        page.get_by_role("combobox", name="To:").wait_for(state="visible", timeout=timeout_ms)
    combo = page.get_by_role("combobox", name="To:").first
    combo.fill(email)                    # fill replaces restored draft text
    person = _first_person_option(page, timeout_ms, max_options=2)
    if person is not None:
        person.click(timeout=timeout_ms)
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.get_by_role("textbox", name=re.compile(r"^Message to")).count() > 0:
            return
        # Ambiguous entry trap: clear the field and Slack opens the existing DM.
        notice = page.get_by_text(re.compile("multiple matches", re.I))
        if notice.count() > 0:
            for _ in range(len(email) + 8):
                combo.press("Backspace")
            page.wait_for_timeout(1500)
            if page.get_by_role("textbox", name=re.compile(r"^Message to")).count() > 0:
                return
        page.wait_for_timeout(500)
    raise SystemExit(f"could not resolve a DM for {email}")


def composer(page):
    return page.get_by_role("textbox", name=re.compile(r"^Message to")).first


def send_dm(page, email: str, text: str) -> dict:
    open_dm(page, email)
    box = composer(page)
    box.click()
    box.fill(text)                       # fill replaces leftovers; type() appends
    page.wait_for_timeout(800)

    send = page.get_by_role("button", name="Send now")
    if not send.is_enabled():
        raise SystemExit("Send now stayed disabled — the recipient is unresolved or the body is empty")
    send.click()
    page.wait_for_timeout(3000)

    probe = text.splitlines()[0][:40]
    delivered = page.get_by_text(probe, exact=False).count() > 0
    return {
        "action": "send",
        "email": email,
        "delivered": delivered,
        "url": page.url,
        "text": text,
    }


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
            results.append({"index": len(results), "email": None, "delivered": False,
                            "error": "item is not an object"})
            continue
        email = (item.get("email") or "").strip()
        text = (item.get("text") or "").strip()
        try:
            if not email or not text:
                raise SystemExit("item needs non-empty email and text")
            result = send_dm(page, email, text)
            results.append({"index": len(results), **result})
        except (SystemExit, PWTimeout) as exc:
            detail = str(exc).splitlines()[0][:200]
            results.append({"index": len(results), "email": email, "delivered": False, "error": detail})
        page.wait_for_timeout(delay_ms)
    sent = sum(1 for r in results if r.get("delivered"))
    return {"action": "send-batch", "total": len(items), "sent": sent, "results": results}


def message_rows(page, match: str):
    return page.get_by_role("listitem").filter(has_text=match)


def delete_dm(page, email: str, match: str) -> dict:
    open_dm(page, email)
    page.wait_for_timeout(1500)
    rows = message_rows(page, match)
    total = rows.count()
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
    open_dm(page, email)
    page.wait_for_timeout(1500)
    rows = page.get_by_role("listitem")
    count = rows.count()
    out = []
    for i in range(max(0, count - limit), count):
        label = rows.nth(i).get_attribute("aria-label") or rows.nth(i).inner_text()
        text = " ".join((label or "").split())
        if text:
            out.append(text)
    return {"action": "read", "email": email, "messages": out}


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

    shot = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BrainClaw" / "slack-diagnose.png"
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
                emit({"action": "whoami", "signed_in": signed_in(page), "url": page.url})
            elif cmd == "send":
                email = (req.get("email") or "").strip()
                text = (req.get("text") or "").strip()
                if not email or not text:
                    emit({"action": "send", "delivered": False, "error": "email and text are required"})
                else:
                    emit(send_dm(page, email, text))
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

    p_send = sub.add_parser("send", help="send a DM")
    p_send.add_argument("--email", required=True)
    p_send.add_argument("--text")
    p_send.add_argument("--text-file")
    p_send.add_argument("--text-stdin", action="store_true")

    p_batch = sub.add_parser("send-batch", help="send several approved DMs in one browser session")
    p_batch.add_argument("--file", required=True, help="JSON list: [{\"email\": ..., \"text\": ...}, ...]")
    p_batch.add_argument("--delay", type=int, default=1000, help="pause between sends, ms (default 1000)")

    p_del = sub.add_parser("delete", help="delete messages containing a string in a DM")
    p_del.add_argument("--email", required=True)
    p_del.add_argument("--match", required=True)

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
                # Enterprise entry redirects into app.slack.com via IBM SSO;
                # app.slack.com directly shows the generic sign-in form instead.
                page.goto("https://ibm.enterprise.slack.com/", wait_until="domcontentloaded")
                wait_s = int(os.environ.get("BRAINCLAW_LOGIN_WAIT", "300"))
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
                          "profile": str(profile_dir())})
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
                emit(send_dm(page, args.email, text))
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
