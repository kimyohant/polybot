# Polymarket paper trader (Noom)

**Paper trading only.** Uses Polymarket's public, read-only APIs. No wallet, no keys, no real orders.

## Rerun
```
cd /workspace/polymarket && python3 run.py
```
Options:
- `--no-trade` — scan, mark-to-market, settle, and auto-close only (no new opens)
- `--sell ID,ID --sell-reason MANUAL_CUT` — paper-sell those open positions at the live CLOB best bid, charge the taker fee, and add the market id + event slug to the blocklist
- `--db PATH`, `--bankroll-thb N` (only used the first time a ledger is created)

A 1-minute box loop (`loop.sh`) runs `run.py` under `flock /tmp/pm_run.lock` with a 55s timeout. One-off ledger edits must take that lock and must not kill the loop.

Each run does:
1. **Settle** open positions whose market has closed and resolved (pays $1 / $0 / $0.50 per share from Gamma `outcomePrices`, no extra fee).
2. **Auto-close** any remaining open position whose best bid is ≥ 0.99 **and** whose game/market has ended (`endDate` or `gameStartTime` passed, or `closed` / not `acceptingOrders`). Paper-sold at the best bid, taker fee charged, reason `AUTO_CLOSE_099`. Official settlement is preferred when the market is already resolved.
3. **Mark to market** the rest at the current CLOB best bid (what you'd get selling now).
4. **Scan** active binary markets from Gamma (`/markets`, by 24h volume, liquidity ≥ $10k, 24h volume ≥ $5k), plus the current and next two 15-minute crypto Up/Down windows for BTC/ETH/SOL/XRP (slugs like `btc-updown-15m-<unix>`), which rarely rank in the volume pages and use a lower liquidity floor ($1k) with no 24h-volume requirement. Skips markets that are `closed`, not `acceptingOrders`, have a past `endDate`/`endDateIso`, or whose question/title names a date that is already past (15m/5m crypto Up/Down windows trust `endDate` only, because the title's ET calendar date can already be yesterday in UTC). Blocklisted markets/events are also skipped. Then pull live order books from CLOB (`POST /books`).
5. **Signals** (`pm/signals.py`):
   - `ARB_BUY_BOTH` – best ask YES + best ask NO + taker fees < $1 (locked profit), and the market resolves within `ARB_MAX_DAYS` (30) days. Traded.
   - `NEAR_RES_FAVOURITE` – ≤144h to the later of Gamma `endDate` and any calendar date found in the question/description (long-stop “if not known by …” clauses are stripped), and favourite's ask in 0.85–0.97. Traded.
   - `MOMENTUM_24H` – |24h price change| ≥ 5 pts (was 10), follow the move if the ask is 0.15–0.85. Traded, smaller size.
   - `PROPOSAL` – an actionable buy from `proposals/*.json` that is still fresh, passes a live CLOB price check, and still has positive edge after the taker fee. Traded (5% of equity, same caps as other fills). Helper-proposal fills are attempted first in a run, before built-in signals.
   - `INFO_WIDE_SPREAD` (spread ≥ 5¢), `INFO_BID_SUM_GT_1` – shown only, not traded.
6. **Paper fills** at the live ask (walking the book, ≤1¢ slippage; helper proposals allow at most +0.5¢ over their limit price). Fees come from each market's `feeSchedule.rate` using Polymarket's formula `fee = shares × rate × p × (1−p)`. Paper-sells use the same formula on the bid.
7. **Report** goes to `reports/snapshot_YYYYMMDD_HHMM.md` and `reports/latest.md`. Ledger is `ledger.sqlite` (tables `positions`, `cash_log`, `runs`, `meta`, `blocklist`, `proposal_log`).

## Risk rules (`RISK` in run.py)
- Max 5% of equity per position (2% for momentum).
- One position per event. Max 15 new positions per run. No per-category cap.
- Split exposure: general positions (everything except 15m/5m crypto Up/Down) at most 40% of equity in play; crypto 15-minute Up/Down markets (`is_short_crypto_updown`) have their own 10% of equity bucket.
- No new *directional* crypto in the general bucket (category/feeType crypto such as BTC/ETH above/hit/daily up-down, excluding the 15m bucket) while marked directional crypto (general crypto + 15m) is ≥ 15% of equity. The 15m bucket is not blocked by this rule. `ARB_BUY_BOTH` is not directional and is exempt.
- Never enter a market whose deadline has passed: reject if `endDate` has passed, or the title's explicit deadline date has passed (15m/5m exemption above), or the market is closed / not accepting orders. This is checked in `eligible()`, in `compute()` on every signal path (including momentum), and again in `open_positions()` before a fill.
- No new arbitrage (`ARB_BUY_BOTH`, helper-proposal arbs from `proposals/arbitrage.json`, or any multi-leg basket) whose resolution is more than `ARB_MAX_DAYS` (30) days away. The date is `resolution_end()` — the later of Gamma `endDate` and calendar dates in the question/description — the same logic as `NEAR_RES_FAVOURITE`. For a basket, the latest leg date is used. Rejects are logged with reason `arb_resolves_gt_30d`. Existing open arbs (the December Fed dissent basket, group `arb:770454`, resolves ~10 Dec) are left in place.
- Manual cuts can paper-sell an open position at the best bid (`Ledger.paper_sell`, reason e.g. `MANUAL_CUT`) and add that market id and event slug to `blocklist` so they cannot be re-entered.

## Helper proposals (`proposals/*.json`)

The 1-minute loop reads helper-agent JSON from `proposals/` (`crypto`, `sports`, `politics`, `arbitrage`; `risk.json` and `analytics.json` are informational and skipped). Formats may differ; the loader (`pm/proposals.py`) is tolerant, but helpers should use the schema below so entries are not dropped. Everything is treated as paper, even if `paper_only` is missing — the bot never places a real order.

### Schema
```json
{
  "generated_at": "2026-11-16T08:00:00+07:00",
  "expires_at": "2026-11-16T08:30:00+07:00",
  "agent": "PM-Crypto",
  "paper_only": true,
  "proposals": [
    {
      "id": "CRY-202611160800-01",
      "generated_at": "2026-11-16T08:00:00+07:00",
      "expires_at": "2026-11-16T08:02:00+07:00",
      "side": "buy",
      "market_id": "7054389",
      "token_id": "<clob token id>",
      "slug": "btc-updown-15m-1794790800",
      "event_slug": "btc-updown-15m-1794790800",
      "outcome": "Down",
      "limit_price": 0.42,
      "size_shares": 10.0,
      "size_usd": 4.20,
      "question": "Bitcoin Up or Down - November 15, 8:00PM-8:15PM ET",
      "reason": "optional note"
    }
  ]
}
```

A multi-leg arbitrage (buy every outcome in one event so payout is $1/set) uses a `legs` array instead of a single market/token. Politics files may use `new_proposals` as the list key. Required on each buy (or each leg): a market id **or** token id **or** slug, an `outcome`, `side` of `buy`, and a limit/entry price (`limit_price`, `entry_price`, or `ask`). `size_shares` / `size_usd` are optional caps (risk rules still apply). Entries without those fields, or with actions such as hold / cut / sell / parameter-change, are ignored.

Timestamps may be on the entry or the file. Naive values are treated as Asia/Bangkok (UTC+7). Rejected if `expires_at` has passed, or if `generated_at` is older than: **2 minutes** for 15-minute and hourly crypto markets, **30 minutes** for daily crypto markets (above-on, daily up/down, price-on-date), **60 minutes** otherwise.

A proposal is filled only if the live CLOB ask is ≤ the limit plus 0.5¢ and the edge after the taker fee is still positive. Multi-leg arbs are re-checked as a group: `sum(asks) + fees < $1` per set with the same 0.5¢ min-edge as `ARB_BUY_BOTH`, equal shares on every leg, and all legs fill together or not at all. A new arb or multi-leg basket is also rejected (`arb_resolves_gt_30d`) if `resolution_end()` is more than `ARB_MAX_DAYS` (30) days away.

The same risk rules as built-in signals apply: no past-deadline markets, blocklist, one position per event, 40% general / 10% 15-minute caps, no new general directional crypto while directional crypto is ≥ 15% of equity (15-minute bucket and arb groups are exempt), 5% of equity per proposal, `max_new_per_run`. Already-filled proposals are recorded in `proposal_log` and will not be entered again. User-declined proposals (Maduro Prison Time, Bank of Korea January) are on a declined list and will not be entered. The December Fed dissent arb (`ARB-202611160801-02`, group `arb:770454`) is already filled and will not be doubled.

## Bankroll
10,000 THB, converted to USD once at start with the live rate from open.er-api.com (stored in `meta`). Equity in THB is reported at each run's current rate.

## Caveats
The signals are unproven heuristics. A 0.90 favourite loses about 1 time in 10, and a single loss wipes out about 9 wins. Fills are simulated against a snapshot of the book: there's no queue, latency, or market impact. Paper-sells assume the full size fills at the current best bid (the book is not walked down the bid side). Crypto 15-minute Up/Down markets charge a 7% taker fee (`fee = shares × 0.07 × p × (1−p)`), often have a few cents of spread, and can move to an extreme in the last minutes — the leftover edge after fees and spread is thin.

Gamma `endDate` can disagree with the real resolution date (e.g. the Ljubljana mayoral market had `endDate` on election day while the description scheduled a runoff for 29 Nov). `NEAR_RES_FAVOURITE` therefore uses the later of `endDate` and dates parsed from the question/description, after stripping common long-stop wording such as “if the result isn't known by … resolve to Other”. This is a cheap text parse: it can miss a later date that is not written as a calendar date, and unusual long-stop wording could slip through. Position #20 (Ljubljana) was opened under the old endDate-only rule and is left in place until it is sold or settles.

For many sports markets Gamma sets `endDate` equal to `gameStartTime` (tip-off). `AUTO_CLOSE_099` can therefore fire after the game has started once the bid is ≥ 0.99, not only after the final buzzer.

Position #9 (US x Iran Effective Ceasefire by November 8, Yes, `MOMENTUM_24H`) was opened on 15 Nov 2026, after both the 8 Nov `endDate` and the date in the title, because Gamma still marked the market active and the scanner did not yet reject past deadlines. The same first-day window also let #8 (Andersson, `endDate` 14 Sep 2026) in. The deadline filter now applies to every signal path.

Real trading would add USDC on-ramp and FX costs. Polymarket lists Thailand (TH) as close-only. See https://docs.polymarket.com/developers/CLOB/geoblock
