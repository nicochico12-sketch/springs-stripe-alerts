# Springs Pickleball — Stripe → Slack payment alerts

Posts **enriched Stripe payment notifications** to the Slack `#stripepayment` channel:
amount, **item/tier**, payer, organization, email, date, and Stripe receipt link.

Runs on a schedule via GitHub Actions (`.github/workflows/alerts.yml`, every ~15 min).
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

No secrets live in this repo. Safe to keep public (which also gives unlimited free Actions minutes).
