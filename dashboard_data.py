#!/usr/bin/env python3
"""Read-only dashboard data for the Polymarket PAPER-trading ledger.

Prints ONE compact JSON object on a single line (for the dashboard Page's
`const DATA = /*DATA_START*/{...}/*DATA_END*/;` line). Never writes the ledger,
never touches the loop. Usage:
    python3 /workspace/polymarket/dashboard_data.py            # JSON only
    python3 /workspace/polymarket/dashboard_data.py --line     # full DATA line
"""
import json, os, re, sqlite3, sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ledger.sqlite")
BKK = timezone(timedelta(hours=7))
CAPS = dict(general=40.0, short=10.0, crypto_dir=15.0)
UPDOWN = re.compile(r"^([a-z0-9]+)-updown-(5m|15m)-(\d+)$")
COIN = {"btc": "Bitcoin", "eth": "Ethereum", "sol": "Solana", "xrp": "XRP", "doge": "Dogecoin", "bnb": "BNB"}
SIG = {"MOMENTUM_24H": "โมเมนตัม 24 ชม.", "NEAR_RES_FAVOURITE": "ใกล้ปิดรอบ",
       "PROPOSAL": "ข้อเสนอผู้ช่วย", "ARB_BUY_BOTH": "อาร์บิทราจ"}
CAT = {"crypto": "คริปโต", "sports": "กีฬา", "politics": "การเมือง", "economics": "เศรษฐกิจ",
       "culture": "วัฒนธรรม", "other": "อื่นๆ"}
REASON = {"AUTO_CLOSE_099": "ปิดอัตโนมัติที่ 0.99", "settle": "ตลาดตัดสินผล", None: "ตลาดตัดสินผล",
          "MANUAL_CUT": "ตัดไม้ตามคำสั่ง",
          "MANUAL_USER_CLOSE": "ปิดตามคำสั่งผู้ใช้"}


def r(x, n=2):
    return None if x is None else round(float(x), n)


def hm(ts):
    d = datetime.fromisoformat(ts).astimezone(BKK)
    return d.strftime("%d/%m %H:%M")


def name(p):
    m = UPDOWN.match(p["event_slug"] or "")
    q = p["question"] or ""
    if m:
        win = q.split(",")[-1].strip() if "," in q else ""
        return f"{COIN.get(m.group(1), m.group(1).upper())} ขึ้นหรือลง {win}".strip()
    return q


