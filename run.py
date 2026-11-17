#!/usr/bin/env python3
"""Polymarket PAPER-TRADING run: settle -> auto-close -> mark -> scan -> open simulated positions -> report.
Read-only public APIs only. No wallet, no keys, no real orders."""
import argparse, datetime as dt, json, os, sys
from pm import api
from pm.book import best, walk_asks, taker_fee
from pm.ledger import Ledger, now
from pm.signals import (
    compute, eligible, expired, CFG, fee_rate, market_finished,
    is_short_crypto_updown, is_short_crypto_updown_slug, is_crypto_market,
    event_slug, arb_resolves_too_far,
)
from pm.proposals import load_proposal_signals, fmt_proposal_stats, PROPOSAL_SLIPPAGE

HERE = os.path.dirname(os.path.abspath(__file__))
TRADEABLE = {"ARB_BUY_BOTH", "NEAR_RES_FAVOURITE", "MOMENTUM_24H", "PROPOSAL"}
RISK = dict(
    max_pos_pct={"ARB_BUY_BOTH": 0.05, "NEAR_RES_FAVOURITE": 0.05, "MOMENTUM_24H": 0.02, "PROPOSAL": 0.05},
    max_general_exposure_pct=0.40,       # non-15m positions
    max_short_crypto_exposure_pct=0.10,  # 15m/5m crypto Up/Down bucket
    crypto_dir_cap_pct=0.15,             # block new general directional crypto at/above this share
    max_new_per_run=15,
    slippage=0.01,
    min_shares=5,
)


def link(slug):
    return f"https://polymarket.com/event/{slug}"


def is_short_crypto_pos(p):
    return is_short_crypto_updown_slug(p["event_slug"] or "")


def is_proposal_sig(s):
    return s["signal"] == "PROPOSAL" or str(s["signal"]).startswith("PROPOSAL_")


def is_arb_sig(s):
    return s["signal"] == "ARB_BUY_BOTH" or (is_proposal_sig(s) and s.get("is_arb"))


def is_arb_pos(p, group_sizes=None):
    """Arbitrage legs (built-in ARB_BUY_BOTH or a multi-leg proposal arb group) are not directional."""
    if p["signal"] == "ARB_BUY_BOTH" or str(p["group_key"] or "").startswith("arb:"):
        return True
    if group_sizes is not None and str(p["signal"] or "").startswith("PROPOSAL"):
        return group_sizes.get(p["group_key"], 0) > 1
    return False


def bucket_values(L):
    """Return (general_usd, short_crypto_usd, directional_crypto_usd) marked values.

    Directional crypto = non-15m, non-arbitrage crypto positions only. The 15m/5m
    Up/Down bucket is exempt (own 10% cap) and is valued ~0 after its window ends
    until settlement, so including it made the share dip temporarily."""
    general = short = crypto = 0.0
    ops = L.open_positions()
    gs = {}
    for p in ops:
        gs[p["group_key"]] = gs.get(p["group_key"], 0) + 1
    for p in ops:
        v = p["mark_value"] or 0.0
        if is_short_crypto_pos(p):
            short += v
        else:
            general += v
            if (p["category"] or "") == "crypto" and not is_arb_pos(p, gs):
                crypto += v
    return general, short, crypto


def paper_sell_open(L, p, bid, reason, market=None):
    """Paper-sell an open position at bid, charging the taker fee. No real order."""
    if market is None:
        market = api.market_by_id(p["market_id"])
    fee = taker_fee(p["shares"], bid, fee_rate(market))
    pnl = L.paper_sell(p["id"], bid, fee, reason)
    return dict(id=p["id"], reason=reason, bid=bid, fee=fee, pnl=pnl,
                question=p["question"], outcome=p["outcome"], shares=p["shares"],
                slug=p["event_slug"], cost=p["cost_usd"], entry_fee=p["fee_usd"])


