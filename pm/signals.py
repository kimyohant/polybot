"""Signal computation from live Gamma market data + CLOB order books."""
import datetime as dt
import re
from .api import parse_list
from .book import best, taker_fee

CFG = dict(
    min_liquidity=10_000,      # USD, Gamma liquidityNum
    min_volume24h=5_000,       # USD
    min_liquidity_15m=1_000,   # short-lived crypto Up/Down windows; no 24h-volume floor
    wide_spread=0.05,          # 5 cents
    near_hours=144,            # "near resolution" window (was 72h; half as strict)
    extreme_lo=0.85, extreme_hi=0.97,  # favourite ask band (was 0.90-0.97; half as strict)
    momentum_move=0.05,        # |24h price change| >= 5 pts (was 10)
    momentum_band=(0.15, 0.85),
    arb_min_edge=0.005,        # net-of-fee edge per $1 payout
    arb_max_days=30,           # set from ARB_MAX_DAYS below
)

# Block any NEW arbitrage / multi-leg basket whose resolution is later than this.
# Existing open arbs are left untouched.
ARB_MAX_DAYS = 30
CFG["arb_max_days"] = ARB_MAX_DAYS

# e.g. btc-updown-15m-1794789000 — title uses an ET calendar date that can already
# be "yesterday" in UTC while the window is still live.
_UPDOWN_SLUG = re.compile(r"^[a-z0-9]+-updown-(?:5m|15m)-\d+$")


def is_short_crypto_updown_slug(slug):
    return bool(_UPDOWN_SLUG.match(slug or ""))


def is_short_crypto_updown(m):
    return is_short_crypto_updown_slug(m.get("slug") or "")


def is_crypto_market(m):
    """True for crypto-fee markets and 15m/5m Up/Down windows."""
    if is_short_crypto_updown(m):
        return True
    ft = m.get("feeType") or ""
    return ft.split("_")[0] == "crypto"

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH = r"(?:january|february|march|april|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec|may)"

# Long-stop / fallback clauses in market descriptions (e.g. "if the result isn't
# known by May 31, 2027, this market will resolve to Other"). These are not the
# expected resolution date and must not inflate NEAR_RES_FAVOURITE hours_left.
_FALLBACK_SENTENCE = re.compile(
    r"[^.]*?(?:isn't known by|is not known by|if no such\b|"
    r"does not start by|no official result by|"
    r"11:59\s*pm\s*et,\s*this market will resolve)[^.]*\.",
    re.I,
)


def fee_rate(m):
    if not m.get("feesEnabled"):
        return 0.0
    fs = m.get("feeSchedule") or {}
    if isinstance(fs, str):
        try:
            import json
            fs = json.loads(fs)
        except Exception:
            fs = {}
    if not isinstance(fs, dict):
        return 0.05
    return float(fs.get("rate", 0.05))


def event_slug(m):
    ev = m.get("events") or []
    return ev[0].get("slug") if ev else m.get("slug")


def _aware(d):
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _parse_end_field(s):
    """Parse a Gamma endDate / endDateIso / gameStartTime value to an aware UTC datetime."""
    if not s:
        return None
    s = str(s).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            d = dt.date.fromisoformat(s)
            # Date-only: keep the market tradable through the end of that UTC day.
            return dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=dt.timezone.utc)
        s = s.replace("Z", "+00:00")
        s = re.sub(r"([+-]\d{2})$", r"\1:00", s)
        return _aware(dt.datetime.fromisoformat(s))
    except Exception:
        return None


def market_end(m):
    """Deadline from the market's own Gamma fields (not the parent event's endDate)."""
    for key in ("endDate", "endDateIso"):
        end = _parse_end_field(m.get(key))
        if end is not None:
            return end
    return None


