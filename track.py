"""Poll an order.is tracking page and email when the scheduled arrival time changes.

State is kept in `last_time.json` next to this script so the GitHub Action can commit it
between runs. On the first run there's nothing to compare against, so no email is sent.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("locale-tracker")

STATE_FILE = Path(__file__).parent / "last_time.json"
DISPLAY_TZ = ZoneInfo("America/Los_Angeles")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
INIT_DATA_RE = re.compile(r'window\.INIT_DATA\s*=\s*JSON\.parse\(window\.atob\("([^"]+)"\)\)')


def fetch_scheduled_time(url: str) -> tuple[str, dict]:
    """Return (ISO-8601 UTC timestamp, full order dict) for the order behind `url`."""
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (locale-tracker)"})
    resp.raise_for_status()
    m = INIT_DATA_RE.search(resp.text)
    if not m:
        raise RuntimeError("INIT_DATA blob not found in page HTML")
    payload = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    order = payload.get("order") or {}
    scheduled = order.get("scheduledAt") or order.get("eta")
    if not scheduled:
        raise RuntimeError(f"No scheduledAt/eta in payload: {order!r}")
    return scheduled, order


def humanize(iso_utc: str) -> str:
    """Render '2026-06-01T21:21:04Z' as 'Today at 2:21 PM' in DISPLAY_TZ."""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(DISPLAY_TZ)
    now = datetime.now(DISPLAY_TZ)
    if dt.date() == now.date():
        day = "Today"
    elif (dt.date() - now.date()).days == 1:
        day = "Tomorrow"
    else:
        day = dt.strftime("%A, %b %-d")
    return f"{day} at {dt.strftime('%-I:%M %p')}"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(scheduled: str, history: list[dict], now_iso: str) -> None:
    # Deliberately does NOT store the tracking URL: the state file is committed to a
    # public repo, and the URL is a secret.
    STATE_FILE.write_text(json.dumps({
        "scheduledAt": scheduled,
        "checkedAt": now_iso,
        "history": history,
    }, indent=2) + "\n")


def format_log(history: list[dict]) -> tuple[str, str]:
    """Render the running log of observed ETAs as (plain text, html)."""
    text_lines, html_rows = [], []
    for h in history:
        checked = (
            datetime.fromisoformat(h["checkedAt"])
            .astimezone(DISPLAY_TZ)
            .strftime("%Y-%m-%d %-I:%M %p %Z")
        )
        text_lines.append(f"{checked}   ->   {h['label']}   ({h['eta']})")
        html_rows.append(
            f"<tr><td style='padding:2px 16px 2px 0;color:#888'>{checked}</td>"
            f"<td style='padding:2px 0'><b>{h['label']}</b> "
            f"<span style='color:#888'>({h['eta']})</span></td></tr>"
        )
    text = "ETA log (each entry = an observed time):\n\n" + "\n".join(text_lines) + "\n"
    html = (
        "<html><body style='font-family:-apple-system,sans-serif;line-height:1.5'>"
        "<h2>ETA log</h2><table style='border-collapse:collapse'>"
        + "".join(html_rows)
        + "</table></body></html>"
    )
    return text, html


def send_email(subject: str, text_body: str, html_body: str) -> None:
    """Send via Gmail SMTP. Sender and receiver are both the authenticated account."""
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = user  # sender and receiver are both me
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(user, pw)
        server.send_message(msg)
    log.info("Sent email to %s: %s", user, subject)


def send_sms(short_text: str) -> None:
    """Optionally text a short alert via a carrier email-to-SMS gateway.

    No-op unless SMS_TO is set (e.g. "5551234567@tmomail.net" for Mint/T-Mobile).
    SMS truncates hard, so this sends one short text-only line, not the full log.
    """
    sms_to = os.environ.get("SMS_TO")
    if not sms_to:
        return
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"]

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = sms_to
    msg["Subject"] = ""  # keep the message to a single line in the SMS body
    msg.set_content(short_text)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(user, pw)
        server.send_message(msg)
    log.info("Sent SMS to %s: %s", sms_to, short_text)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    url = os.environ.get("TRACK_URL")
    if not url:
        log.error("TRACK_URL env var is required")
        return 2

    scheduled, _order = fetch_scheduled_time(url)
    pretty = humanize(scheduled)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.info("Scheduled time of arrival: %s  (raw: %s)", pretty, scheduled)

    state = load_state()
    prev = state.get("scheduledAt")
    history = state.get("history", [])

    if prev is None:
        subject = "LOCALE: ETA"
        log.info("First run — emailing the ETA.")
    elif prev == scheduled:
        log.info("No change since last check (%s).", prev)
        save_state(scheduled, history, now_iso)
        return 0
    else:
        subject = "LOCALE: new ETA"
        log.info("Time changed: %s -> %s. Sending email.", prev, scheduled)

    # First run or a change: append to the log, email the full log, and text a short line.
    history.append({"checkedAt": now_iso, "eta": scheduled, "label": pretty})
    text_body, html_body = format_log(history)
    send_email(subject, text_body, html_body)
    send_sms(f"{subject} — {pretty}")

    save_state(scheduled, history, now_iso)
    return 0


if __name__ == "__main__":
    sys.exit(main())