def sell_ids(L, ids, reason="MANUAL_CUT", block=True):
    """One-off paper-sell of given position ids at the live best bid."""
    results = []
    for pid in ids:
        p = L.position(pid)
        if p is None:
            print(f"  warn: no position #{pid}")
            continue
        if p["status"] != "open":
            print(f"  warn: #{pid} is {p['status']}, skip")
            continue
        try:
            m = api.market_by_id(p["market_id"])
        except Exception as e:
            print(f"  warn: could not fetch market {p['market_id']} for #{pid}: {e}")
            continue
        try:
            bk = api.books({p["token_id"]})
        except Exception as e:
            print(f"  warn: could not fetch book for #{pid}: {e}")
            continue
        b = bk.get(p["token_id"])
        bid = best(b, "bids")[0] if b else None
        if bid is None:
            print(f"  warn: no bid for #{pid}, skip")
            continue
        rec = paper_sell_open(L, p, bid, reason, market=m)
        if block:
            L.block(market_id=p["market_id"], event_slug=p["event_slug"], reason=reason)
        results.append(rec)
        print(f"PAPER-SELL #{rec['id']} {reason} {rec['shares']:.4f} × {rec['outcome']} @ bid {rec['bid']:.4f} "
              f"fee ${rec['fee']:.5f} proceeds ${rec['shares'] * rec['bid'] - rec['fee']:.4f} "
              f"P&L ${rec['pnl']:+.4f} — {rec['question']}")
    return results


def settle_and_mark(L):
    opened = L.open_positions()
    settled, pnl = 0, 0.0
    sold = []
    still = []
    markets = {}
    for p in opened:
        try:
            m = api.market_by_id(p["market_id"])
            markets[p["id"]] = m
        except Exception as e:
            print(f"  warn: could not fetch market {p['market_id']}: {e}")
            still.append(p); continue
        prices = [float(x) for x in api.parse_list(m.get("outcomePrices"))]
        toks = api.parse_list(m.get("clobTokenIds"))
        resolved = m.get("closed") and prices and all(x in (0.0, 0.5, 1.0) for x in prices)
        if resolved and p["token_id"] in toks:
            sp = prices[toks.index(p["token_id"])]
            pnl += L.settle(p["id"], sp); settled += 1
        else:
            still.append(p)
    if still:
        bk = api.books({p["token_id"] for p in still})
        for p in still:
            b = bk.get(p["token_id"])
            bid = best(b, "bids")[0] if b else None
            m = markets.get(p["id"])
            if bid is not None and bid >= 0.99 and m is not None and market_finished(m):
                sold.append(paper_sell_open(L, p, bid, "AUTO_CLOSE_099", market=m))
            elif bid is None and m is not None and market_finished(m) and p["mark_price"] is not None:
                # Ended but not yet settled; book is gone (CLOB 404). Keep the last good mark
                # so equity and cap checks don't treat it as $0 (fix 2026-11-16 17:07).
                pass
            else:
                L.mark(p["id"], bid)
    return settled, pnl, sold


def equity(L):
    open_val = sum((p["mark_value"] or 0) for p in L.open_positions())
    return L.cash(), open_val, L.cash() + open_val