def market_finished(m, now=None):
    """True if the market/game appears to have ended for auto-close purposes.

    Criteria: closed or not accepting orders, Gamma endDate passed, or
    gameStartTime passed. Caveat: for many sports markets Gamma sets endDate
    equal to gameStartTime (tip-off), so this can become true after the game
    starts, not only after the final buzzer.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if m.get("closed") or m.get("archived"):
        return True
    if m.get("acceptingOrders") is False:
        return True
    end = market_end(m)
    if end is not None and end <= now:
        return True
    gst = _parse_end_field(m.get("gameStartTime"))
    if gst is not None and gst <= now:
        return True
    return False


def hours_left(m, now=None):
    """Hours until the later of Gamma endDate and any detectable resolution date."""
    end = resolution_end(m)
    if end is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return (end - now).total_seconds() / 3600


def _valid_date(y, mo, d):
    try:
        return dt.date(int(y), int(mo), int(d))
    except ValueError:
        return None


def _dates_in_text(text, year_hint):
    """Calendar dates (and range ends) mentioned in a question/title. Year-only mentions are ignored."""
    if not text:
        return []
    found = []
    raw = text.replace("–", "-").replace("—", "-")
    # Keep hyphens for ranges ("November 9-15") and ISO dates; also scan a
    # spaced copy so slug-style "november-8" still matches the month-name patterns.
    variants = [raw.lower(), raw.replace("-", " ").lower()]

    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", raw):
        d = _valid_date(m.group(1), m.group(2), m.group(3))
        if d:
            found.append(d)

    seen = set()
    def add(d):
        if d and d not in seen:
            seen.add(d)
            found.append(d)

    for low in variants:
        # "November 9-15[, 2026]" / "November 9 to 15"
        for m in re.finditer(
            rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|to)\s*(\d{{1,2}})(?:st|nd|rd|th)?(?:[, ]+(\d{{4}}))?",
            low,
        ):
            mo = _MONTHS[m.group(1).rstrip(".")]
            y = int(m.group(4)) if m.group(4) else year_hint
            add(_valid_date(y, mo, m.group(3)))

        # "November 8[, 2026]" / "Nov. 8"
        for m in re.finditer(
            rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:[, ]+(\d{{4}}))?",
            low,
        ):
            mo = _MONTHS[m.group(1).rstrip(".")]
            y = int(m.group(3)) if m.group(3) else year_hint
            add(_valid_date(y, mo, m.group(2)))

        # "8 November 2026"
        for m in re.finditer(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})\.?(?:[, ]+(\d{{4}}))?",
            low,
        ):
            mo = _MONTHS[m.group(2).rstrip(".")]
            y = int(m.group(3)) if m.group(3) else year_hint
            add(_valid_date(y, mo, m.group(1)))

    return found


def _strip_fallback_clauses(text):
    if not text:
        return ""
    return _FALLBACK_SENTENCE.sub(" ", text)


def _text_resolution_dates(m, year_hint):
    """Dates from the question/title plus description (fallback/long-stop clauses removed)."""
    dates = []
    for key in ("question", "groupItemTitle", "title"):
        dates.extend(_dates_in_text(m.get(key) or "", year_hint))
    dates.extend(_dates_in_text(_strip_fallback_clauses(m.get("description") or ""), year_hint))
    for ev in m.get("events") or []:
        dates.extend(_dates_in_text(ev.get("title") or "", year_hint))
        dates.extend(_dates_in_text(_strip_fallback_clauses(ev.get("description") or ""), year_hint))
    return dates


def resolution_end(m):
    """Latest of Gamma endDate and detectable dates in the question/description.

    Used by NEAR_RES_FAVOURITE so a stale early endDate (e.g. election day
    when a runoff is scheduled later) does not look 'near resolution'.

    Caveat: this is a cheap text parse. It can miss a later real resolution
    date that is not written as a calendar date, and a novel long-stop wording
    that escapes the fallback stripper could push the deadline too far out.
    """
    end = market_end(m)
    year_hint = end.year if end is not None else dt.datetime.now(dt.timezone.utc).year
    candidates = []
    if end is not None:
        candidates.append(end)
    for d in _text_resolution_dates(m, year_hint):
        candidates.append(dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=dt.timezone.utc))
    return max(candidates) if candidates else None


def arb_resolves_too_far(markets, now=None):
    """True if a new arb / multi-leg basket resolves more than ARB_MAX_DAYS out.

    Uses resolution_end() — the later of Gamma endDate and calendar dates in
    the question/description — the same logic as NEAR_RES_FAVOURITE. For a
    basket, the latest leg date is used (capital is locked until the last
    payout). Returns False when no resolution date can be determined.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if markets is None:
        return False
    if isinstance(markets, dict):
        markets = [markets]
    latest = None
    for m in markets:
        if not m:
            continue
        end = resolution_end(m)
        if end is not None and (latest is None or end > latest):
            latest = end
    if latest is None:
        return False
    return (latest - now) > dt.timedelta(days=ARB_MAX_DAYS)


