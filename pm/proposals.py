"""Load helper-agent proposal JSON and turn actionable buys into PROPOSAL signals.

Paper trading only — never sends a real order. Agent file formats differ, so
this module normalizes conservatively and skips anything that is not a buy
with a market/token/slug, an outcome, and a limit/entry price.
"""
import datetime as dt
import hashlib
import json
import os
import re

from .api import market_by_id, market_by_slug, books, parse_list
from .book import best, taker_fee
from .signals import (
    CFG, arb_resolves_too_far, event_slug, expired, fee_rate, hours_left,
    is_crypto_market, is_short_crypto_updown, is_short_crypto_updown_slug,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PROPOSALS_DIR = os.path.join(os.path.dirname(HERE), "proposals")

# Informational helper dumps — not a source of new trades.
SKIP_FILES = {"risk.json", "analytics.json"}

TRADE_LIST_KEYS = ("proposals", "new_proposals", "trades", "orders")

# User declined these arb events (16 Nov 2026); never auto-enter them.
DECLINED_PROPOSAL_IDS = {"ARB-202611160801-01", "ARB-202611160801-03"}
DECLINED_EVENT_SLUGS = {
    "maduro-prison-time-527",
    "bank-of-korea-decision-in-january-2027",
}
DECLINED_EVENT_IDS = {"141230", "1310202"}
DECLINED_TITLE_RE = re.compile(
    r"maduro\s+prison\s+time|bank\s+of\s+korea\s+decision\s+in\s+january",
    re.I,
)

# Already paper-filled (group arb:770454) before the PROPOSAL path existed.
SEEDED_FILLED = (
    dict(
        proposal_id="ARB-202611160801-02",
        proposal_hash=None,  # filled in on first seed from a hash of the slug
        source_file="arbitrage.json",
        event_slug="how-many-dissent-at-the-december-fed-meeting-20260730000304473",
        event_id="770454",
        status="filled",
        reason="already entered as group arb:770454 (Fed dissent Dec)",
        group_key="arb:770454",
    ),
    dict(
        proposal_id="ARB-202611160801-01",
        source_file="arbitrage.json",
        event_slug="maduro-prison-time-527",
        event_id="141230",
        status="declined",
        reason="user declined (Maduro Prison Time)",
    ),
    dict(
        proposal_id="ARB-202611160801-03",
        source_file="arbitrage.json",
        event_slug="bank-of-korea-decision-in-january-2027",
        event_id="1310202",
        status="declined",
        reason="user declined (Bank of Korea January)",
    ),
)

PROPOSAL_SLIPPAGE = 0.005  # +0.5¢ over the proposed limit, no more
MAX_AGE_15M_HOURLY = 2 * 60
MAX_AGE_DAILY = 30 * 60
MAX_AGE_DEFAULT = 60 * 60

_SKIP_ACTIONS = {
    "hold", "hold_position", "cut_loss", "cut_loss_and_block_market",
    "reduce_or_exit_to_free_cap", "package_exits_for_headroom",
    "adjust_cap_reserve_for_15m", "block_new_correlated_crypto",
    "keep_collecting", "sell", "exit", "close", "reduce", "watch",
    "no_trade", "informational", "keep", "skip", "reject", "rejected",
}

_BUY = {"buy", "b", "long", "yes_buy"}

_TZ_BKK = dt.timezone(dt.timedelta(hours=7))


def _now():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(v, default=None):
    """Parse an ISO timestamp; naive values are treated as Asia/Bangkok."""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e12:
            ts /= 1000.0
        return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    s = str(v).strip()
    if not s:
        return default
    try:
        s = s.replace("Z", "+00:00")
        s = re.sub(r"([+-]\d{2})$", r"\1:00", s)
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_TZ_BKK)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return default


def _first(*vals):
    for v in vals:
        if v is None or v == "":
            continue
        return v
    return None


