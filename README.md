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
- Auth: Composio API key, stored as the GitHub Actions secret **`COMPOSIO_API_KEY`** (never in code).
- Target channel + window are constants at the top of `stripe_payment_alerts.py`.

No secrets live in this repo. Safe to keep public (which also gives unlimited free Actions minutes).
