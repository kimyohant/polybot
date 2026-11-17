#!/usr/bin/env python3
"""PM-Crypto scanner: model-based fair value for Polymarket crypto price markets.
PAPER TRADING ONLY. Public read-only endpoints only (Gamma, CLOB /books, Binance public
market data via data-api.binance.vision). Never writes ledger.sqlite, never edits pm/*.

Model (transparent, deliberately simple):
  * Spot S = Binance BTC/ETH/SOL/XRP-USDT last price (the resolution source for daily/hourly
    markets; 15m windows resolve on Chainlink TWAP, so Binance is a proxy there).
  * Driftless lognormal, sigma from realized Binance returns (1m bars for <=6h horizons,
    1h bars over 7d otherwise; central sigma = mean of the two, never below either * 0.8).
  * Terminal digital:  P(S_T > K)  = N( ln(S/K) / (sigma*sqrt(T)) - sigma*sqrt(T)/2 )
    Bucket [lo,hi):    P = P(>lo) - P(>hi)
    Touch (barrier):   P(hit K)     = 2 * (1 - N( |ln(K/S)| / (sigma*sqrt(T)) ))   (reflection, zero drift)
  * Robust edge: fair p is recomputed for sigma x {0.75, 1, 1.35} and spot x (1 +/- basis);
    robust_edge = min over scenarios of (fair - ask - fee). fee/share = 0.07 * p * (1-p).
  * Keep only: robust_edge >= MIN_ROBUST, nominal net edge >= MIN_NET, 0.05 <= ask <= 0.95,
    enough ask depth within +1c, >= MIN_MINUTES to expiry. One proposal per event.
  * Size: 0.10 Kelly on worst-case fair vs cost (limit+0.5c + fee), capped at 2.5% of current
    paper equity (latest runs.equity_usd, read-only). Proposals for events already held are dropped.
  * Output follows the README "Helper proposals" schema; written atomically (temp + rename).
Usage: python3 proposals/crypto_scan.py [--out proposals/crypto.json] [--dry]
"""
import sys, os, re, json, math, time, argparse, datetime as dt
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from pm import api            # read-only use: crypto_15m_markets(), books(), parse_list()
from pm.book import best, levels

BKK = dt.timezone(dt.timedelta(hours=7))
BIN = "https://data-api.binance.vision/api/v3"
FEE_RATE = 0.07
BANKROLL_THB = 10_000
CFG = dict(min_robust=0.02, min_net=0.04, min_net_touch=0.06, ask_lo=0.05, ask_hi=0.95,
           min_minutes=4, kelly_frac=0.10, max_pct=0.025, min_shares=5, min_liq=1000,
           sig_band=(0.75, 1.0, 1.35))
SLIP = 0.005          # loop fills a proposal only if live ask <= limit + 0.5c
# Per-proposal freshness (minutes): 15m/hourly 2, daily 30 (loader treats what-price-hit as daily), else 60.
TTL_MIN = {"updown_15m": 2, "updown_hourly": 2, "above_hourly": 2, "above_daily": 30,
           "updown_daily": 30, "bucket": 30, "price-on-date": 30, "touch": 30}
ASSETS = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH",
          "solana": "SOL", "sol": "SOL", "xrp": "XRP"}
S = api.S