def sig_label(s):
    s = s or ""
    if s.startswith("PROPOSAL"):
        return SIG["PROPOSAL"]
    return SIG.get(s, s)


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    meta = {k: v for k, v in c.execute("select key, value from meta")}
    start_thb = float(meta.get("start_thb", 10000))
    start_usd = float(meta.get("start_usd", 0))
    runs = [dict(x) for x in c.execute("select ts, usd_thb, cash_usd, open_value_usd, equity_usd, equity_thb, n_open from runs order by id")]
    last = runs[-1]
    now = datetime.now(BKK)

    # equity history, downsampled to <= 200 points (always keep first, last, min, max)
    n = len(runs)
    MAXP = 120  # keep the page source small; must stay <= 200
    idx = set(round(i * (n - 1) / (MAXP - 1)) for i in range(MAXP)) if n > MAXP else set(range(n))
    eq = [x["equity_thb"] for x in runs]
    idx |= {0, n - 1, eq.index(min(eq)), eq.index(max(eq))}
    idx = sorted(idx)
    while len(idx) > MAXP:  # drop from the interior if the kept extremes pushed us over
        cand = [i for i in idx if i not in (0, n - 1, eq.index(min(eq)), eq.index(max(eq)))]
        idx.remove(cand[len(cand) // 2])
    hist = [[hm(runs[i]["ts"]), round(runs[i]["equity_thb"])] for i in idx]

    def is_new(ts):  # opened/closed within the last 10 minutes -> row fades in
        return bool(ts) and (now - datetime.fromisoformat(ts)).total_seconds() <= 600

    pos = [dict(x) for x in c.execute("select * from positions order by id")]
    open_ = [p for p in pos if p["status"] == "open"]
    closed = [p for p in pos if p["status"] != "open"]

    equity_usd = last["equity_usd"]
    general = short = crypto = 0.0
    open_rows = []
    gsize = {}
    for p in open_:
        gsize[p["group_key"]] = gsize.get(p["group_key"], 0) + 1

    def is_arb(p):  # same definition as run.is_arb_pos
        if p["signal"] == "ARB_BUY_BOTH" or str(p["group_key"] or "").startswith("arb:"):
            return True
        return str(p["signal"] or "").startswith("PROPOSAL") and gsize.get(p["group_key"], 0) > 1

    for p in open_:
        v = p["mark_value"] or 0.0
        m = UPDOWN.match(p["event_slug"] or "")
        is_short = bool(m)
        if is_short:
            short += v  # 15m bucket is exempt from the directional-crypto share
        else:
            general += v
            if (p["category"] or "") == "crypto" and not is_arb(p):
                crypto += v
        ended = False
        if m:
            mins = 5 if m.group(2) == "5m" else 15
            ended = now.timestamp() > int(m.group(3)) + mins * 60
        cost = (p["cost_usd"] or 0) + (p["fee_usd"] or 0)
        open_rows.append(dict(
            id=p["id"], name=name(p), slug=p["event_slug"], side=p["outcome"],
            entry=r(p["avg_price"], 4), mark=r(p["mark_price"], 4), shares=r(p["shares"], 1),
            cost=r(cost), value=r(v), upnl=r(v - cost),
            cat=CAT.get(p["category"], p["category"]), sig=sig_label(p["signal"]),
            short=is_short, pending=bool(ended or not p["mark_price"]), opened=hm(p["opened_at"]),
            new=is_new(p["opened_at"])))

    def pct(x):
        return r(100 * x / equity_usd, 1) if equity_usd else 0

    closed.sort(key=lambda p: (p["closed_at"] or "", p["id"]), reverse=True)
    closed_rows = [dict(
        id=p["id"], at=hm(p["closed_at"]) if p["closed_at"] else "", name=name(p), slug=p["event_slug"],
        side=p["outcome"], entry=r(p["avg_price"], 4), exit=r(p["settle_price"], 4), pnl=r(p["pnl_usd"]),
        reason=REASON.get(p["close_reason"], p["close_reason"]), sig=sig_label(p["signal"]),
        new=is_new(p["closed_at"])) for p in closed[:20]]

    def breakdown(key):
        agg = {}
        for p in closed:
            k = key(p)
            a = agg.setdefault(k, dict(label=k, pnl=0.0, n=0, w=0, l=0))
            a["pnl"] += p["pnl_usd"] or 0; a["n"] += 1
            if (p["pnl_usd"] or 0) > 0.005: a["w"] += 1
            elif (p["pnl_usd"] or 0) < -0.005: a["l"] += 1
        out = sorted(agg.values(), key=lambda a: -a["pnl"])
        for a in out: a["pnl"] = r(a["pnl"])
        return out

    realized = sum(p["pnl_usd"] or 0 for p in closed)
    wins = sum(1 for p in closed if (p["pnl_usd"] or 0) > 0.005)
    losses = sum(1 for p in closed if (p["pnl_usd"] or 0) < -0.005)
    fees = sum((p["fee_usd"] or 0) + (p["close_fee_usd"] or 0) for p in pos)
    peak = max(runs, key=lambda x: x["equity_thb"]); low = min(runs, key=lambda x: x["equity_thb"])
    rate = last["usd_thb"]

    data = dict(
        generated_at=now.isoformat(timespec="seconds"),
        last_run=last["ts"], run_count=n, started_at=meta.get("started_at"),
        rate=r(rate, 4), start_thb=start_thb, start_usd=start_usd,
        equity_thb=r(last["equity_thb"]), equity_usd=r(equity_usd),
        ret_pct=r(100 * (last["equity_thb"] / start_thb - 1), 2),
        cash_usd=r(last["cash_usd"]), cash_thb=r(last["cash_usd"] * rate),
        open_value_usd=r(last["open_value_usd"]),
        peak=[hm(peak["ts"]), r(peak["equity_thb"], 1)], low=[hm(low["ts"]), r(low["equity_thb"], 1)],
        history=hist,
        exposure=[
            dict(key="general", label="ไม้ทั่วไป", usd=r(general), pct=pct(general), cap=CAPS["general"], kind="เพดาน"),
            dict(key="short", label="คริปโต 15 นาที", usd=r(short), pct=pct(short), cap=CAPS["short"], kind="เพดาน"),
            dict(key="crypto_dir", label="คริปโตทิศทาง", usd=r(crypto), pct=pct(crypto), cap=CAPS["crypto_dir"], kind="เกณฑ์หยุด"),
        ],
        counts=dict(open=len(open_), closed=len(closed), wins=wins, losses=losses,
                    pending=sum(1 for o in open_rows if o["pending"]), total=len(pos)),
        realized_usd=r(realized), unrealized_usd=r(sum(o["upnl"] for o in open_rows)), fees_usd=r(fees),
        open=open_rows, closed=closed_rows,
        by_signal=breakdown(lambda p: sig_label(p["signal"])),
        by_category=breakdown(lambda p: CAT.get(p["category"], p["category"])),
        by_reason=breakdown(lambda p: REASON.get(p["close_reason"], p["close_reason"])),
    )
    s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("*/", "*\\/")  # never break the marker comments
    if "--line" in sys.argv:
        print(f"const DATA = /*DATA_START*/{s}/*DATA_END*/;")
    else:
        print(s)


if __name__ == "__main__":
    main()