def _s(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _agent_name(meta, filename):
    src = meta.get("source")
    if isinstance(src, str) and src.strip():
        return src.strip()
    for k in ("agent", "name"):
        if _s(meta.get(k)):
            return _s(meta[k])
    scan = meta.get("scan") if isinstance(meta.get("scan"), dict) else {}
    if _s(scan.get("name")):
        return _s(scan["name"])
    stem = os.path.splitext(filename)[0]
    return {"crypto": "PM-Crypto", "sports": "PM-Sports", "politics": "PM-Politics",
            "arbitrage": "PM-Arbitrage", "risk": "PM-Risk",
            "analytics": "PM-Analytics"}.get(stem, stem)


def _file_meta(data, path):
    scan = data.get("scan") if isinstance(data.get("scan"), dict) else {}
    generated = _first(data.get("generated_at"), scan.get("generated_at"))
    expires = _first(data.get("expires_at"), scan.get("expires_at"))
    mtime = dt.datetime.fromtimestamp(os.path.getmtime(path), tz=dt.timezone.utc)
    return dict(
        generated_at=parse_ts(generated, default=None),
        expires_at=parse_ts(expires, default=None),
        mtime=mtime,
        agent=_agent_name(data, os.path.basename(path)),
        raw_generated=generated,
        raw_expires=expires,
    )


def _is_buy(side):
    if side is None or side == "":
        return True
    return str(side).strip().lower() in _BUY


def _leg_from(obj):
    """Pull one buy leg out of a dict. Returns None if it is not a buy trade."""
    if not isinstance(obj, dict):
        return None
    action = _s(obj.get("action"))
    if action and action.lower() in _SKIP_ACTIONS:
        return None
    # Parameter / risk recommendations have no trade target.
    if "parameter" in obj and "proposed_value" in obj and not _first(
        obj.get("market_id"), obj.get("token_id"), obj.get("slug")
    ):
        return None
    side = _first(obj.get("side"), obj.get("action"))
    # "buy" is a side; "cut_loss" already skipped. If action is "hold" skipped.
    if side and str(side).strip().lower() not in _BUY | {s for s in _SKIP_ACTIONS}:
        # e.g. side="BUY YES" — still a buy if it starts with buy
        if not str(side).strip().lower().startswith("buy"):
            # default only when side is missing; explicit sell/hold/etc. is out
            if str(side).strip().lower() not in {"yes", "no"}:
                return None
    if not _is_buy(obj.get("side")) and obj.get("side") is not None:
        return None
    market_id = _s(_first(obj.get("market_id"), obj.get("marketId"), obj.get("market")))
    # "market" is sometimes a question string; only keep it if it looks like an id
    if market_id and not re.fullmatch(r"[0-9]+", market_id) and not obj.get("market_id"):
        market_id = None
    token_id = _s(_first(obj.get("token_id"), obj.get("tokenId"), obj.get("token"), obj.get("asset_id")))
    slug = _s(_first(obj.get("slug"), obj.get("market_slug"), obj.get("event_slug")))
    event_slug_ = _s(_first(obj.get("event_slug"), obj.get("eventSlug")))
    outcome = _s(_first(obj.get("outcome"), obj.get("outcome_name"), obj.get("selection")))
    limit = _f(_first(obj.get("limit_price"), obj.get("limit"), obj.get("entry_price"),
                     obj.get("entry"), obj.get("price"), obj.get("best_ask"), obj.get("ask"),
                     obj.get("avg_price")))
    size_shares = _f(_first(obj.get("size_shares"), obj.get("shares"), obj.get("size"),
                           obj.get("qty")))
    size_usd = _f(_first(obj.get("size_usd"), obj.get("usd"), obj.get("notional_usd")))
    if not (market_id or token_id or slug):
        return None
    if limit is None or limit <= 0 or limit >= 1.5:
        return None
    if not outcome and not token_id:
        return None
    question = _s(_first(obj.get("question"), obj.get("title"), obj.get("market_question")))
    condition_id = _s(obj.get("condition_id"))
    return dict(
        market_id=market_id, token_id=token_id, slug=slug, event_slug=event_slug_,
        outcome=outcome, limit_price=limit, size_shares=size_shares, size_usd=size_usd,
        side="buy", question=question, condition_id=condition_id,
        best_ask=_f(obj.get("best_ask") or obj.get("ask")),
    )


def _event_from(obj):
    evs = obj.get("events") if isinstance(obj.get("events"), list) else []
    event_id = event_slug_ = title = None
    if evs and isinstance(evs[0], dict):
        event_id = _s(_first(evs[0].get("event_id"), evs[0].get("id")))
        event_slug_ = _s(evs[0].get("event_slug") or evs[0].get("slug"))
        title = _s(evs[0].get("title") or evs[0].get("ticker"))
    event_id = _s(_first(event_id, obj.get("event_id"), obj.get("eventId")))
    event_slug_ = _s(_first(event_slug_, obj.get("event_slug"), obj.get("eventSlug")))
    title = _s(_first(title, obj.get("title"), obj.get("question"), obj.get("name")))
    return event_id, event_slug_, title


def _size_from_proposed(obj, n_legs):
    prop = obj.get("proposed") if isinstance(obj.get("proposed"), dict) else {}
    sets = _f(_first(prop.get("sets"), obj.get("sets"), obj.get("n_sets")))
    usd = _f(_first(prop.get("usd"), obj.get("proposed_usd")))
    return sets, usd


def normalize_entry(obj, file_meta, source_file):
    """Return a normalized proposal dict, or (None, reason)."""
    if not isinstance(obj, dict):
        return None, "not_actionable"
    action = _s(obj.get("action"))
    if action and action.lower() in _SKIP_ACTIONS:
        return None, "informational"
    if "parameter" in obj and "proposed_value" in obj:
        return None, "informational"

    generated = parse_ts(_first(obj.get("generated_at"), file_meta.get("generated_at"),
                                file_meta.get("mtime")))
    expires = parse_ts(_first(obj.get("expires_at"), file_meta.get("expires_at")))

    event_id, ev_slug, title = _event_from(obj)
    pid = _s(_first(obj.get("id"), obj.get("proposal_id"), obj.get("proposalId")))

    legs = []
    raw_legs = obj.get("legs") if isinstance(obj.get("legs"), list) else None
    if raw_legs:
        for lg in raw_legs:
            leg = _leg_from(lg) if isinstance(lg, dict) else None
            if leg is None:
                return None, "not_actionable"
            legs.append(leg)
    else:
        leg = _leg_from(obj)
        if leg is None:
            return None, "not_actionable"
        legs.append(leg)

    if not ev_slug:
        ev_slug = next((lg.get("event_slug") for lg in legs if lg.get("event_slug")), None)
    if not ev_slug:
        ev_slug = next((lg.get("slug") for lg in legs if lg.get("slug")), None)

    sets, usd = _size_from_proposed(obj, len(legs))
    if sets and not any(lg.get("size_shares") for lg in legs):
        for lg in legs:
            lg["size_shares"] = sets
    if usd and not any(lg.get("size_usd") for lg in legs):
        # usd is the group cost; per-leg usd already on arb legs
        pass

    kind = _s(_first(obj.get("type"), obj.get("kind"), obj.get("market_type")))
    is_arb = bool(
        len(legs) >= 2
        or (kind and re.search(r"arb|negrisk|basket|buy_both", kind, re.I))
    )
    h = _hash_of(ev_slug, legs)
    if not pid:
        pid = f"auto:{os.path.splitext(source_file)[0]}:{h}"

    question = _s(_first(title, obj.get("question"), legs[0].get("question")))
    return dict(
        proposal_id=pid,
        proposal_hash=h,
        source_file=source_file,
        agent=file_meta["agent"],
        generated_at=generated,
        expires_at=expires,
        event_id=event_id,
        event_slug=ev_slug,
        title=question,
        kind=kind,
        is_arb=is_arb,
        legs=legs,
        size_shares=sets or next((lg.get("size_shares") for lg in legs if lg.get("size_shares")), None),
        size_usd=usd,
    ), None


def _hash_of(ev_slug, legs):
    parts = [ev_slug or ""]
    for lg in sorted(legs, key=lambda x: (x.get("market_id") or "", x.get("token_id") or "", x.get("outcome") or "")):
        parts.append(f"{lg.get('market_id') or ''}|{lg.get('token_id') or ''}|{lg.get('outcome') or ''}|buy")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def read_proposal_files(directory=None):
    """Yield (source_file, file_meta, raw_entries, skip_reason_or_none)."""
    directory = directory or PROPOSALS_DIR
    if not os.path.isdir(directory):
        return
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json") or name.startswith("_") or name.startswith("."):
            continue
        path = os.path.join(directory, name)
        if name in SKIP_FILES:
            yield name, None, [], "informational_file"
            continue
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            yield name, None, [], "bad_json"
            continue
        if isinstance(data, list):
            meta = _file_meta({}, path)
            yield name, meta, data, None
            continue
        if not isinstance(data, dict):
            yield name, None, [], "bad_json"
            continue
        meta = _file_meta(data, path)
        entries = []
        for key in TRADE_LIST_KEYS:
            v = data.get(key)
            if isinstance(v, list):
                entries.extend(v)
        yield name, meta, entries, None


def max_age_seconds(entry):
    """Freshness window: 2 min for 15m/hourly crypto, 30 min daily crypto, else 60 min."""
    slugs = [entry.get("event_slug") or ""]
    for lg in entry.get("legs") or []:
        slugs.append(lg.get("slug") or "")
        slugs.append(lg.get("event_slug") or "")
    kinds = " ".join(filter(None, [entry.get("kind"), entry.get("title")])).lower()
    blob = " ".join(slugs).lower() + " " + kinds
    if any(is_short_crypto_updown_slug(s) for s in slugs):
        return MAX_AGE_15M_HOURLY
    if re.search(r"updown[-_]?(?:5m|15m)|-updown-1h-|updown[-_]hourly|up-or-down-hourly|"
                 r"updown_15m|updown_hourly|\b1h\b.*up.?or.?down", blob):
        return MAX_AGE_15M_HOURLY
    # Daily crypto (above-on, daily up/down, price-on-date) — not a same-day sports game.
    if re.search(r"updown[-_]daily|above[-_]daily|above-on|up-or-down-on-|price-on-date|"
                 r"what-price-will-.*-hit", blob):
        return MAX_AGE_DAILY
    if re.search(r"bitcoin|ethereum|solana|\bbtc\b|\beth\b|\bsol\b|\bxrp\b", blob) and \
            re.search(r"daily|above-on|up-or-down", blob):
        return MAX_AGE_DAILY
    return MAX_AGE_DEFAULT


def freshness_ok(entry, now=None):
    now = now or _now()
    exp = entry.get("expires_at")
    if exp is not None and now >= exp:
        return False, "expired"
    gen = entry.get("generated_at")
    if gen is None:
        return False, "stale"
    age = (now - gen).total_seconds()
    if age < -60:
        # more than a minute in the future: treat as just-issued
        age = 0
    if age > max_age_seconds(entry):
        return False, "stale"
    return True, None


def _declined_by_constants(entry):
    if entry.get("proposal_id") in DECLINED_PROPOSAL_IDS:
        return True
    if (entry.get("event_slug") or "") in DECLINED_EVENT_SLUGS:
        return True
    if (entry.get("event_id") or "") in DECLINED_EVENT_IDS:
        return True
    if entry.get("title") and DECLINED_TITLE_RE.search(entry["title"]):
        return True
    return False


def _fed_hash_from_files(directory=None):
    for name, meta, entries, skip in read_proposal_files(directory):
        if name != "arbitrage.json" or skip:
            continue
        for obj in entries:
            entry, why = normalize_entry(obj, meta, name)
            if entry and entry.get("proposal_id") == "ARB-202611160801-02":
                return entry["proposal_hash"]
            if entry and (entry.get("event_id") == "770454" or
                          (entry.get("event_slug") or "").startswith("how-many-dissent-at-the-december-fed")):
                return entry["proposal_hash"]
    return None


def seed_proposal_log(L, directory=None):
    """Ensure Fed-dissent is marked filled and Maduro/BoK are declined (idempotent)."""
    rows = [dict(r) for r in SEEDED_FILLED]
    fed_hash = _fed_hash_from_files(directory)
    if fed_hash:
        rows.append(dict(rows[0], proposal_hash=fed_hash))
    for row in rows:
        rec = dict(row)
        if not rec.get("proposal_hash"):
            rec["proposal_hash"] = hashlib.sha256(
                (rec.get("event_slug") or "").encode()
            ).hexdigest()[:16]
        if rec["status"] == "filled":
            id_hit = rec.get("proposal_id") and L.proposal_is_filled(proposal_id=rec["proposal_id"])
            hash_hit = rec.get("proposal_hash") and L.proposal_is_filled(proposal_hash=rec["proposal_hash"])
            if id_hit and hash_hit:
                continue
            if id_hit and not rec.get("proposal_hash"):
                continue
            # Record a missing hash even when the proposal id is already logged,
            # so a later rescan with a new id but the same legs is still deduped.
            if id_hit and rec.get("proposal_hash") and not hash_hit:
                L.record_proposal(**rec)
                continue
            if hash_hit:
                continue
        if rec["status"] == "declined" and L.proposal_is_declined(
            rec.get("proposal_id"), rec.get("proposal_hash"), rec.get("event_slug"), rec.get("event_id"),
        ):
            continue
        L.record_proposal(**rec)
    L.set_meta("proposal_defaults_seeded", "1")


def _lookup_market(leg):
    if leg.get("market_id") and re.fullmatch(r"[0-9]+", str(leg["market_id"])):
        try:
            m = market_by_id(leg["market_id"], tries=2)
            if m:
                return m
        except Exception:
            pass
    slug = leg.get("slug")
    if slug:
        try:
            m = market_by_slug(slug, tries=2)
            if m:
                return m
        except Exception:
            pass
    return None


def _match_outcome(name, outcomes):
    if not name or not outcomes:
        return None
    n = name.strip().lower()
    for i, o in enumerate(outcomes):
        if str(o).strip().lower() == n:
            return i
    n2 = re.sub(r"^yes:\s*", "", n).strip()
    for i, o in enumerate(outcomes):
        ol = str(o).strip().lower()
        if ol == n2 or ol == "yes" and n2 in ("yes", "y"):
            return i
    # "YES: 0" vs outcome "0" / "Yes" on a single-outcome-named market
    for i, o in enumerate(outcomes):
        ol = str(o).strip().lower()
        if n.endswith(": " + ol) or n.endswith(":" + ol):
            return i
    return None


def _resolve_leg(leg, market):
    """Attach live token/outcome from the Gamma market. Returns (leg, err)."""
    toks = parse_list(market.get("clobTokenIds"))
    outs = parse_list(market.get("outcomes"))
    token = leg.get("token_id")
    if token and token in toks:
        i = toks.index(token)
        if not leg.get("outcome") and i < len(outs):
            leg = dict(leg, outcome=outs[i])
        return dict(leg, market_id=str(market.get("id") or leg.get("market_id")),
                    token_id=token), None
    if token and toks and token not in toks:
        # Trust the explicit CLOB token even if Gamma lists drifted.
        return dict(leg, market_id=str(market.get("id") or leg.get("market_id")),
                    token_id=token), None
    i = _match_outcome(leg.get("outcome"), outs)
    if i is None and len(toks) == 2 and leg.get("outcome"):
        # Yes/No fallback
        ol = (leg.get("outcome") or "").lower()
        if ol.startswith("yes") or ol == "up":
            i = 0 if str(outs[0]).lower().startswith(("yes", "up")) else (
                0 if "yes" in str(outs[0]).lower() or "up" in str(outs[0]).lower() else 1
            )
            # prefer exact-ish
            for j, o in enumerate(outs):
                if str(o).lower() in ("yes", "up"):
                    i = j
                    break
        elif ol.startswith("no") or ol == "down":
            for j, o in enumerate(outs):
                if str(o).lower() in ("no", "down"):
                    i = j
                    break
    if i is None or i >= len(toks):
        return None, "no_market"
    return dict(leg, market_id=str(market.get("id") or leg.get("market_id")),
                token_id=toks[i],
                outcome=leg.get("outcome") or (outs[i] if i < len(outs) else None)), None


def load_proposal_signals(L, directory=None, now=None):
    """Read proposals/*.json, filter, re-check live books, return (sigs, stats, books)."""
    now = now or _now()
    seed_proposal_log(L)
    stats = dict(
        files_read=0, files_skipped=0, entries_seen=0, informational=0,
        actionable=0, accepted=0, rejected={},
        rejected_detail=[],
    )

    def reject(reason, entry=None, extra=""):
        stats["rejected"][reason] = stats["rejected"].get(reason, 0) + 1
        label = ""
        if entry:
            label = entry.get("proposal_id") or entry.get("title") or ""
            src = entry.get("source_file") or ""
            label = f"{src} {label}".strip()
        if extra:
            label = (label + " — " + extra).strip(" —")
        if label:
            stats["rejected_detail"].append(f"{reason}: {label}")
        return None

    raw_accepted = []
    for name, meta, entries, skip in read_proposal_files(directory):
        if skip:
            stats["files_skipped"] += 1
            continue
        stats["files_read"] += 1
        if not entries:
            continue
        for obj in entries:
            stats["entries_seen"] += 1
            entry, why = normalize_entry(obj, meta, name)
            if entry is None:
                if why == "informational":
                    stats["informational"] += 1
                else:
                    reject(why or "not_actionable")
                continue
            stats["actionable"] += 1
            ok, reason = freshness_ok(entry, now)
            if not ok:
                reject(reason, entry)
                continue
            if _declined_by_constants(entry) or L.proposal_is_declined(
                proposal_id=entry["proposal_id"], proposal_hash=entry["proposal_hash"],
                event_slug=entry.get("event_slug"), event_id=entry.get("event_id"),
                title=entry.get("title"),
            ):
                reject("declined", entry)
                continue
            if L.proposal_is_filled(
                proposal_id=entry["proposal_id"], proposal_hash=entry["proposal_hash"],
            ):
                reject("already_filled", entry)
                continue
            raw_accepted.append(entry)

    # Resolve markets + live books only for still-eligible proposals.
    needed_markets = []
    for entry in raw_accepted:
        for lg in entry["legs"]:
            needed_markets.append((entry, lg))

    market_cache = {}
    still = []
    for entry in raw_accepted:
        resolved, err = [], None
        markets = []
        for lg in entry["legs"]:
            key = ("id", lg.get("market_id")) if lg.get("market_id") else ("slug", lg.get("slug"))
            m = market_cache.get(key)
            if m is None and key not in market_cache:
                m = _lookup_market(lg)
                market_cache[key] = m
            if m is None:
                err = "no_market"
                break
            lg2, e2 = _resolve_leg(dict(lg), m)
            if e2 or lg2 is None:
                err = "no_market"
                break
            resolved.append(lg2)
            markets.append(m)
        if err:
            reject(err, entry)
            continue
        # Past-deadline / closed on every leg.
        dead = False
        for m in markets:
            if expired(m) or m.get("closed") or m.get("acceptingOrders") is False:
                reject("past_deadline", entry, extra=(m.get("question") or m.get("slug") or "")[:80])
                dead = True
                break
            if L.is_blocked(m.get("id"), event_slug(m) or entry.get("event_slug")):
                reject("blocked", entry)
                dead = True
                break
        if dead:
            continue
        # System-wide: no new arb / multi-leg basket resolving > ARB_MAX_DAYS out.
        if entry.get("is_arb") or len(markets) >= 2:
            if arb_resolves_too_far(markets):
                hs = [hours_left(m) for m in markets]
                hs = [h for h in hs if h is not None]
                extra = f"{max(hs)/24:.1f}d to resolution" if hs else ""
                reject("arb_resolves_gt_30d", entry, extra=extra)
                continue
        entry = dict(entry, legs=resolved, markets=markets)
        still.append(entry)

    tokens = [lg["token_id"] for e in still for lg in e["legs"] if lg.get("token_id")]
    live_books = {}
    if tokens:
        try:
            live_books = books(set(tokens))
        except Exception as e:
            for entry in still:
                reject("no_book", entry, extra=str(e)[:80])
            still = []

    sigs = []
    for entry in still:
        quotes = []
        ok = True
        for lg, m in zip(entry["legs"], entry["markets"]):
            b = live_books.get(lg["token_id"])
            if not b:
                reject("no_book", entry, extra=lg.get("outcome") or "")
                ok = False
                break
            ba, bas = best(b, "asks")
            bb, bbs = best(b, "bids")
            if ba is None:
                reject("no_book", entry, extra=lg.get("outcome") or "no ask")
                ok = False
                break
            if ba > lg["limit_price"] + PROPOSAL_SLIPPAGE:
                reject("price", entry,
                       extra=f"{lg.get('outcome')} live ask {ba:.4f} > limit {lg['limit_price']:.4f}+0.5¢")
                ok = False
                break
            rate = fee_rate(m)
            quotes.append(dict(
                token=lg["token_id"], outcome=lg.get("outcome") or "?",
                bid=bb, bid_sz=bbs, ask=ba, ask_sz=bas,
                limit_price=lg["limit_price"], fee_rate=rate, market=m,
                slug=event_slug(m) or lg.get("slug"),
                question=m.get("question") or lg.get("question"),
                market_id=str(m.get("id") or lg.get("market_id")),
                condition_id=m.get("conditionId") or lg.get("condition_id"),
            ))
        if not ok:
            continue

        rates_ok = True
        if entry["is_arb"] or len(quotes) >= 2:
            ask_sum = sum(q["ask"] for q in quotes)
            fees = sum(taker_fee(1, q["ask"], q["fee_rate"]) for q in quotes)
            edge = 1 - ask_sum - fees
            if edge < CFG["arb_min_edge"]:
                reject("no_edge", entry,
                       extra=f"ask-sum={ask_sum:.4f} fees={fees:.4f} edge={edge:.4f}")
                rates_ok = False
            entry = dict(entry, is_arb=True)
        else:
            q = quotes[0]
            edge = 1 - q["ask"] - taker_fee(1, q["ask"], q["fee_rate"])
            if edge <= 0:
                reject("no_edge", entry,
                       extra=f"ask={q['ask']:.4f} net={edge:.4f}")
                rates_ok = False
        if not rates_ok:
            continue

        m0 = entry["markets"][0]
        slug = entry.get("event_slug") or event_slug(m0)
        short = is_short_crypto_updown(m0) or any(is_short_crypto_updown(m) for m in entry["markets"])
        detail = (
            f"from proposals/{entry['source_file']} ({entry['agent']}) "
            f"id={entry['proposal_id']}"
        )
        if entry.get("is_arb"):
            ask_sum = sum(q["ask"] for q in quotes)
            fees = sum(taker_fee(1, q["ask"], q["fee_rate"]) for q in quotes)
            detail += f"; live ask-sum={ask_sum:.3f} fees/set={fees:.4f} net edge={edge:.4f}/share"
        else:
            q = quotes[0]
            detail += f"; {q['outcome']} live ask {q['ask']:.3f} ≤ limit {q['limit_price']:.3f}, net {edge:.4f} after fee"
        sig = dict(
            market=m0,
            markets=entry["markets"],
            quotes=quotes,
            fee_rate=quotes[0]["fee_rate"],
            hours_left=hours_left(m0),
            slug=slug,
            question=entry.get("title") or m0.get("question"),
            signal="PROPOSAL",
            score=max(edge, 0) * 200 + 5,  # competitive with built-in signals but processed first
            edge=edge,
            detail=detail,
            side=quotes[0],
            is_arb=bool(entry.get("is_arb")),
            proposal_id=entry["proposal_id"],
            proposal_hash=entry["proposal_hash"],
            source_file=entry["source_file"],
            agent=entry["agent"],
            size_shares=entry.get("size_shares"),
            size_usd=entry.get("size_usd"),
            short_crypto=short,
            crypto=any(is_crypto_market(m) for m in entry["markets"]),
        )
        sigs.append(sig)
        stats["accepted"] += 1

    return sigs, stats, live_books


def fmt_proposal_stats(stats):
    rej = stats.get("rejected") or {}
    parts = [f"{k}={v}" for k, v in sorted(rej.items()) if v]
    rej_s = ", ".join(parts) if parts else "none"
    lines = [
        f"- Files read: {stats.get('files_read', 0)} "
        f"(skipped informational: {stats.get('files_skipped', 0)})",
        f"- Entries seen: {stats.get('entries_seen', 0)} "
        f"(actionable {stats.get('actionable', 0)}, "
        f"informational {stats.get('informational', 0)})",
        f"- Accepted: {stats.get('accepted', 0)}",
        f"- Rejected: {rej_s}",
    ]
    for d in (stats.get("rejected_detail") or [])[:12]:
        lines.append(f"  - {d}")
    return lines