def open_positions(L, sigs, books):
    cash, open_val, eq = equity(L)
    general_val, short_val, crypto_val = bucket_values(L)
    held = L.held_markets()
    held_ev = {p["event_slug"] for p in L.open_positions()}  # max one position per event (correlation)
    new = []

    def _sort(s):
        # Helper proposals first (user-approved path), then built-in signals by score.
        return (0 if is_proposal_sig(s) else 1, -s["score"])

    tradeable = [s for s in sigs if s["signal"] in TRADEABLE or is_proposal_sig(s)]
    for s in sorted(tradeable, key=_sort):
        if len(new) >= RISK["max_new_per_run"]:
            break
        m = s["market"]
        markets = s.get("markets") or [m]
        cat = (m.get("feeType") or "other").split("_")[0]
        if s["slug"] in held_ev or any(mm.get("id") in held for mm in markets):
            continue
        # Deadline / closed / blocklist on every fill path (every leg of a proposal).
        blocked = False
        for mm in markets:
            if expired(mm) or mm.get("closed") or mm.get("acceptingOrders") is False:
                blocked = True
                break
            if L.is_blocked(mm.get("id"), s["slug"] or event_slug(mm)):
                blocked = True
                break
        if blocked:
            continue
        if is_proposal_sig(s) and L.proposal_is_filled(
            proposal_id=s.get("proposal_id"), proposal_hash=s.get("proposal_hash"),
        ):
            continue
        short = s.get("short_crypto") or is_short_crypto_updown(m)
        arb = is_arb_sig(s)
        # User decision 2026-11-16 15:21: no MOMENTUM_24H entries on 15m/5m crypto Up/Down.
        if short and s["signal"] == "MOMENTUM_24H":
            print(f"reject momentum_15m_disabled: {s.get('question') or s.get('slug')}")
            continue
        # System-wide: no NEW arb / multi-leg basket resolving > ARB_MAX_DAYS out.
        # Existing open arbs (e.g. Fed dissent Dec, group arb:770454) are not closed.
        if arb and arb_resolves_too_far(markets):
            print(f"reject arb_resolves_gt_30d: {s.get('question') or s.get('slug')}")
            continue
        # No new general-bucket directional crypto if (directional crypto value + this order's
        # cost) would reach 15% of equity. Directional crypto excludes 15m and arb positions.
        # 15m bucket is separate and is not blocked; ARB / proposal-arb is not directional.
        cryptoish = is_crypto_market(m) or s.get("crypto")
        dir_crypto = (not arb and not short and cryptoish)
        if dir_crypto and crypto_val >= eq * RISK["crypto_dir_cap_pct"]:
            continue
        if short:
            headroom = eq * RISK["max_short_crypto_exposure_pct"] - short_val
        else:
            headroom = eq * RISK["max_general_exposure_pct"] - general_val
        pct = RISK["max_pos_pct"].get("PROPOSAL" if is_proposal_sig(s) else s["signal"], 0.05)
        budget = min(eq * pct, cash - 1, headroom)
        if is_proposal_sig(s) and s.get("size_usd"):
            budget = min(budget, s["size_usd"])
        if budget < 5:
            continue
        fills = []
        if arb:
            legs = s["quotes"]
            if is_proposal_sig(s):
                if any(x["ask"] is None or x["ask"] > x.get("limit_price", x["ask"]) + PROPOSAL_SLIPPAGE
                       for x in legs):
                    continue
                fees = sum(taker_fee(1, x["ask"], x.get("fee_rate", s["fee_rate"])) for x in legs)
                if 1 - sum(x["ask"] for x in legs) - fees < CFG["arb_min_edge"]:
                    continue
            # equal share count on every leg so payout is locked at $1/set
            pair_cost = sum(x["ask"] for x in legs)
            n = min(budget / pair_cost, *(x["ask_sz"] for x in legs))
            if is_proposal_sig(s) and s.get("size_shares"):
                n = min(n, s["size_shares"])
            if len(new) + len(legs) > RISK["max_new_per_run"]:
                continue
            for x in legs:
                fills.append((x, n, x["ask"], n * x["ask"]))
        else:
            x = s["side"]
            book = books.get(x["token"])
            if not book:
                continue
            max_px = x["ask"] + RISK["slippage"]
            if is_proposal_sig(s):
                max_px = min(max_px, x.get("limit_price", x["ask"]) + PROPOSAL_SLIPPAGE)
                if s.get("size_shares"):
                    budget = min(budget, s["size_shares"] * max_px)
            cap = s.get("size_shares") if is_proposal_sig(s) else None
            sh, avg, spent = walk_asks(book, budget, max_px, max_shares=cap)
            fills.append((x, sh, avg, spent))
        if any(f[1] < RISK["min_shares"] for f in fills):
            continue
        if dir_crypto:
            order_cost = sum(f[3] + taker_fee(f[1], f[2], f[0].get("fee_rate", s["fee_rate"])) for f in fills)
            if crypto_val + order_cost >= eq * RISK["crypto_dir_cap_pct"]:
                print(f"reject crypto_dir_cap: dir crypto ${crypto_val:.2f} + order ${order_cost:.2f} "
                      f">= {RISK['crypto_dir_cap_pct']*100:.0f}% of ${eq:.2f} — {s.get('question') or s.get('slug')}")
                continue
        if is_proposal_sig(s):
            gk = f"prop:{s.get('proposal_id') or m['id']}:{now()}"
        else:
            gk = f"{m['id']}:{now()}"
        opened_ids = []
        for x, sh, avg, spent in fills:
            xm = x.get("market") or m
            xcat = (xm.get("feeType") or "other").split("_")[0]
            rate = x.get("fee_rate", s["fee_rate"])
            qn = x.get("question") or s["question"]
            mid = x.get("market_id") or xm.get("id") or m["id"]
            cid = x.get("condition_id") or xm.get("conditionId") or m.get("conditionId")
            fee = taker_fee(sh, avg, rate)
            pid = L.open(market_id=mid, condition_id=cid, event_slug=s["slug"],
                         question=qn, outcome=x["outcome"], token_id=x["token"], shares=round(sh, 4),
                         avg_price=round(avg, 4), fee_usd=fee, cost_usd=round(spent, 4), signal=s["signal"],
                         group_key=gk, category=xcat, mark_price=x["bid"], mark_value=sh * (x["bid"] or 0),
                         marked_at=now())
            new.append(dict(id=pid, signal=s["signal"], question=qn, slug=s["slug"],
                            outcome=x["outcome"], shares=sh, avg=avg, cost=spent, fee=fee, short=short,
                            source=s.get("source_file")))
            opened_ids.append(pid)
            cash -= spent + fee
            mark = sh * (x["bid"] or 0)
            open_val += mark
            if short:
                short_val += mark
            else:
                general_val += mark
                if not arb and (xcat == "crypto" or is_crypto_market(xm)):
                    crypto_val += mark
            held.add(mid)
        held_ev.add(s["slug"])
        if is_proposal_sig(s):
            L.record_proposal(
                proposal_id=s.get("proposal_id"), proposal_hash=s.get("proposal_hash"),
                source_file=s.get("source_file"), event_slug=s.get("slug"),
                market_id=m.get("id"), status="filled",
                reason="paper fill", group_key=gk,
            )
    return new


