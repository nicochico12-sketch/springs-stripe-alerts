#!/usr/bin/env python3
"""
Stripe -> Slack enriched payment alerts for Springs Pickleball.

Each run:
  1. Pull recent SUCCEEDED Stripe charges (within LOOKBACK_HOURS).
  2. For each, enrich: charge -> payment_intent -> checkout session -> line item (tier),
     plus customer name + organization + receipt URL.
  3. Dedup against the #stripepayment channel (skips any charge ID already posted).
  4. Post a rich alert for anything new.

Idempotent: safe to run on a schedule. The channel itself is the dedup state
(every post embeds "Charge <id>"), so no separate state file is needed.

Requires: the `composio` CLI, authenticated (`composio whoami`), with Stripe + Slack connected.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

COMPOSIO_BIN = os.environ.get("COMPOSIO_BIN", "composio")  # full path under launchd
CHANNEL = "C0B6FENKU2K"          # #stripepayment
LOOKBACK_HOURS = 72               # only consider charges newer than this (first-run backflood guard)
CHARGE_SCAN = 25                  # how many recent charges to inspect
HISTORY_SCAN = 60                 # how many recent channel messages to scan for dedup


def run_composio(slug, payload):
    """Execute a composio tool and return the most useful dict (inline or offloaded file)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    proc = subprocess.run(
        [COMPOSIO_BIN, "execute", slug, "-d", f"@{path}"],
        capture_output=True, text=True,
    )
    out = proc.stdout
    start = out.find("{")
    if start == -1:
        raise RuntimeError(f"{slug}: no JSON in output:\n{out}\n{proc.stderr}")
    top = json.loads(out[start:])
    if not top.get("successful", True):
        # surface Slack/Stripe API errors but don't crash the whole run on one bad charge
        err = top.get("error") or (top.get("data") or {})
        raise RuntimeError(f"{slug} failed: {err}")
    # large responses are offloaded to a file
    fp = top.get("outputFilePath")
    data = top.get("data")
    if fp and (not data or (isinstance(data, dict) and not data)):
        with open(fp) as fh:
            return json.load(fh)
    return data if isinstance(data, dict) else top


def find_list(obj, key):
    """Recursively find the first list stored under `key`."""
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], list):
            return obj[key]
        for v in obj.values():
            r = find_list(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_list(v, key)
            if r is not None:
                return r
    return None


def already_posted_ids():
    """Charge IDs already announced in the channel (dedup state)."""
    data = run_composio("SLACK_FETCH_CONVERSATION_HISTORY",
                        {"channel": CHANNEL, "limit": HISTORY_SCAN})
    msgs = find_list(data, "messages") or []
    ids = set()
    for m in msgs:
        text = m.get("text", "") or ""
        for att in (m.get("attachments") or []):
            text += " " + (att.get("text") or att.get("fallback") or "")
        for token in text.replace("\n", " ").split():
            t = token.strip(" _*`<>|.,")
            if t.startswith(("py_", "ch_")):
                ids.add(t)
    return ids


def enrich(charge):
    """Return dict with item, payer, org, email, receipt for a charge."""
    bd = charge.get("billing_details", {}) or {}
    info = {
        "item": charge.get("description") or "—",
        "payer": bd.get("name") or "—",
        "org": None,
        "email": bd.get("email") or charge.get("receipt_email") or "—",
        "receipt": charge.get("receipt_url"),
        "method": (charge.get("payment_method_details", {}) or {}).get("type") or "—",
    }
    pi = charge.get("payment_intent")
    if not pi:
        return info
    try:
        sess_data = run_composio("STRIPE_LIST_CHECKOUT_SESSIONS",
                                {"payment_intent": pi, "limit": 1})
        sessions = find_list(sess_data, "data") or []
        if not sessions:
            return info
        session = sessions[0]
        cd = session.get("customer_details", {}) or {}
        if cd.get("individual_name"):
            info["payer"] = cd["individual_name"]
        if cd.get("business_name") and cd["business_name"] != info["payer"]:
            info["org"] = cd["business_name"]
        info["email"] = cd.get("email") or info["email"]
        li_data = run_composio("STRIPE_GET_CHECKOUT_SESSIONS_SESSION_LINE_ITEMS",
                              {"session": session["id"]})
        items = find_list(li_data, "data") or []
        names = [it.get("description") for it in items if it.get("description")]
        if names:
            info["item"] = ", ".join(names)
    except Exception as e:
        print(f"  enrich warning for {charge.get('id')}: {e}", file=sys.stderr)
    return info


def build_message(charge, info):
    amount = (charge.get("amount", 0) or 0) / 100
    cur = (charge.get("currency", "") or "").upper()
    created = charge.get("created", 0)
    method = {"link": "Stripe Link", "card": "Card"}.get(info["method"], info["method"].title())
    frm = info["payer"]
    if info["org"]:
        frm += f"  ·  _{info['org']}_"
    lines = [
        f":moneybag: *New payment received — ${amount:,.2f} {cur}*",
        "",
        f"*Item:* {info['item']}",
        f"*From:* {frm}",
        f"*Email:* {info['email']}",
        f"*Date:* <!date^{created}^{{date_long_pretty}} at {{time}}|payment received>",
        f"*Method:* {method}",
    ]
    if info["receipt"]:
        lines.append(f"*Receipt:* <{info['receipt']}|View Stripe receipt>")
    lines.append("")
    lines.append(f"_Charge {charge.get('id')}_")
    return "\n".join(lines)


def main():
    cutoff = time.time() - LOOKBACK_HOURS * 3600
    charges_data = run_composio("STRIPE_LIST_CHARGES", {"limit": CHARGE_SCAN})
    charges = find_list(charges_data, "data") or []
    succeeded = [c for c in charges
                 if c.get("status") == "succeeded" and c.get("paid")
                 and (c.get("created", 0) >= cutoff)]
    posted = already_posted_ids()
    new = [c for c in succeeded if c.get("id") not in posted]
    print(f"succeeded in window: {len(succeeded)} | already posted: "
          f"{len([c for c in succeeded if c.get('id') in posted])} | new: {len(new)}")

    new.sort(key=lambda c: c.get("created", 0))  # oldest first
    for charge in new:
        info = enrich(charge)
        msg = build_message(charge, info)
        run_composio("SLACK_SEND_MESSAGE",
                    {"channel": CHANNEL, "markdown_text": msg,
                     "unfurl_links": False, "unfurl_media": False})
        print(f"posted: {charge.get('id')} ${ (charge.get('amount',0) or 0)/100 } "
              f"{info['payer']} / {info['item']}")

    if not new:
        print("nothing new to post.")


if __name__ == "__main__":
    main()
