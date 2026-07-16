#!/usr/bin/env python3
"""
Stripe -> Slack enriched payment alerts for Springs Pickleball.

Posts enriched alerts (amount, item/tier, payer, organization, email, receipt) to
the #stripepayment Slack channel for each new SUCCEEDED Stripe payment.

Talks to Stripe and Slack directly (stdlib only — no CLI, no pip installs):
  - STRIPE_API_KEY: a RESTRICTED key, read-only on Charges, Checkout Sessions,
    and Invoices. Nothing else. It cannot move money.
  - SLACK_BOT_TOKEN: the payroll_helper bot. Needs chat:write + groups:history
    and membership in #stripepayment (private channel).

Robustness notes (learned the hard way):
  - FAIL-CLOSED dedup: if the channel can't be read, abort rather than risk duplicates.
  - Charges with no stable key are skipped (never post something we can't dedup).
  - Idempotent: the channel itself is the dedup state (each post embeds "Ref <pi>").
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CHANNEL = "C0B6FENKU2K"          # #stripepayment
LOOKBACK_HOURS = 72
CHARGE_SCAN = 25
HISTORY_SCAN = 60
DEBUG = os.environ.get("ALERTS_DEBUG", "1") == "1"
DRYRUN = os.environ.get("ALERTS_DRYRUN") == "1"

STRIPE_KEY = os.environ.get("STRIPE_API_KEY", "")
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")


def diag(*a):
    if DEBUG:
        print("[diag]", *a, file=sys.stderr)


def _http(url, headers, data=None, attempts=3):
    """GET/POST JSON with retries on transient failures (5xx, 429, network)."""
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            if e.code in (429, 500, 502, 503, 504) and attempt < attempts:
                diag(f"HTTP {e.code} from {url.split('?')[0]}; retry {attempt}/{attempts}")
                time.sleep(5 * attempt)
                continue
            raise RuntimeError(f"HTTP {e.code} {url.split('?')[0]}: {body}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < attempts:
                diag(f"network error {e}; retry {attempt}/{attempts}")
                time.sleep(5 * attempt)
                continue
            raise


def stripe_get(path, **params):
    url = f"https://api.stripe.com/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _http(url, {"Authorization": f"Bearer {STRIPE_KEY}"})


def slack_call(method, payload):
    body = _http(f"https://slack.com/api/{method}",
                 {"Authorization": f"Bearer {SLACK_TOKEN}",
                  "Content-Type": "application/json; charset=utf-8"},
                 data=json.dumps(payload).encode())
    if not body.get("ok"):
        raise RuntimeError(f"slack {method} failed: {body.get('error')}")
    return body


def posted_keys():
    """Payment-intent / charge refs already announced. FAIL-CLOSED on read failure."""
    body = slack_call("conversations.history", {"channel": CHANNEL, "limit": HISTORY_SCAN})
    msgs = body.get("messages")
    if not isinstance(msgs, list) or len(msgs) == 0:
        raise RuntimeError("dedup read returned no messages — aborting to avoid duplicates")
    keys = set()
    for m in msgs:
        text = m.get("text", "") or ""
        for att in (m.get("attachments") or []):
            text += " " + (att.get("text") or att.get("fallback") or "")
        for tok in text.replace("\n", " ").split():
            t = tok.strip(" _*`<>|.,")
            if t.startswith(("pi_", "py_", "ch_")):
                keys.add(t)
    diag(f"dedup: read {len(msgs)} msgs, found {len(keys)} prior refs")
    return keys


def charge_key(charge):
    """Stable dedup key: prefer payment_intent; else the charge id."""
    return charge.get("payment_intent") or charge.get("id")


def enrich(charge):
    bd = charge.get("billing_details", {}) or {}
    info = {"item": "—", "payer": bd.get("name") or "—", "org": None,
            "email": bd.get("email") or charge.get("receipt_email") or "—",
            "receipt": charge.get("receipt_url"),
            "method": (charge.get("payment_method_details", {}) or {}).get("type") or "—"}
    # Subscription / invoice charges have NO checkout session — their item, email, and
    # payer name live on the INVOICE instead. Without this branch they'd post with
    # blank "Item:" and "Email:" fields.
    inv = charge.get("invoice")
    if inv:
        try:
            iv = stripe_get(f"invoices/{inv}")
            names = [ln.get("description")
                     for ln in ((iv.get("lines") or {}).get("data") or [])
                     if ln.get("description")]
            if names:
                info["item"] = ", ".join(names)
            if iv.get("customer_email"):
                info["email"] = iv["customer_email"]
            cname = iv.get("customer_name")
            if cname:
                if info["payer"] == "—":
                    info["payer"] = cname
                elif cname != info["payer"]:
                    info["org"] = cname
        except Exception as e:
            diag(f"enrich invoice {inv}: {e}")
        return info

    pi = charge.get("payment_intent")
    if not pi:
        return info
    try:
        sessions = (stripe_get("checkout/sessions", payment_intent=pi, limit=1)
                    .get("data") or [])
        if not sessions:
            diag(f"enrich {pi}: no checkout session")
            return info
        s = sessions[0]
        cd = s.get("customer_details", {}) or {}
        if cd.get("individual_name"):
            info["payer"] = cd["individual_name"]
        if cd.get("business_name") and cd["business_name"] != info["payer"]:
            info["org"] = cd["business_name"]
        info["email"] = cd.get("email") or info["email"]
        items = (stripe_get(f"checkout/sessions/{s['id']}/line_items").get("data") or [])
        names = [it.get("description") for it in items if it.get("description")]
        if names:
            info["item"] = ", ".join(names)
    except Exception as e:
        diag(f"enrich warning {pi}: {e}")
    return info


def build_message(charge, info, key):
    amount = (charge.get("amount", 0) or 0) / 100
    cur = (charge.get("currency", "") or "").upper()
    created = charge.get("created", 0)
    method = {"link": "Stripe Link", "card": "Card"}.get(info["method"], str(info["method"]).title())
    frm = info["payer"] + (f"  ·  _{info['org']}_" if info["org"] else "")
    lines = [f":moneybag: *New payment received — ${amount:,.2f} {cur}*", "",
             f"*Item:* {info['item']}", f"*From:* {frm}", f"*Email:* {info['email']}",
             f"*Date:* <!date^{created}^{{date_long_pretty}} at {{time}}|payment received>",
             f"*Method:* {method}"]
    if info["receipt"]:
        lines.append(f"*Receipt:* <{info['receipt']}|View Stripe receipt>")
    lines += ["", f"_Ref {key}_"]
    return "\n".join(lines)


def main():
    missing = [n for n, v in (("STRIPE_API_KEY", STRIPE_KEY),
                              ("SLACK_BOT_TOKEN", SLACK_TOKEN)) if not v]
    if missing:
        sys.exit(f"missing env: {', '.join(missing)}")

    cutoff = time.time() - LOOKBACK_HOURS * 3600
    charges = stripe_get("charges", limit=CHARGE_SCAN).get("data") or []
    succeeded = [c for c in charges
                 if c.get("status") == "succeeded" and c.get("paid")
                 and c.get("created", 0) >= cutoff]
    diag(f"pulled {len(charges)} charges; {len(succeeded)} succeeded in window")

    if DRYRUN:
        # Scope self-check: probe every endpoint the enrichers need, so a
        # mis-scoped restricted key surfaces here instead of as a degraded
        # post ("Item: —") when the next real payment lands.
        for label, probe in [("checkout_sessions", lambda: stripe_get("checkout/sessions", limit=1)),
                             ("invoices", lambda: stripe_get("invoices", limit=1)),
                             ("slack_history", posted_keys)]:
            try:
                probe()
                print(f"scope-check {label}: OK")
            except Exception as e:
                print(f"scope-check {label}: FAILED — {e}")
        for c in succeeded:
            info = enrich(c)
            diag(f"DRYRUN charge: key={charge_key(c)} amount=${(c.get('amount',0) or 0)/100:.2f} "
                 f"payer={info['payer']!r} org={info['org']!r} item={info['item']!r}")
        print(f"DRYRUN: {len(succeeded)} succeeded charge(s); no posting")
        return

    keyed = [(c, charge_key(c)) for c in succeeded]
    usable = [(c, k) for c, k in keyed if k]
    if len(usable) != len(succeeded):
        diag(f"WARNING: {len(succeeded) - len(usable)} charge(s) had no stable key; skipped")

    posted = posted_keys()   # fail-closed
    new = [(c, k) for c, k in usable if k not in posted]
    print(f"succeeded={len(succeeded)} usable={len(usable)} "
          f"already_posted={len(usable) - len(new)} new={len(new)}")

    for charge, key in sorted(new, key=lambda ck: ck[0].get("created", 0)):
        info = enrich(charge)
        slack_call("chat.postMessage", {"channel": CHANNEL,
                                        "text": build_message(charge, info, key),
                                        "unfurl_links": False, "unfurl_media": False})
        print(f"posted ${(charge.get('amount', 0) or 0) / 100:.2f} {info['payer']} / {info['item']}")
    if not new:
        print("nothing new to post.")


if __name__ == "__main__":
    main()