def fmt_q(q):
    return " | ".join(f"{x['outcome']}: bid {x['bid']:.3f} ask {x['ask']:.3f}" for x in q)


def fmt_sold(n):
    return (f"- #{n['id']} `{n['reason']}` SELL {n['shares']:.2f} × **{n['outcome']}** @ bid {n['bid']:.3f} "
            f"fee ${n['fee']:.5f} P&L ${n['pnl']:+.2f} — {n['question']} ({link(n['slug'])})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "ledger.sqlite"))
    ap.add_argument("--bankroll-thb", type=float, default=10_000)
    ap.add_argument("--no-trade", action="store_true", help="scan + mark + auto-close only, no new opens")
    ap.add_argument("--sell", help="comma-separated open position ids to paper-sell at the best bid")
    ap.add_argument("--sell-reason", default="MANUAL_CUT")
    a = ap.parse_args()

    L = Ledger(a.db)
    rate, src, asof = api.usd_thb()
    if L.init_bankroll(a.bankroll_thb, rate, src, asof):
        print(f"Initialised paper bankroll {a.bankroll_thb:,.0f} THB = ${float(L.meta('start_usd')):,.2f} "
              f"at {rate:.4f} THB/USD ({src}, {asof})")

    if a.sell:
        ids = [int(x.strip()) for x in a.sell.split(",") if x.strip()]
        sold = sell_ids(L, ids, reason=a.sell_reason, block=True)
        cash, open_val, eq = equity(L)
        general_val, short_val, crypto_val = bucket_values(L)
        print(f"Sold {len(sold)} position(s). Cash ${cash:,.2f}  open ${open_val:,.2f}  equity ${eq:,.2f}  "
              f"general ${general_val:,.2f} ({(general_val/eq*100 if eq else 0):.1f}% / 40%)  "
              f"15m ${short_val:,.2f} ({(short_val/eq*100 if eq else 0):.1f}% / 10%)  "
              f"dir crypto (non-15m) ${crypto_val:,.2f} ({(crypto_val/eq*100 if eq else 0):.1f}% / 15%)")
        return 0

    n_settled, settled_pnl, sold = settle_and_mark(L)

    mk = [m for m in api.merge_markets(api.active_markets(), api.crypto_15m_markets()) if eligible(m)]
    toks = {t for m in mk for t in api.parse_list(m["clobTokenIds"])}
    bk = api.books(toks)
    sigs = compute(mk, bk)
    prop_sigs, prop_stats, prop_books = load_proposal_signals(L)
    if prop_books:
        bk.update(prop_books)
    sigs = list(sigs) + list(prop_sigs)
    new = [] if a.no_trade else open_positions(L, sigs, bk)
    cash, open_val, eq = equity(L)
    general_val, short_val, crypto_val = bucket_values(L)
    L.log_run(ts=now(), usd_thb=rate, cash_usd=cash, open_value_usd=open_val, equity_usd=eq,
              equity_thb=eq * rate, n_open=len(L.open_positions()), n_new=len(new), n_settled=n_settled)

    # ---- report ----
    ts = dt.datetime.now().astimezone()
    tradeable = sorted([s for s in sigs if s["signal"] in TRADEABLE], key=lambda s: -s["score"])
    info = sorted([s for s in sigs if s["signal"] not in TRADEABLE], key=lambda s: -s["score"])
    top, seen = [], set()
    for s in tradeable + info:
        if s["market"]["id"] in seen: continue
        seen.add(s["market"]["id"]); top.append(s)
        if len(top) >= 10: break
    start_usd = float(L.meta("start_usd"))
    sold_pnl = sum(x["pnl"] for x in sold)
    lines = [f"# Polymarket paper-trading snapshot — {ts:%Y-%m-%d %H:%M} (Asia/Bangkok, UTC+7)", "",
             "**PAPER TRADING ONLY — simulated fills, no real money or orders.**", "",
             f"- FX: 1 USD = {rate:.4f} THB ({src}, as of {asof}); start bankroll {L.meta('start_thb')} THB = "
             f"${start_usd:,.2f} at {float(L.meta('start_rate')):.4f}",
             f"- Markets scanned (active, not ended, accepting orders, liq ≥ ${CFG['min_liquidity']:,}, 24h vol ≥ ${CFG['min_volume24h']:,}, binary; 15m crypto Up/Down: liq ≥ ${CFG['min_liquidity_15m']:,}, no 24h-vol floor): {len(mk)}",
             f"- Signals: " + ", ".join(f"{k}={sum(1 for s in sigs if s['signal']==k)}" for k in
                                        ["ARB_BUY_BOTH", "NEAR_RES_FAVOURITE", "MOMENTUM_24H", "PROPOSAL",
                                         "INFO_WIDE_SPREAD", "INFO_BID_SUM_GT_1"]),
             f"- Settled this run: {n_settled} (P&L ${settled_pnl:,.2f})",
             f"- Paper-sold this run: {len(sold)} (P&L ${sold_pnl:,.2f})", "",
             "## Top candidates", ""]
    for i, s in enumerate(top, 1):
        hl = s["hours_left"]
        lines += [f"{i}. **{s['question']}** — {link(s['slug'])}",
                  f"   - Prices: {fmt_q(s['quotes'])}",
                  f"   - Signal: `{s['signal']}` — {s['detail']}",
                  f"   - Ends in: {hl:.1f} h · fee rate {s['fee_rate']} · liq ${float(s['market'].get('liquidityNum') or 0):,.0f} · 24h vol ${float(s['market'].get('volume24hr') or 0):,.0f}"
                  if hl is not None else ""]
    lines += ["", "## Helper proposals", ""] + fmt_proposal_stats(prop_stats)
    lines += ["", "## Simulated positions opened this run", ""]
    if not new:
        lines.append("None.")
    for n in new:
        lines.append(f"- #{n['id']} `{n['signal']}` BUY {n['shares']:.2f} × **{n['outcome']}** @ {n['avg']:.3f} "
                     f"= ${n['cost']:.2f} + fee ${n['fee']:.3f} — {n['question']} ({link(n['slug'])})")
    lines += ["", "## Paper-sold this run", ""]
    if not sold:
        lines.append("None.")
    for n in sold:
        lines.append(fmt_sold(n))
    def pct(part):
        return (part / eq * 100) if eq else 0.0
    lines += ["", "## Bankroll", "",
              f"- Cash: ${cash:,.2f} ({cash * rate:,.0f} THB)",
              f"- Open positions marked at best bid: ${open_val:,.2f} ({open_val * rate:,.0f} THB) — {len(L.open_positions())} open",
              f"- Exposure: general ${general_val:,.2f} ({pct(general_val):.1f}% of equity; cap 40%) · "
              f"15m crypto ${short_val:,.2f} ({pct(short_val):.1f}%; cap 10%) · "
              f"directional crypto (non-15m, non-arb) ${crypto_val:,.2f} ({pct(crypto_val):.1f}%; new directional crypto blocked if it + order ≥15%)",
              f"- Equity: ${eq:,.2f} ({eq * rate:,.0f} THB at today's rate) vs start ${start_usd:,.2f} "
              f"→ {((eq / start_usd) - 1) * 100:+.2f}%"]
    rep = "\n".join(l for l in lines if l is not None)
    os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
    path = os.path.join(HERE, "reports", f"snapshot_{ts:%Y%m%d_%H%M}.md")
    open(path, "w").write(rep + "\n")
    open(os.path.join(HERE, "reports", "latest.md"), "w").write(rep + "\n")
    print(rep); print(f"\nReport saved: {path}")


if __name__ == "__main__":
    sys.exit(main())