def title_deadline_passed(m, now=None):
    """True if the latest date in the question/title is before today's UTC date.

    Uses the market endDate's year when the text omits one, so a still-open event
    with a stale later endDate cannot hide an already-passed 'by November 8'.
    Year-only phrases such as 'in 2026' are not treated as a deadline.
    Description dates are ignored here (they often include historical context).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    end = market_end(m)
    year_hint = end.year if end is not None else now.year
    dates = []
    for key in ("question", "groupItemTitle", "title"):
        dates.extend(_dates_in_text(m.get(key) or "", year_hint))
    if not dates:
        return False
    return max(dates) < now.date()


def expired(m, now=None):
    """True if the market deadline has passed or its question/title date is already past.

    Position #9 (US x Iran ceasefire by November 8) was opened on 15 Nov 2026 via
    MOMENTUM_24H while Gamma still showed the market as active; at that time the
    scanner did not reject past endDate / past title dates. That filter now runs
    in eligible(), compute() (every signal path, including momentum), and again
    in open_positions() before a fill.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    end = market_end(m)
    if end is not None and end <= now:
        return True
    # 15m/5m crypto Up/Down titles look like "November 15, 7:30PM-7:45PM ET".
    # After UTC midnight that ET date is already "yesterday", so a date-only
    # title check would kill a still-live window. Trust endDate instead.
    if is_short_crypto_updown(m):
        return False
    return title_deadline_passed(m, now)


def eligible(m, now=None):
    """Tradeable, still-open binary markets with enough liquidity that have not ended."""
    if not m.get("enableOrderBook"):
        return False
    if not m.get("acceptingOrders"):
        return False
    if m.get("closed") or m.get("archived"):
        return False
    if m.get("active") is False or m.get("tradingEligible") is False:
        return False
    if expired(m, now):
        return False
    if len(parse_list(m.get("clobTokenIds"))) != 2:
        return False
    # Each 15m window is a brand-new market, so 24h volume is often tiny or
    # unset, and in-play liquidity can sit well below the $10k general floor.
    if is_short_crypto_updown(m):
        return float(m.get("liquidityNum") or 0) >= CFG["min_liquidity_15m"]
    return (float(m.get("liquidityNum") or 0) >= CFG["min_liquidity"]
            and float(m.get("volume24hr") or 0) >= CFG["min_volume24h"])


