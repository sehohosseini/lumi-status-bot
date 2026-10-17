#!/usr/bin/env python3
"""Telegram alerts for LUMI outages.

Polls the official LUMI status API (https://status.lumi.csc.fi) and sends a
Telegram message when LUMI goes down and again when it is working again.

    python3 lumi_status_bot.py setup             one-time: save bot token + chat id
    python3 lumi_status_bot.py check             one poll (what the scheduler runs)
    python3 lumi_status_bot.py check --dry-run   print what would be sent; send/save nothing
    python3 lumi_status_bot.py test              send the current LUMI status right now
    python3 lumi_status_bot.py chat-id           reply in Telegram with your chat id

Settings come from ~/.lumi-status-bot/config.json, overridden by the environment
variables TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and LUMI_WATCH (comma-separated
component ids), which is how the GitHub Actions workflow passes its secrets.

Standard library only; works with macOS's /usr/bin/python3 (3.9+).
"""

import argparse
import getpass
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

STATUS_PAGE = "https://status.lumi.csc.fi/"
STATUS_API = STATUS_PAGE + "api/status"
EVENT_APIS = (
    (STATUS_PAGE + "api/incidents", "incidents", "Incident"),
    (STATUS_PAGE + "api/maintenance", "maintenance", "Maintenance"),
)

HOME = Path(os.environ.get("LUMI_BOT_HOME", Path.home() / ".lumi-status-bot"))
CONFIG_FILE = HOME / "config.json"
STATE_FILE = HOME / "state.json"
LOG_FILE = HOME / "bot.log"
LOG_MAX_BYTES = 1_000_000

DEFAULTS = {
    # Component ids (from STATUS_API) that together mean "LUMI is working".
    "watch": ["login_nodes", "lumi_c", "lumi_g"],
    # A change must be seen on this many polls in a row before it is reported.
    "confirm_polls": 2,
    # Warn after this many failed attempts in a row to reach the status page.
    "unreachable_after": 3,
}

ICON = {"up": "🟢", "degraded": "🟠", "maintenance": "🔧", "down": "🔴"}
LABEL = {"up": "Operational", "degraded": "Degraded", "maintenance": "Maintenance", "down": "Outage"}
OVERALL = {
    "up": "All Key Systems Operational",
    "degraded": "Partial Outage",
    "maintenance": "Scheduled Maintenance in Progress",
    "down": "Major Outage",
}
HEADLINE = {
    "up": "LUMI is working again",
    "degraded": "LUMI is partly degraded",
    "maintenance": "LUMI is in maintenance",
    "down": "LUMI is down",
}
STATE_WORD = {"up": "up", "degraded": "partly degraded", "maintenance": "in maintenance", "down": "down"}

NETWORK_ERRORS = (OSError, ValueError, KeyError, TypeError)  # URLError/timeouts are OSError


# ── helpers ──────────────────────────────────────────────────────────────

def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.parent / (LOG_FILE.name + ".1"))
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    """Atomic write, readable by the owner only (config holds the bot token)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "lumi-status-telegram-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fmt_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%d %b %H:%M")
    except (AttributeError, ValueError):
        return iso or "?"


# ── Telegram ─────────────────────────────────────────────────────────────

def telegram(token, method, params=None, timeout=30):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            reply = json.load(e)
        except ValueError:
            raise RuntimeError(f"Telegram {method}: HTTP {e.code}") from None
    if not reply.get("ok"):
        raise RuntimeError(f"Telegram {method}: {reply.get('description', 'unknown error')}")
    return reply["result"]


def send(cfg, text):
    telegram(cfg["bot_token"], "sendMessage", {
        "chat_id": cfg["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })


# ── status logic ─────────────────────────────────────────────────────────

def verdict(statuses):
    """Collapse the watched components' statuses into one LUMI-wide status."""
    values = set(statuses.values())
    if not values:
        return "unknown"
    for s in ("down", "maintenance", "degraded"):
        if s in values:
            return s
    return "up" if values == {"up"} else "degraded"


def active_events(watch):
    """Titles of active incidents/maintenance that touch a watched component."""
    lines = []
    for url, key, kind in EVENT_APIS:
        try:
            items = get_json(url).get(key) or []
        except NETWORK_ERRORS:
            continue
        for ev in items:
            if ev.get("status") == "active" and set(ev.get("component_ids") or []) & set(watch):
                lines.append(f"{kind}: {html.escape(str(ev.get('title')))} "
                             f"(since {fmt_time(ev.get('start_time'))})")
    return lines


