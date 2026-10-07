# Springs Pickleball — Stripe → Slack payment alerts

Posts **enriched Stripe payment notifications** to the Slack `#stripepayment` channel:
amount, **item/tier**, payer, organization, email, date, and Stripe receipt link.

Runs as a GitHub Actions workflow (`.github/workflows/alerts.yml`) **every 5 min, started by Supabase pg_cron**
(project `springs-marketing`, edge function `scheduler`, via `workflow_dispatch`). GitHub's own `schedule`
trigger is throttled on this account to ~6-7 runs a day, so the workflow's hourly cron is only a backup —
see *Scheduling* below.
The script enriches each charge via **charge → payment_intent → checkout session → line item**,
because payment-link charges carry no product info on the charge itself.

## How it stays correct
- **Dedup = the Slack channel.** Each post embeds `Charge <id>`; every run skips IDs already posted.
- **Self-healing.** 72h lookback backfills anything missed during downtime — no payment lost, no duplicates.

## Config
- Auth: two GitHub Actions secrets (never in code):
  - **`STRIPE_API_KEY`** — a *restricted* Stripe key, read-only on Charges, Checkout Sessions, and Invoices. It cannot move money.
  - **`SLACK_BOT_TOKEN`** — the `payroll_helper` bot (`chat:write` + `groups:history`, member of #stripepayment).
- The script is stdlib-only Python — no installs, no third-party services in the data path.
- Target channel + window are constants at the top of `stripe_payment_alerts.py`.

## Scheduling
- **Clock:** pg_cron job `springs-stripe-alerts` (`*/5`) in Supabase project `springs-marketing` → edge function
  `scheduler` → `POST /repos/nicochico12-sketch/springs-stripe-alerts/actions/workflows/alerts.yml/dispatches`.
  Code + runbook: `supabase/functions/scheduler/` in the springs-marketing repo. Every tick is logged to
  `public.scheduler_runs` there.
- **Dispatch token:** fine-grained PAT, this repo only, *Actions: read and write*, stored in Supabase Vault as
  `scheduler_github_token` (not a GitHub secret). Rotate by editing it in Vault — nothing to redeploy.
- **Backup + dead-man:** the hourly GitHub cron still runs the alerts whenever GitHub lets it, then checks that a
  dispatched run happened in the last 30 min. If not, it posts a ⚠️ into #stripepayment and fails red (GitHub emails).
- **Verify:** `gh run list -R nicochico12-sketch/springs-stripe-alerts --workflow alerts.yml --limit 20 --json createdAt,event`
  — dispatched runs show event `workflow_dispatch`.

No secrets live in this repo. Safe to keep public (which also gives unlimited free Actions minutes).