def N(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def get(url, params=None):
    r = S.get(url, params=params, timeout=20, headers={"Accept-Encoding": "gzip"}); r.raise_for_status(); return r.json()


def klines(sym, interval, limit, start=None):
    p = {"symbol": sym + "USDT", "interval": interval, "limit": limit}
    if start is not None: p["startTime"] = int(start * 1000)
    return get(f"{BIN}/klines", p)


def realized_sigma(closes, bar_sec):
    r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(r) < 10: return None
    m = sum(r) / len(r)
    var = sum((x - m) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(var / bar_sec)          # per sqrt(second)


VOL, SPOT = {}, {}
def asset_data(a):
    if a not in VOL:
        SPOT[a] = float(get(f"{BIN}/ticker/price", {"symbol": a + "USDT"})["price"])
        k1 = klines(a, "1m", 360)              # last 6h
        kh = klines(a, "1h", 168)              # last 7d
        s_short = realized_sigma([float(k[4]) for k in k1], 60)
        s_long = realized_sigma([float(k[4]) for k in kh], 3600)
        VOL[a] = (s_short, s_long)
    return SPOT[a], VOL[a]


def sigma_for(a, T):
    s_short, s_long = asset_data(a)[1]
    base = s_short if T <= 6 * 3600 else s_long
    other = s_long if T <= 6 * 3600 else s_short
    return max((base + other) / 2, 0.8 * max(base, other))


def p_above(S0, K, sig, T):
    if T <= 0: return 1.0 if S0 > K else 0.0
    v = sig * math.sqrt(T)
    return N(math.log(S0 / K) / v - v / 2)


def p_touch(S0, K, sig, T):
    if (K >= S0 and S0 >= K) : return 1.0
    v = sig * math.sqrt(T)
    return min(1.0, 2 * (1 - N(abs(math.log(K / S0)) / v)))


def num(s): return float(s.replace(",", "").replace("$", ""))


def fee(p): return FEE_RATE * p * (1 - p)


# ---------- market discovery ----------
def crypto_events():
    out = []
    for off in range(0, 400, 50):
        b = get("https://gamma-api.polymarket.com/events",
                {"tag_slug": "crypto", "active": "true", "closed": "false", "limit": 50,
                 "offset": off, "order": "volume24hr", "ascending": "false"})
        if not b: break
        out += b
        if len(b) < 50: break
    return out


RX = [("above_daily", re.compile(r"^(bitcoin|ethereum|solana|xrp)-above-on-[a-z]+-\d+-\d{4}$")),
      ("above_hourly", re.compile(r"^(bitcoin|ethereum|solana|xrp)-above-on-[a-z]+-\d+-\d{4}-\d+(am|pm)-et$")),
      ("bucket", re.compile(r"^(bitcoin|ethereum|solana|xrp)-price-on-[a-z]+-\d+-\d{4}$")),
      ("updown_daily", re.compile(r"^(bitcoin|ethereum|solana|xrp)-up-or-down-on-[a-z]+-\d+-\d{4}$")),
      ("updown_hourly", re.compile(r"^(bitcoin|ethereum|solana|xrp)-up-or-down-[a-z]+-\d+-\d{4}-\d+(am|pm)-et$")),
      ("touch", re.compile(r"^what-price-will-(bitcoin|ethereum|solana|xrp)-hit-(?!before)")),
      ("updown_15m", re.compile(r"^(btc|eth|sol|xrp)-updown-15m-\d+$"))]


def classify(slug):
    for kind, rx in RX:
        m = rx.match(slug)
        if m: return kind, ASSETS[m.group(1)]
    return None, None


def iso(s): return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def model(kind, a, m, ev, now):
    """Return (fair_yes_fn(spot_mult, sig_mult), note) for outcome index 0 ('Yes'/'Up')."""
    end = iso(m["endDate"]).timestamp()
    T = end - now
    S0, _ = asset_data(a)
    sig = sigma_for(a, T)
    g = (m.get("groupItemTitle") or "").strip()
    q = m.get("question") or ""
    if kind in ("above_daily", "above_hourly"):
        K = num(re.search(r"\$([\d,\.]+)", q).group(1))
        return (lambda sm, vm: p_above(S0 * sm, K, sig * vm, T)), f"S={S0:,.4g} K={K:,.4g}"
    if kind == "bucket":
        if g.startswith("<"):
            hi = num(g[1:]); f = lambda sm, vm: 1 - p_above(S0 * sm, hi, sig * vm, T)
        elif g.startswith(">"):
            lo = num(g[1:]); f = lambda sm, vm: p_above(S0 * sm, lo, sig * vm, T)
        else:
            lo, hi = map(num, g.split("-"))
            f = lambda sm, vm: p_above(S0 * sm, lo, sig * vm, T) - p_above(S0 * sm, hi, sig * vm, T)
        return f, f"S={S0:,.4g} bucket {g}"
    if kind in ("updown_daily", "updown_hourly", "updown_15m"):
        ptb = (ev.get("eventMetadata") or {}).get("priceToBeat")
        if kind == "updown_hourly":
            st = end - 3600
            k = klines(a, "1h", 1, st)
            ptb = float(k[0][1]) if k and k[0][0] == st * 1000 else None
        elif kind == "updown_15m":
            st = end - 900
            if st > now: ptb = None   # window not started: fair ~0.5, skip
            else:
                k = klines(a, "1m", 1, st)
                ptb = float(k[0][1]) if k else None
        elif ptb is None:
            st = iso(m["eventStartTime"]).timestamp() - 60
            k = klines(a, "1m", 1, st); ptb = float(k[0][4]) if k else None
        if not ptb: return None, "no reference price"
        return (lambda sm, vm: p_above(S0 * sm, ptb, sig * vm, T)), f"S={S0:,.5g} open={ptb:,.5g}"
    if kind == "touch":
        K = num(re.sub(r"[^\d,\.]", "", g))
        up = "↑" in g or K > S0
        if (up and S0 >= K) or (not up and S0 <= K): return None, "already through barrier"
        return (lambda sm, vm: p_touch(S0 * sm, K, sig * vm, T)), f"S={S0:,.5g} barrier {g}"
    return None, "unknown"


def equity_usd_from_ledger(rate):
    """Latest paper equity from the main ledger (read-only); fallback 10,000 THB."""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{os.path.join(ROOT, 'ledger.sqlite')}?mode=ro", uri=True)
        r = c.execute("select equity_usd from runs order by id desc limit 1").fetchone()
        if r and r[0]: return float(r[0])
    except Exception:
        pass
    return BANKROLL_THB / rate


def held_events():
    """Event slugs already held in the main ledger (read-only); run.py allows one position per event."""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{os.path.join(ROOT, 'ledger.sqlite')}?mode=ro", uri=True)
        return {r[0] for r in c.execute("select event_slug from positions where status='open'")}
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "crypto.json"))
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--test-loosen", action="store_true",
                    help="TEST ONLY: drop edge thresholds to exercise the schema; never use with the real --out")
    a_ = ap.parse_args()
    if a_.test_loosen:
        assert os.path.abspath(a_.out) != os.path.join(HERE, "crypto.json"), "refusing to loosen the live file"
        CFG.update(min_robust=-1.0, min_net=-1.0, min_net_touch=-1.0)
    now = time.time()
    rate = api.usd_thb()[0]
    equity_usd = equity_usd_from_ledger(rate)
    pairs = []      # (kind, asset, market, event)
    for ev in crypto_events():
        kind, a = classify(ev["slug"])
        if kind:
            for m in ev.get("markets", []): pairs.append((kind, a, m, ev))
    seen = {m["id"] for _, _, m, _ in pairs}
    for m in api.crypto_15m_markets():
        ev = (m.get("events") or [{}])[0]
        kind, a = classify(m["slug"])
        if kind and m["id"] not in seen: pairs.append((kind, a, m, ev))
    live = []
    for kind, a, m, ev in pairs:
        if m.get("closed") or not m.get("acceptingOrders") or not m.get("enableOrderBook"): continue
        if not m.get("endDate"): continue
        T = iso(m["endDate"]).timestamp() - now
        if T < CFG["min_minutes"] * 60: continue
        if float(m.get("liquidityNum") or 0) < CFG["min_liq"]: continue
        if len(api.parse_list(m.get("clobTokenIds"))) != 2: continue
        live.append((kind, a, m, ev))
    bk = api.books({t for _, _, m, _ in live for t in api.parse_list(m["clobTokenIds"])})
    rows, props = [], []
    for kind, a, m, ev in live:
        try:
            f, note = model(kind, a, m, ev, now)
        except Exception as e:
            f, note = None, f"model error {e}"
        if f is None: continue
        toks = api.parse_list(m["clobTokenIds"]); outs = api.parse_list(m["outcomes"])
        basis = 0.0004 if kind == "updown_15m" else 0.0001
        scen = [f(sm, vm) for sm in (1 - basis, 1, 1 + basis) for vm in CFG["sig_band"]]
        fair0 = min(max(f(1, 1), 0), 1)
        for i, (t, o) in enumerate(zip(toks, outs)):
            b = bk.get(t)
            if not b: continue
            ask, asz = best(b, "asks"); bid, _ = best(b, "bids")
            if ask is None: continue
            fair = fair0 if i == 0 else 1 - fair0
            fs = [s if i == 0 else 1 - s for s in scen]
            fe = fee(ask)
            net = fair - ask - fe
            robust = min(fs) - ask - fe
            spread = (ask - bid) if bid is not None else None
            T = iso(m["endDate"]).timestamp() - now
            row = dict(kind=kind, asset=a, slug=m["slug"], event_slug=ev.get("slug"), outcome=o, ask=ask,
                       bid=bid, fair=round(fair, 4), net=round(net, 4), robust=round(robust, 4),
                       hours=round(T / 3600, 2), note=note)
            rows.append(row)
            need = CFG["min_net_touch"] if kind == "touch" else CFG["min_net"]
            # Worst fill the loop allows is limit + 0.5c; demand the edge at that price.
            limit = ask
            wpx = min(limit + SLIP, 0.999)
            wfee = fee(wpx)
            worst = min(fs) - wpx - wfee
            row.update(worst=round(worst, 4))
            if not (CFG["ask_lo"] <= ask <= CFG["ask_hi"] and net >= need and worst >= CFG["min_robust"]):
                continue
            if T < CFG["min_minutes"] * 60 + 120:
                continue
            cost = wpx + wfee
            kelly = max(0.0, (min(fs) - cost) / (1 - cost))
            cap_usd = equity_usd * CFG["max_pct"]
            size_usd = min(cap_usd, equity_usd * kelly * CFG["kelly_frac"])
            if a_.test_loosen:
                size_usd = cap_usd
            depth = sum(sz for px, sz in levels(b, "asks") if px <= limit + SLIP)
            shares = math.floor(min(size_usd / wpx, depth) * 100) / 100
            if shares < CFG["min_shares"]:
                continue
            conf = max(0.1, min(0.6, 0.3 + 3 * worst - (0.1 if kind in ("touch", "updown_15m") else 0)))
            props.append(dict(
                kind=("price-on-date" if kind == "bucket" else kind), side="buy", market_id=str(m["id"]), token_id=t, slug=m["slug"],
                event_slug=ev.get("slug") or m["slug"], outcome=o, limit_price=round(limit, 4),
                size_shares=shares, size_usd=round(shares * limit, 2), question=m.get("question"),
                condition_id=m.get("conditionId"), _end=iso(m["endDate"]),
                best_ask=ask, best_bid=bid, spread=round(spread, 4) if spread is not None else None,
                fair_prob=round(fair, 4), fair_prob_worst=round(min(fs), 4),
                net_edge=round(net, 4), worst_case_net_edge=round(worst, 4),
                fee_per_share=round(fe, 5), confidence=round(conf, 2),
                reason=(f"{kind} {a}: {note}, sigma(ann)~{sigma_for(a, T) * math.sqrt(365 * 86400) * 100:.0f}%, "
                        f"T={T / 3600:.2f}h -> fair {fair:.3f} (worst {min(fs):.3f}) vs ask {ask:.3f}; "
                        f"worst-case edge at limit+0.5c after fee {worst:.3f}"),
                market_end=iso(m["endDate"]).astimezone(BKK).isoformat(timespec="seconds")))
    best_by_ev = {}
    for p_ in sorted(props, key=lambda p_: -p_["worst_case_net_edge"]):
        best_by_ev.setdefault(p_["event_slug"], p_)
    held = held_events()
    skipped_held = sorted(p_["event_slug"] for p_ in best_by_ev.values() if p_["event_slug"] in held)
    props = sorted([p_ for p_ in best_by_ev.values() if p_["event_slug"] not in held],
                   key=lambda p_: -p_["worst_case_net_edge"])
    gen = dt.datetime.now(BKK).replace(microsecond=0)
    for n, p_ in enumerate(props, 1):
        ttl = TTL_MIN.get(p_["kind"], 60)
        exp = min(gen + dt.timedelta(minutes=ttl), p_.pop("_end").astimezone(BKK) - dt.timedelta(minutes=CFG["min_minutes"]))
        p_["id"] = f"CRY-{gen:%Y%m%d%H%M%S}-{n:02d}"
        p_["generated_at"] = gen.isoformat()
        p_["expires_at"] = exp.replace(microsecond=0).isoformat()
    file_exp = min([dt.datetime.fromisoformat(p_["expires_at"]) for p_ in props],
                   default=gen + dt.timedelta(minutes=60))
    order = ["id", "generated_at", "expires_at", "side", "market_id", "token_id", "slug", "event_slug",
             "outcome", "limit_price", "size_shares", "size_usd", "question"]
    props = [{**{k: p_[k] for k in order}, **{k: v for k, v in p_.items() if k not in order}} for p_ in props]
    near = sorted(rows, key=lambda r: -r.get("worst", r["robust"]))[:10]
    out = dict(generated_at=gen.isoformat(), expires_at=file_exp.isoformat(), agent="PM-Crypto",
               paper_only=True, proposals=props,
               scan_stats=dict(markets_scanned=len(live), outcomes_priced=len(rows),
                               equity_usd=round(equity_usd, 2), usd_thb=rate,
                               fee_model="fee/share = 0.07*p*(1-p) taker; edge checked at limit+0.5c",
                               spot_source="Binance public market data (data-api.binance.vision)",
                               vol={k: {"sigma_ann_short_1m_6h": round(v[0] * math.sqrt(365 * 86400), 3),
                                        "sigma_ann_long_1h_7d": round(v[1] * math.sqrt(365 * 86400), 3),
                                        "spot": SPOT[k]} for k, v in VOL.items()},
                               rules=CFG, skipped_already_held_event=skipped_held),
               near_misses_info_only=near)
    if not a_.dry:
        tmp = os.path.join(os.path.dirname(a_.out), "." + os.path.basename(a_.out) + ".tmp")
        with open(tmp, "w") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, a_.out)
    print(f"{gen.isoformat()} scanned={len(live)} priced={len(rows)} proposals={len(props)} "
          f"held_skipped={len(skipped_held)} equity=${equity_usd:.2f}")
    for p_ in props:
        print("  ", p_["id"], p_["slug"], p_["outcome"], p_["limit_price"], p_["size_shares"],
              p_["fair_prob"], p_["worst_case_net_edge"], p_["expires_at"])


if __name__ == "__main__":
    main()
