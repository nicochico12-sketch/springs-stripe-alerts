# Composio-free Stripe → Slack payment alerts

**Date:** 2026-07-16 · **Status:** approved by Nico (chat)

## Why

All 5 workflow failures in the last 200 runs (Jul 7–16) were the Composio CLI
installer flaking ("Failed to determine the latest CLI release with a
composio-linux-x64.zip asset" / HTTP 429), each one emailing Nico. Composio also
holds a full-access Stripe connection and a workspace-wide Slack identity —
more access than this job needs.

## Design

Replace Composio with direct REST calls. Same repo, same 15-min GitHub Actions
cron, same message format, same channel-scan dedup (fail-closed), same 72h
self-healing backfill, same on-failure Slack warning.

- **Stripe:** restricted API key, read-only on exactly **Charges, Checkout
  Sessions, Invoices**. Endpoints used:
  - `GET /v1/charges?limit=25`
  - `GET /v1/checkout/sessions?payment_intent=pi_…&limit=1`
  - `GET /v1/checkout/sessions/{id}/line_items`
  - `GET /v1/invoices/{id}`
  - The payment-link line-items path is deleted — it only existed because
    Composio redacted session ids. Direct API doesn't, so line items come from
    the session itself (also covers payments made without a payment link).
- **Slack:** the existing `payroll_helper` bot (`U0B890KV3E1`).
  `chat.postMessage` to post; `conversations.history` for dedup. Requires the
  `groups:history` scope (being added) and membership in #stripepayment
  (`C0B6FENKU2K`, private).
- **Secrets (GitHub encrypted):** `STRIPE_API_KEY`, `SLACK_BOT_TOKEN`.
  `COMPOSIO_API_KEY` is deleted after cutover; the Composio Stripe connection
  gets disconnected at composio.dev.
- **Workflow:** checkout → run script. No installs (stdlib `urllib` only), so
  the flaky-installer failure mode is structurally gone. Failure step posts the
  warning via a plain `curl` to `chat.postMessage`.

## Behavior kept verbatim

- Dedup = the channel: scan ~60 recent messages for `pi_/py_/ch_` refs; each
  post embeds `_Ref <key>_`. **Fail-closed**: zero messages read → abort.
- 72h lookback backfills anything missed; charges with no stable key skipped.
- Invoice branch for subscription charges (item/email/name live on the invoice).
- `ALERTS_DRYRUN=1` prints instead of posting.

## Cutover plan

1. Build on branch `composio-free` (cron only fires from main → no failure
   emails while secrets are missing).
2. Nico: create the restricted Stripe key; add `groups:history` to the payroll
   app + reinstall; `/invite @payroll_helper` to #stripepayment; `gh secret set`
   both secrets (pasted in his own terminal).
3. `workflow_dispatch` on the branch in dry-run, then live; verify post + dedup.
4. Merge to main; watch 2 consecutive scheduled runs; then delete
   `COMPOSIO_API_KEY` and disconnect Composio's Stripe connection.

## Testing

- Local `ALERTS_DRYRUN=1` run against real Stripe data (needs the key).
- One manual live run on the branch; confirm no duplicate posts on the next run.
- Force-fail path (`force_fail=true` input) re-tested to confirm the curl-based
  Slack warning posts.
