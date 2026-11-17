"""Read-only clients for Polymarket public APIs + FX rate. No auth, no orders."""
import json, time
from concurrent.futures import ThreadPoolExecutor
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
S = requests.Session()
S.headers["User-Agent"] = "noom-paper-trader/0.1 (read-only)"

# 15-minute crypto Up/Down series use slugs like btc-updown-15m-<unix-window-start>.
CRYPTO_15M_ASSETS = ("btc", "eth", "sol", "xrp")
CRYPTO_15M_WINDOW = 900  # seconds


def _get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=25)
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def active_markets(max_pages=6, page=100):
    """Active, open markets ordered by 24h volume (desc)."""
    out = []
    for p in range(max_pages):
        batch = _get(f"{GAMMA}/markets", {"active": "true", "closed": "false", "limit": page,
                                          "offset": p * page, "order": "volume24hr", "ascending": "false"})
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
    return out


def crypto_15m_markets(assets=CRYPTO_15M_ASSETS, ahead=2):
    """Current and next N 15-minute crypto Up/Down windows, fetched by slug.

    These are usually missing from active_markets() because only the in-play BTC
    window (sometimes) ranks in the top pages by volume24hr; ETH/SOL/XRP and
    not-yet-started windows have little or no 24h volume. Closed windows are
    omitted — settlement uses market_by_id on open positions instead.
    """
    now = int(time.time())
    start = now - (now % CRYPTO_15M_WINDOW)
    slugs = [f"{a}-updown-15m-{start + CRYPTO_15M_WINDOW * i}"
             for a in assets for i in range(0, ahead + 1)]

    def one(slug):
        try:
            batch = _get(f"{GAMMA}/markets", {"slug": slug}, tries=2)
            return batch[0] if batch else None
        except Exception:
            return None

    out = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for m in pool.map(one, slugs):
            if m:
                out.append(m)
    return out


def merge_markets(*groups):
    """Concatenate market lists, first occurrence of each id wins."""
    seen, out = set(), []
    for group in groups:
        for m in group:
            mid = m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            out.append(m)
    return out


def market_by_id(mid, tries=3):
    return _get(f"{GAMMA}/markets/{mid}", tries=tries)


def market_by_slug(slug, tries=3):
    """Lookup a single market by slug. Returns None if Gamma has no match."""
    batch = _get(f"{GAMMA}/markets", {"slug": slug}, tries=tries)
    return batch[0] if batch else None


def books(token_ids, chunk=40):
    """Order books for many tokens via POST /books. Returns {token_id: book}."""
    res = {}
    ids = list(token_ids)
    for i in range(0, len(ids), chunk):
        body = [{"token_id": t} for t in ids[i:i + chunk]]
        for k in range(3):
            try:
                r = S.post(f"{CLOB}/books", json=body, timeout=30)
                r.raise_for_status()
                for b in r.json():
                    res[b["asset_id"]] = b
                break
            except Exception:
                if k == 2:
                    raise
                time.sleep(2)
    return res


def usd_thb():
    """Returns (THB per USD, source, as_of)."""
    try:
        d = _get("https://open.er-api.com/v6/latest/USD")
        return float(d["rates"]["THB"]), "open.er-api.com", d.get("time_last_update_utc")
    except Exception:
        d = _get("https://api.frankfurter.app/latest", {"from": "USD", "to": "THB"})
        return float(d["rates"]["THB"]), "frankfurter.app (ECB)", d.get("date")


def parse_list(v):
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "[]")
    except Exception:
        return []
