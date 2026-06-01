# Locale arrival-time tracker

Polls an [order.is](http://order.is) tracking page every 20 minutes and emails
when the **Scheduled time of arrival** changes. Runs as a GitHub Action and
auto-disables itself ~9 hours after the first run.

## Local run

```bash
source locale_venv/bin/activate
pip install -r requirements.txt

export TRACK_URL="http://order.is/your-weekly-code"   # the order.is link (kept secret)
export GMAIL_USER="vicpal1989@gmail.com"
export GMAIL_APP_PASSWORD="..."          # Gmail app password
python track.py
```

The first run writes `last_time.json` and does not email. Each subsequent run
compares against that file and emails on change.

## GitHub Action setup

1. Push this directory to a GitHub repo.
2. In **Settings → Secrets and variables → Actions → Secrets**:
   - `TRACK_URL` = the order.is link for the current week.
   - `GMAIL_USER` and `GMAIL_APP_PASSWORD`.
3. The workflow at `.github/workflows/track.yml` runs every 20 min on the
   default branch. Trigger it once via **Run workflow** to lay down the
   `.first_run_at` timestamp; it then auto-disables ~9h later.

When the link rotates next week, update the `TRACK_URL` repo secret and
re-enable the workflow (Actions → Track scheduled arrival time → Enable).
