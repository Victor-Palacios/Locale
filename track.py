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


def load_last() -> str | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text()).get("scheduledAt")
    except Exception:
        return None


def save_last(iso_utc: str) -> None:
    # Deliberately does NOT store the tracking URL: the state file is committed to a
    # public repo, and the URL is a secret.
    STATE_FILE.write_text(json.dumps({
        "scheduledAt": iso_utc,
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2) + "\n")


def send_change_email(prev: str, curr: str, url: str) -> None:
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT", user)

    prev_h, curr_h = humanize(prev), humanize(curr)
    subject = f"[Locale] Arrival time changed: {prev_h} -> {curr_h}"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(
        f"Scheduled time of arrival changed.\n\n"
        f"Previous: {prev_h}  ({prev})\n"
        f"Current:  {curr_h}  ({curr})\n\n"
        f"Tracking page: {url}\n"
    )
    msg.add_alternative(
        f"<html><body style='font-family:-apple-system,sans-serif;line-height:1.5'>"
        f"<h2>Scheduled time of arrival changed</h2>"
        f"<p><b>Previous:</b> {prev_h} <span style='color:#888'>({prev})</span><br>"
        f"<b>Current:</b> {curr_h} <span style='color:#888'>({curr})</span></p>"
        f"<p><a href='{url}'>View tracking page</a></p>"
        f"</body></html>",
        subtype="html",
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(user, pw)
        server.send_message(msg)
    log.info("Sent change-notification email to %s", recipient)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    url = os.environ.get("TRACK_URL")
    if not url:
        log.error("TRACK_URL env var is required")
        return 2

    scheduled, _order = fetch_scheduled_time(url)
    pretty = humanize(scheduled)
    log.info("Scheduled time of arrival: %s  (raw: %s)", pretty, scheduled)

    prev = load_last()
    if prev is None:
        log.info("First run — recording baseline, no email sent.")
    elif prev == scheduled:
        log.info("No change since last check (%s).", prev)
    else:
        log.info("Time changed: %s -> %s. Sending email.", prev, scheduled)
        send_change_email(prev, scheduled, url)

    save_last(scheduled)
    return 0


if __name__ == "__main__":
    sys.exit(main())
