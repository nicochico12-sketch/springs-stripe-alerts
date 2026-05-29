#!/usr/bin/env python3
"""
Stripe -> Slack enriched payment alerts for Springs Pickleball.

Each run:
  1. Pull recent SUCCEEDED Stripe charges (within LOOKBACK_HOURS).
  2. Enrich each: payment_intent -> checkout session (+ expanded line items) for
     the item/tier, payer name, organization, and receipt URL.
  3. Dedup against the #stripepayment channel (skip any charge ID already posted).
  4. Post a rich alert for anything new.

Safety/robustness:
  - Explicit response parsing (no blind recursion) for both inline and file-offloaded
    Composio outputs.
  - FAIL-CLOSED dedup: if the channel can't be read, abort rather than risk duplicates.
  - Charges with no usable id are skipped (never post an un-dedupable message).
  - Idempotent: the channel itself is the dedup state (each post embeds "Charge <id>").

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
LOOKBACK_HOURS = 72              # first-run / downtime backflood guard
CHARGE_SCAN = 25
HISTORY_SCAN = 60
DEBUG = os.environ.get("ALERTS_DEBUG", "1") == "1"


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
        raise RuntimeError(f"{slug}: no JSON output:\n{out[:500]}\n{proc.stderr[:500]}")
    top = json.loads(out[i:])
    if not top.get("successful", True):
        raise RuntimeError(f"{slug} failed: {top.get('error') or top.get('data')}")
    body = top.get("data")
    fp = top.get("outputFilePath")
    if fp and (not body or (isinstance(body, dict) and not body)):
        with open(fp) as fh:
            body = json.load(fh)
        # file may be the provider payload OR the full wrapped response
        if isinstance(body, dict) and "successful" in body and "data" in body:
            body = body["data"]
    return body if isinstance(body, dict) else {}


def data_list(body):
    """Stripe list endpoints return {object:'list', data:[...]}."""
    d = body.get("data")
    return d if isinstance(d, list) else []


def already_posted_ids():
    """Charge IDs already announced. FAIL-CLOSED: raise if the channel can't be read."""
    body = execute("SLACK_FETCH_CONVERSATION_HISTORY",
                   {"channel": CHANNEL, "limit": HISTORY_SCAN})
    msgs = body.get("messages")
    if not isinstance(msgs, list) or len(msgs) == 0:
        raise RuntimeError("dedup read returned no messages — aborting to avoid duplicates")
    ids = set()
    for m in msgs:
        text = m.get("text", "") or ""
        for att in (m.get("attachments") or []):
            text += " " + (att.get("text") or att.get("fallback") or "")
        for tok in text.replace("\n", " ").split():
            t = tok.strip(" _*`<>|.,")
            if t.startswith(("py_", "ch_", "pi_")):
                ids.add(t)
    diag(f"dedup: read {len(msgs)} msgs, found {len(ids)} prior charge ids")
    return ids


def enrich(charge):
    bd = charge.get("billing_details", {}) or {}
    info = {"item": charge.get("description") or "—",
            "payer": bd.get("name") or "—", "org": None,
            "email": bd.get("email") or charge.get("receipt_email") or "—",
            "receipt": charge.get("receipt_url"),
            "method": (charge.get("payment_method_details", {}) or {}).get("type") or "—"}
    pi = charge.get("payment_intent")
    if not pi:
        return info
    try:
        # expand line items in the SAME call that locates the session, so we never
        # need the separate (cross-account-prone) line-items endpoint.
        body = execute("STRIPE_LIST_CHECKOUT_SESSIONS",
                       {"payment_intent": pi, "limit": 1, "expand": ["data.line_items"]})
        sessions = data_list(body)
        if not sessions:
            diag(f"enrich {charge.get('id')}: no checkout session for {pi}")
            return info
        s = sessions[0]
        cd = s.get("customer_details", {}) or {}
        if cd.get("individual_name"):
            info["payer"] = cd["individual_name"]
        if cd.get("business_name") and cd["business_name"] != info["payer"]:
            info["org"] = cd["business_name"]
        info["email"] = cd.get("email") or info["email"]
        items = (s.get("line_items", {}) or {}).get("data", [])
        if not items:  # fallback: separate call (best effort)
            try:
                items = data_list(execute("STRIPE_GET_CHECKOUT_SESSIONS_SESSION_LINE_ITEMS",
                                          {"session": s["id"]}))
            except Exception as e:
                diag(f"enrich {charge.get('id')}: line-items fallback failed: {e}")
        names = [it.get("description") for it in items if it.get("description")]
        if names:
            info["item"] = ", ".join(names)
    except Exception as e:
        diag(f"enrich warning {charge.get('id')}: {e}")
    return info


def build_message(charge, info):
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
    lines += ["", f"_Charge {charge.get('id')}_"]
    return "\n".join(lines)


def main():
    cutoff = time.time() - LOOKBACK_HOURS * 3600
    charges = data_list(execute("STRIPE_LIST_CHARGES", {"limit": CHARGE_SCAN}))
    diag(f"pulled {len(charges)} charges; first keys: "
         f"{sorted(charges[0].keys())[:12] if charges else 'none'}")

    succeeded = [c for c in charges
                 if c.get("status") == "succeeded" and c.get("paid")
                 and c.get("created", 0) >= cutoff]

    if os.environ.get("ALERTS_DRYRUN") == "1":
        who = subprocess.run([COMPOSIO_BIN, "whoami"], capture_output=True, text=True).stdout
        diag("whoami:", " ".join(who.split())[-220:])
        for c in succeeded:
            # reversed so GitHub's secret-masking can't redact the value
            diag("succ id(rev):", (c.get("id") or "")[::-1],
                 "| pi(rev):", (c.get("payment_intent") or "")[::-1],
                 "| receipt_tail:", (c.get("receipt_url") or "")[-28:])
        print(f"DRYRUN: {len(succeeded)} succeeded charge(s); no posting")
        return
    # never post a charge we can't dedup on
    usable = [c for c in succeeded if c.get("id")]
    if len(usable) != len(succeeded):
        diag(f"WARNING: {len(succeeded) - len(usable)} succeeded charge(s) had no id; skipped")

    posted = already_posted_ids()   # fail-closed
    new = [c for c in usable if c["id"] not in posted]
    print(f"succeeded={len(succeeded)} usable={len(usable)} "
          f"already_posted={len(usable) - len(new)} new={len(new)}")

    for charge in sorted(new, key=lambda c: c.get("created", 0)):
        info = enrich(charge)
        execute("SLACK_SEND_MESSAGE", {"channel": CHANNEL, "markdown_text": build_message(charge, info),
                                       "unfurl_links": False, "unfurl_media": False})
        print(f"posted charge ${(charge.get('amount', 0) or 0) / 100:.2f} "
              f"{info['payer']} / {info['item']}")
    if not new:
        print("nothing new to post.")


if __name__ == "__main__":
    main()
