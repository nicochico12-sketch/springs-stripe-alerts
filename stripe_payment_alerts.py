#!/usr/bin/env python3
"""
Stripe -> Slack enriched payment alerts for Springs Pickleball.

Posts enriched alerts (amount, item/tier, payer, organization, email, receipt) to
the #stripepayment Slack channel for each new SUCCEEDED Stripe payment.

Robustness notes (learned the hard way):
  - Composio redacts the `id` field of `charge` and `checkout.session` objects in this
    account, but NOT `payment_intent`, `receipt_url`, or `payment_link`. So:
      * dedup keys on the PAYMENT INTENT (pi_...), which is always present & stable.
      * the item/tier is fetched via the PAYMENT LINK's line items (not the session id).
  - FAIL-CLOSED dedup: if the channel can't be read, abort rather than risk duplicates.
  - Charges with no stable key are skipped (never post something we can't dedup).
  - Idempotent: the channel itself is the dedup state (each post embeds "Ref <pi>").

Requires: the `composio` CLI authenticated with Stripe + Slack connected.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

COMPOSIO_BIN = os.environ.get("COMPOSIO_BIN", "composio")
CHANNEL = "C0B6FENKU2K"          # #stripepayment
LOOKBACK_HOURS = 72
CHARGE_SCAN = 25
HISTORY_SCAN = 60
DEBUG = os.environ.get("ALERTS_DEBUG", "1") == "1"
DRYRUN = os.environ.get("ALERTS_DRYRUN") == "1"
REDACTED = "<REDACTED>"


def diag(*a):
    if DEBUG:
        print("[diag]", *a, file=sys.stderr)


def execute(slug, payload):
    """Run a Composio tool; return the provider payload dict (handles file offload)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    proc = subprocess.run([COMPOSIO_BIN, "execute", slug, "-d", f"@{path}"],
                          capture_output=True, text=True)
    out = proc.stdout
    i = out.find("{")
    if i == -1:
        raise RuntimeError(f"{slug}: no JSON output:\n{out[:400]}\n{proc.stderr[:400]}")
    top = json.loads(out[i:])
    if not top.get("successful", True):
        raise RuntimeError(f"{slug} failed: {top.get('error') or top.get('data')}")
    body = top.get("data")
    fp = top.get("outputFilePath")
    if fp and (not body or (isinstance(body, dict) and not body)):
        with open(fp) as fh:
            body = json.load(fh)
        if isinstance(body, dict) and "successful" in body and "data" in body:
            body = body["data"]
    return body if isinstance(body, dict) else {}


def data_list(body):
    d = body.get("data")
    return d if isinstance(d, list) else []


def posted_keys():
    """Payment-intent / charge refs already announced. FAIL-CLOSED on read failure."""
    body = execute("SLACK_FETCH_CONVERSATION_HISTORY", {"channel": CHANNEL, "limit": HISTORY_SCAN})
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
    """Stable dedup key: prefer payment_intent (never redacted); else a real charge id."""
    pi = charge.get("payment_intent")
    if pi and pi != REDACTED:
        return pi
    cid = charge.get("id")
    return cid if cid and cid != REDACTED else None


def enrich(charge):
    bd = charge.get("billing_details", {}) or {}
    info = {"item": "—", "payer": bd.get("name") or "—", "org": None,
            "email": bd.get("email") or charge.get("receipt_email") or "—",
            "receipt": charge.get("receipt_url"),
            "method": (charge.get("payment_method_details", {}) or {}).get("type") or "—"}
    # Subscription / invoice charges have NO checkout session — their item, email, and
    # payer name live on the INVOICE instead (the invoice id survives redaction). Without
    # this branch they'd post with blank "Item:" and "Email:" fields.
    inv = charge.get("invoice")
    if inv and inv != REDACTED:
        try:
            iv = execute("STRIPE_GET_INVOICES_INVOICE", {"invoice": inv})
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
    if not pi or pi == REDACTED:
        return info
    try:
        sessions = data_list(execute("STRIPE_LIST_CHECKOUT_SESSIONS",
                                     {"payment_intent": pi, "limit": 1}))
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
        # Item/tier via the PAYMENT LINK (its id survives redaction; the session id does not).
        plink = s.get("payment_link")
        names = []
        if plink and plink != REDACTED:
            items = data_list(execute("STRIPE_GET_PAYMENT_LINKS_PAYMENT_LINK_LINE_ITEMS",
                                      {"payment_link": plink}))
            names = [it.get("description") for it in items if it.get("description")]
        if not names:  # fallback: line items by session id (works when id isn't redacted)
            sid = s.get("id")
            if sid and sid != REDACTED:
                try:
                    items = data_list(execute("STRIPE_GET_CHECKOUT_SESSIONS_SESSION_LINE_ITEMS",
                                              {"session": sid}))
                    names = [it.get("description") for it in items if it.get("description")]
                except Exception as e:
                    diag(f"enrich {pi}: session line-items fallback failed: {e}")
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
    if info["receipt"] and info["receipt"] != REDACTED:
        lines.append(f"*Receipt:* <{info['receipt']}|View Stripe receipt>")
    lines += ["", f"_Ref {key}_"]
    return "\n".join(lines)


def main():
    cutoff = time.time() - LOOKBACK_HOURS * 3600
    charges = data_list(execute("STRIPE_LIST_CHARGES", {"limit": CHARGE_SCAN}))
    succeeded = [c for c in charges
                 if c.get("status") == "succeeded" and c.get("paid")
                 and c.get("created", 0) >= cutoff]
    diag(f"pulled {len(charges)} charges; {len(succeeded)} succeeded in window")

    if DRYRUN:
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
        execute("SLACK_SEND_MESSAGE", {"channel": CHANNEL, "markdown_text": build_message(charge, info, key),
                                       "unfurl_links": False, "unfurl_media": False})
        print(f"posted ${(charge.get('amount', 0) or 0) / 100:.2f} {info['payer']} / {info['item']}")
    if not new:
        print("nothing new to post.")


if __name__ == "__main__":
    main()
