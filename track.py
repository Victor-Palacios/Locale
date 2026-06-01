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

# State path is overridable so a local cron runner can use its own file and not
# clobber the git-tracked state the GitHub Action commits.
STATE_FILE = Path(os.environ.get("STATE_FILE") or (Path(__file__).parent / "last_time.json"))
DISPLAY_TZ = ZoneInfo("America/Los_Angeles")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
# Only alert when the ETA moves at least this many minutes vs. the last alert.
ALERT_THRESHOLD_MIN = int(os.environ.get("ALERT_THRESHOLD_MIN", "5"))
INIT_DATA_RE = re.compile(r'window\.INIT_DATA\s*=\s*JSON\.parse\(window\.atob\("([^"]+)"\)\)')


def fetch_order(url: str) -> dict:
    """Return the full order dict (with `eta` and `scheduledAt`) for the order behind `url`."""
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (locale-tracker)"})
    resp.raise_for_status()
    m = INIT_DATA_RE.search(resp.text)
    if not m:
        raise RuntimeError("INIT_DATA blob not found in page HTML")
    payload = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    order = payload.get("order") or {}
    if not (order.get("eta") or order.get("scheduledAt")):
        raise RuntimeError(f"No eta/scheduledAt in payload: {order!r}")
    return order


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


def minute_of(iso_utc: str) -> datetime:
    """Truncate an ISO timestamp to the minute (drops second-level jitter)."""
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).replace(second=0, microsecond=0)


def save_state(monitored: str, monitored_label: str, last_alerted: str,
               history: list[dict], checks: list[dict], now_iso: str) -> None:
    # Deliberately does NOT store the tracking URL: the state file is committed to a
    # public repo, and the URL is a secret.
    # - lastAlerted: the eta that triggered the last alert (threshold is measured from this)
    # - monitoredLabel: most recent displayed minute (for readability)
    # - history: change-only log (what the emails show)
    # - checks:  EVERY poll, for verification/debugging
    STATE_FILE.write_text(json.dumps({
        "monitored": monitored,
        "monitoredLabel": monitored_label,
        "lastAlerted": last_alerted,
        "checkedAt": now_iso,
        "history": history,
        "checks": checks,
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

    order = fetch_order(url)
    # The live, "Updated live" arrival time is `eta`; `scheduledAt` is the fixed slot.
    eta = order.get("eta")
    scheduled = order.get("scheduledAt")
    monitored = eta or scheduled
    pretty = humanize(monitored)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.info("Arrival ETA: %s  (eta=%s scheduledAt=%s)", pretty, eta, scheduled)

    state = load_state()
    # Alerts are measured against the eta that triggered the *last alert*, so small
    # second/minute jitter in the live eta doesn't spam: only moves of at least
    # ALERT_THRESHOLD_MIN minutes notify.
    last_alerted = state.get("lastAlerted")
    history = state.get("history", [])
    checks = state.get("checks", [])

    # Verification log: record EVERY poll, whether or not anything changed.
    checks.append({"checkedAt": now_iso, "eta": eta, "scheduledAt": scheduled, "label": pretty})

    if last_alerted is None:
        subject = "LOCALE: ETA"
        log.info("First run — emailing the ETA.")
    else:
        delta_min = abs((minute_of(monitored) - minute_of(last_alerted)).total_seconds()) / 60
        if delta_min < ALERT_THRESHOLD_MIN:
            log.info("ETA within threshold (moved %.0f < %d min). No alert. Logged poll #%d.",
                     delta_min, ALERT_THRESHOLD_MIN, len(checks))
            save_state(monitored, pretty, last_alerted, history, checks, now_iso)
            return 0
        subject = "LOCALE: new ETA"
        log.info("ETA moved %.0f min (>= %d): %s -> %s. Sending email.",
                 delta_min, ALERT_THRESHOLD_MIN, humanize(last_alerted), pretty)

    # First run or a threshold-crossing change: log it, email it, text a short line.
    history.append({"checkedAt": now_iso, "eta": monitored, "label": pretty})
    text_body, html_body = format_log(history)
    send_email(subject, text_body, html_body)
    send_sms(f"{subject} — {pretty}")

    save_state(monitored, pretty, monitored, history, checks, now_iso)
    return 0


if __name__ == "__main__":
    sys.exit(main())