def build_message(title, now, before, names, events, overall):
    v = verdict(now)
    lines = [f"{ICON.get(v, '⚪')} <b>{html.escape(title)}</b>", ""]
    for cid, st in now.items():
        name = html.escape(names.get(cid, cid))
        cur = f"{ICON.get(st, '⚪')} {LABEL.get(st, st)}"
        old = (before or {}).get(cid)
        if old and old != st:
            lines.append(f"• {name}: {ICON.get(old, '⚪')} {LABEL.get(old, old)} → {cur}")
        else:
            lines.append(f"• {name}: {cur}")
    if events:
        lines += [""] + events
    lines += ["", f"Status page: {OVERALL.get(overall, overall)}", STATUS_PAGE]
    return "\n".join(lines)


def check(cfg, dry_run=False):
    state = load_json(STATE_FILE, {})
    watch = cfg["watch"]

    def notify(text):
        if dry_run:
            print("──── would send ────\n" + text + "\n────────────────────")
            return True
        try:
            send(cfg, text)
            return True
        except (OSError, ValueError, RuntimeError) as e:
            log(f"Telegram send failed (will retry next check): {e}")
            return False

    def save():
        if not dry_run:
            write_json(STATE_FILE, state)

    try:
        status = get_json(STATUS_API)
        comps = {c["id"]: c for g in status["groups"] for c in g["components"]}
    except NETWORK_ERRORS as e:
        state["fetch_failures"] = failures = state.get("fetch_failures", 0) + 1
        log(f"status page unreachable ({failures} in a row): {e!r}")
        if failures >= cfg["unreachable_after"] and not state.get("unreachable_alerted"):
            if notify(f"⚠️ <b>Cannot reach the LUMI status page</b>\n"
                      f"{failures} checks in a row failed. LUMI itself may be fine.\n{STATUS_PAGE}"):
                state["unreachable_alerted"] = True
        save()
        return

    if state.get("unreachable_alerted") and notify("✅ The LUMI status page is reachable again."):
        state["unreachable_alerted"] = False
    state["fetch_failures"] = 0

    missing = [cid for cid in watch if cid not in comps]
    if missing:
        log(f"warning: watched ids not on the status page: {', '.join(missing)}")
    now = {cid: comps[cid].get("status", "unknown") for cid in watch if cid in comps}
    names = {cid: comps[cid].get("name", cid) for cid in now}
    overall = status.get("overall_status", "unknown")
    reported = state.get("reported")
    v = verdict(now)

    if reported is None:
        title = f"Monitoring started: LUMI is {STATE_WORD.get(v, v)}"
        events = active_events(watch) if v != "up" else []
        if notify(build_message(title, now, None, names, events, overall)):
            state["reported"] = now
    elif now == reported:
        state.pop("candidate", None)
        state["candidate_count"] = 0
    else:
        if now == state.get("candidate"):
            state["candidate_count"] = state.get("candidate_count", 0) + 1
        else:
            state["candidate"], state["candidate_count"] = now, 1
        if state["candidate_count"] >= cfg["confirm_polls"]:
            old_v = verdict(reported)
            title = HEADLINE.get(v, f"LUMI status: {v}") if v != old_v else "LUMI status changed"
            events = active_events(watch) if v != "up" else []
            if notify(build_message(title, now, reported, names, events, overall)):
                log(f"notified: {old_v} -> {v}")
                state["reported"] = now
                state.pop("candidate", None)
                state["candidate_count"] = 0
        else:
            log(f"change seen ({state['candidate_count']}/{cfg['confirm_polls']}), waiting to confirm")

    log(f"checked: LUMI {v} {now}")
    save()


def send_current(cfg):
    status = get_json(STATUS_API)
    comps = {c["id"]: c for g in status["groups"] for c in g["components"]}
    now = {cid: comps[cid].get("status", "unknown") for cid in cfg["watch"] if cid in comps}
    names = {cid: comps[cid].get("name", cid) for cid in now}
    v = verdict(now)
    events = active_events(cfg["watch"]) if v != "up" else []
    send(cfg, build_message(f"LUMI is {STATE_WORD.get(v, v)}", now, None, names, events,
                            status.get("overall_status", "unknown")))
    print("Sent.")


# ── setup ────────────────────────────────────────────────────────────────