def compute(markets, books):
    """Returns list of signal dicts (one market may yield several)."""
    sigs = []
    for m in markets:
        # Re-check on every signal path (arb, near-res, momentum, info).
        if expired(m) or m.get("closed") or m.get("acceptingOrders") is False:
            continue
        toks = parse_list(m.get("clobTokenIds"))
        outs = parse_list(m.get("outcomes"))
        if len(toks) != 2 or any(t not in books for t in toks):
            continue
        rate = fee_rate(m)
        q = []
        for t, o in zip(toks, outs):
            bb, bbs = best(books[t], "bids")
            ba, bas = best(books[t], "asks")
            q.append(dict(token=t, outcome=o, bid=bb, bid_sz=bbs, ask=ba, ask_sz=bas))
        base = dict(market=m, quotes=q, fee_rate=rate, hours_left=hours_left(m),
                    slug=event_slug(m), question=m.get("question"))
        if any(x["ask"] is None or x["bid"] is None for x in q):
            continue
        # 1) Complementary-outcome arbitrage: buy both sides for < $1 net of fees
        ask_sum = q[0]["ask"] + q[1]["ask"]
        fees = sum(taker_fee(1, x["ask"], rate) for x in q)
        edge = 1 - ask_sum - fees
        if edge >= CFG["arb_min_edge"]:
            if arb_resolves_too_far(m):
                h = base["hours_left"]
                days_s = f"{h/24:.1f}d" if h is not None else "unknown"
                print(f"reject arb_resolves_gt_30d: {m.get('question') or event_slug(m)} "
                      f"resolves in {days_s}")
            else:
                sigs.append(dict(base, signal="ARB_BUY_BOTH", score=edge * 100, edge=edge,
                                 detail=f"askYES+askNO={ask_sum:.3f}, fees/pair={fees:.4f}, net edge={edge:.4f}/share"))
        bid_sum = q[0]["bid"] + q[1]["bid"]
        if bid_sum > 1.005:
            sigs.append(dict(base, signal="INFO_BID_SUM_GT_1", score=(bid_sum - 1) * 50, edge=bid_sum - 1,
                             detail=f"bidYES+bidNO={bid_sum:.3f} (needs inventory/minting; not traded)"))
        # 2) Wide spread (informational; we don't simulate market making)
        for x in q:
            sp = x["ask"] - x["bid"]
            if sp >= CFG["wide_spread"]:
                sigs.append(dict(base, signal="INFO_WIDE_SPREAD", score=sp * 20, edge=sp, side=x,
                                 detail=f"{x['outcome']}: bid {x['bid']:.3f} / ask {x['ask']:.3f}, spread {sp:.3f}"))
                break
        # 3) Near resolution, priced at an extreme: buy the favourite at its ask.
        # hours_left uses max(endDate, dates in question/description) so a
        # stale early Gamma endDate (Ljubljana runoff Nov 29 vs endDate Nov 16)
        # does not qualify as near-resolution.
        h = base["hours_left"]
        if h is not None and 0 < h <= CFG["near_hours"]:
            fav = max(q, key=lambda x: x["ask"])
            if CFG["extreme_lo"] <= fav["ask"] <= CFG["extreme_hi"]:
                net = 1 - fav["ask"] - taker_fee(1, fav["ask"], rate)
                if net > 0:
                    sigs.append(dict(base, signal="NEAR_RES_FAVOURITE", score=net / fav["ask"] * 100 * 2,
                                     edge=net, side=fav,
                                     detail=f"{fav['outcome']} ask {fav['ask']:.3f}, {h:.1f}h to deadline, "
                                            f"return if it wins {net / fav['ask'] * 100:.1f}% after fee"))
        # 4) 24h momentum (Gamma oneDayPriceChange refers to first outcome)
        ch = m.get("oneDayPriceChange")
        if ch is not None and abs(float(ch)) >= CFG["momentum_move"]:
            side = q[0] if float(ch) > 0 else q[1]
            lo, hi = CFG["momentum_band"]
            if lo <= side["ask"] <= hi:
                sigs.append(dict(base, signal="MOMENTUM_24H", score=abs(float(ch)) * 30, edge=abs(float(ch)), side=side,
                                 detail=f"{q[0]['outcome']} moved {float(ch) * 100:+.1f} pts in 24h; "
                                        f"follow -> {side['outcome']} ask {side['ask']:.3f}"))
    return sigs
