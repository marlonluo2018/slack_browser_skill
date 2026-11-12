#!/usr/bin/env python3
"""One-step setup: Playwright, its Chromium build, and a config file if missing.

    py -3 scripts/install.py          (Windows)
    python3 scripts/install.py        (macOS / Linux)

Idempotent - a usable environment is reported and left alone, so it is safe to
re-run after an update. It never asks for credentials and never signs in: the
sign-in stays a human step, done with the login command.

Everything it decides (where the config and profile live) comes from
slack_driver itself rather than a second copy of the same rules, so the two
cannot drift apart.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)
HERE = Path(__file__).resolve().parent


def say(text: str) -> None:
    print(f"- {text}")


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        say(f"python {sys.version.split()[0]} is too old; this needs "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")
        return 1
    say(f"python {sys.version.split()[0]}")

    try:
        import playwright  # noqa: F401
    except ImportError:
        say("installing playwright")
        done = subprocess.run([sys.executable, "-m", "pip", "install", "playwright"])
        if done.returncode:
            say("pip install failed - fix that, then run this again")
            return done.returncode
    else:
        say("playwright already installed")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            chromium = Path(pw.chromium.executable_path)
    except Exception as exc:  # noqa: BLE001 - any failure means "not usable yet"
        say(f"playwright is installed but not usable: {exc}")
        return 1

    if chromium.exists():
        say(f"chromium present: {chromium}")
    else:
        say("downloading the Chromium build (~130 MB, one time)")
        done = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
        if done.returncode or not chromium.exists():
            say("chromium download failed - re-run this, or run it by hand: "
                f"{sys.executable} -m playwright install chromium")
            return done.returncode or 1
        say(f"chromium ready: {chromium}")

    # Import late: slack_driver needs playwright, which may have just been installed.
    sys.path.insert(0, str(HERE))
    import slack_driver as driver

    config = driver.config_path()
    if config.exists():
        say(f"config present: {config}")
    else:
        template = driver.SKILL_ROOT / "config.example.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        say(f"created {config} from config.example.json - edit it for another workspace")

    state = driver.state_report()
    say(f"state  home={state['home']} portable={state['portable']}")
    channel = driver.setting(
        ("SLACK_SKILL_BROWSER_CHANNEL", "BRAINCLAW_SLACK_BROWSER_CHANNEL"),
        "browser_channel", "") or ""
    if channel:
        say(f"browser channel: {channel} (must be installed on this machine)")
    else:
        say("browser channel: bundled chromium")

    print()
    print("Next: sign in once, in a visible window -")
    print(f"    {sys.executable} {HERE / 'slack_driver.py'} login")
    print("then check it, and try a DM to yourself:")
    print(f"    {sys.executable} {HERE / 'slack_driver.py'} whoami")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