def wait_for_chat(token, wait_s=180):
    deadline = time.monotonic() + wait_s
    offset = None
    while time.monotonic() < deadline:
        params = {"timeout": 20, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            params["offset"] = offset
        for upd in telegram(token, "getUpdates", params, timeout=30):
            offset = upd["update_id"] + 1
            chat = (upd.get("message") or {}).get("chat")
            if chat:
                telegram(token, "getUpdates", {"offset": offset, "timeout": 0})  # mark read
                return chat
    sys.exit("No message arrived. Send your bot a message in Telegram, then run setup again.")


def setup():
    print("1. In Telegram, open @BotFather, send /newbot and follow the prompts.")
    print("2. Copy the token it gives you (looks like 123456789:AA...).\n")
    token = getpass.getpass("Bot token (input hidden): ").strip()
    me = telegram(token, "getMe")
    print(f"\nToken OK: @{me['username']}")
    print(f"3. Now open a chat with @{me['username']} in Telegram and send it any message.")
    print("   Waiting up to 3 minutes...")
    chat = wait_for_chat(token)
    who = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
    if chat.get("username"):
        who += f" (@{chat['username']})"
    if input(f"\nGot a message from {who}. Send alerts to this chat? [Y/n] ").strip().lower() in ("n", "no"):
        sys.exit("Cancelled; nothing saved.")
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG_FILE, {}))  # keep any custom watch list on re-setup
    cfg.update(bot_token=token, chat_id=chat["id"])
    write_json(CONFIG_FILE, cfg)
    send(cfg, "✅ LUMI status bot connected. You'll get a message when LUMI goes down "
              "and when it starts working again.")
    print(f"Saved {CONFIG_FILE} and sent a test message.")


def reply_chat_ids(token):
    """Answer everyone who messaged the bot with their chat id.

    For setups where `setup` can't run (GitHub Actions); the id goes to Telegram,
    never to the (possibly public) log.
    """
    chats, offset = set(), None
    for upd in telegram(token, "getUpdates", {"timeout": 0, "allowed_updates": json.dumps(["message"])}):
        offset = upd["update_id"] + 1
        chat = (upd.get("message") or {}).get("chat")
        if chat:
            chats.add(chat["id"])
    for cid in chats:
        telegram(token, "sendMessage", {
            "chat_id": cid,
            "parse_mode": "HTML",
            "text": f"Your chat id is <code>{cid}</code>\nSave it as the GitHub secret TELEGRAM_CHAT_ID.",
        })
    if offset is not None:
        telegram(token, "getUpdates", {"offset": offset, "timeout": 0})  # mark read
    if not chats:
        sys.exit("No messages found. Send your bot any message in Telegram, then run this again.")
    print(f"Replied to {len(chats)} chat(s) with their chat id.")


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG_FILE, {}))
    # Environment (e.g. GitHub Actions secrets) wins over config.json; empty means unset.
    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        cfg["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    if os.environ.get("TELEGRAM_CHAT_ID", "").strip():
        cfg["chat_id"] = os.environ["TELEGRAM_CHAT_ID"].strip()
    watch = [s.strip() for s in os.environ.get("LUMI_WATCH", "").split(",") if s.strip()]
    if watch:
        cfg["watch"] = watch
    return cfg


def main():
    p = argparse.ArgumentParser(description="Telegram alerts when LUMI goes down / comes back.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="save bot token + chat id (interactive)")
    c = sub.add_parser("check", help="poll once and notify on change (default)")
    c.add_argument("--dry-run", action="store_true", help="print the message; send/save nothing")
    sub.add_parser("test", help="send the current LUMI status now")
    sub.add_parser("chat-id", help="reply in Telegram to whoever messaged the bot with their chat id")
    args = p.parse_args()

    if args.cmd == "setup":
        setup()
        return
    dry_run = getattr(args, "dry_run", False)
    cfg = load_config()
    if args.cmd == "chat-id":
        if not cfg.get("bot_token"):
            sys.exit("No bot token: set TELEGRAM_BOT_TOKEN (GitHub secret) or run setup.")
        reply_chat_ids(cfg["bot_token"])
        return
    if not dry_run and not (cfg.get("bot_token") and cfg.get("chat_id")):
        print("No bot configured yet: run `setup`, or set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        # On GitHub don't fail every scheduled run while the secrets are still being added.
        sys.exit(0 if os.environ.get("GITHUB_ACTIONS") == "true" and args.cmd != "test" else 1)
    if args.cmd == "test":
        send_current(cfg)
    else:
        check(cfg, dry_run)


if __name__ == "__main__":
    main()
