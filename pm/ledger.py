"""SQLite paper-trading ledger."""
import sqlite3, datetime as dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opened_at TEXT, market_id TEXT, condition_id TEXT, event_slug TEXT, question TEXT,
  outcome TEXT, token_id TEXT, shares REAL, avg_price REAL, fee_usd REAL, cost_usd REAL,
  signal TEXT, group_key TEXT, category TEXT, status TEXT DEFAULT 'open',
  mark_price REAL, mark_value REAL, marked_at TEXT,
  settle_price REAL, proceeds_usd REAL, pnl_usd REAL, closed_at TEXT);
CREATE TABLE IF NOT EXISTS cash_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, delta_usd REAL, reason TEXT, position_id INTEGER);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, usd_thb REAL, cash_usd REAL,
  open_value_usd REAL, equity_usd REAL, equity_thb REAL, n_open INTEGER, n_new INTEGER, n_settled INTEGER);
CREATE TABLE IF NOT EXISTS blocklist(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, market_id TEXT, event_slug TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS proposal_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, proposal_id TEXT, proposal_hash TEXT, source_file TEXT,
  event_slug TEXT, event_id TEXT, market_id TEXT,
  status TEXT, reason TEXT, group_key TEXT);
"""

_POSITION_ALTERS = (
    "ALTER TABLE positions ADD COLUMN close_reason TEXT",
    "ALTER TABLE positions ADD COLUMN close_fee_usd REAL",
)


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(positions)")}
        if "close_reason" not in cols:
            self.db.execute("ALTER TABLE positions ADD COLUMN close_reason TEXT")
        if "close_fee_usd" not in cols:
            self.db.execute("ALTER TABLE positions ADD COLUMN close_fee_usd REAL")
        self.db.executescript(
            """CREATE TABLE IF NOT EXISTS proposal_log(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts TEXT, proposal_id TEXT, proposal_hash TEXT, source_file TEXT,
                 event_slug TEXT, event_id TEXT, market_id TEXT,
                 status TEXT, reason TEXT, group_key TEXT);"""
        )
        self.db.commit()

    def meta(self, k, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
        self.db.commit()

    def init_bankroll(self, thb, rate, src, asof):
        if self.meta("initialized"):
            return False
        usd = round(thb / rate, 2)
        for k, v in dict(initialized=1, start_thb=thb, start_usd=usd, start_rate=rate,
                         rate_source=src, rate_asof=asof, started_at=now()).items():
            self.set_meta(k, v)
        self.db.execute("INSERT INTO cash_log(ts,delta_usd,reason) VALUES(?,?,?)", (now(), usd, "initial bankroll"))
        self.db.commit()
        return True

    def cash(self):
        return self.db.execute("SELECT COALESCE(SUM(delta_usd),0) FROM cash_log").fetchone()[0]

    def open_positions(self):
        return self.db.execute("SELECT * FROM positions WHERE status='open'").fetchall()

    def held_markets(self):
        return {r[0] for r in self.db.execute("SELECT market_id FROM positions WHERE status='open'")}

    def position(self, pid):
        return self.db.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()

    def open(self, **p):
        p.setdefault("opened_at", now())
        cols = ",".join(p)
        cur = self.db.execute(f"INSERT INTO positions({cols}) VALUES({','.join('?'*len(p))})", tuple(p.values()))
        pid = cur.lastrowid
        self.db.execute("INSERT INTO cash_log(ts,delta_usd,reason,position_id) VALUES(?,?,?,?)",
                        (now(), -(p["cost_usd"] + p["fee_usd"]), "open " + p["signal"], pid))
        self.db.commit()
        return pid

    def mark(self, pid, price):
        r = self.db.execute("SELECT shares FROM positions WHERE id=?", (pid,)).fetchone()
        self.db.execute("UPDATE positions SET mark_price=?, mark_value=?, marked_at=? WHERE id=?",
                        (price, None if price is None else r[0] * price, now(), pid))
        self.db.commit()

    def settle(self, pid, settle_price):
        r = self.db.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        proceeds = r["shares"] * settle_price
        pnl = proceeds - r["cost_usd"] - r["fee_usd"]
        self.db.execute("""UPDATE positions SET status='settled', settle_price=?, proceeds_usd=?, pnl_usd=?,
                           closed_at=?, mark_price=?, mark_value=?, close_reason=?, close_fee_usd=? WHERE id=?""",
                        (settle_price, proceeds, pnl, now(), settle_price, proceeds, "settle", 0.0, pid))
        self.db.execute("INSERT INTO cash_log(ts,delta_usd,reason,position_id) VALUES(?,?,?,?)",
                        (now(), proceeds, "settle", pid))
        self.db.commit()
        return pnl

    def paper_sell(self, pid, bid_price, fee_usd=0.0, reason="MANUAL_CUT"):
        """Close an open position at the best bid, charging a taker fee.

        Cash received is shares * bid minus the sell fee. Realized P&L subtracts
        both the original entry fee and the sell fee, same cost basis as settle().
        """
        r = self.db.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        if r is None:
            raise ValueError(f"position {pid} does not exist")
        if r["status"] != "open":
            raise ValueError(f"position {pid} is {r['status']}, not open")
        fee_usd = round(float(fee_usd or 0.0), 5)
        proceeds = r["shares"] * bid_price - fee_usd
        pnl = proceeds - r["cost_usd"] - r["fee_usd"]
        self.db.execute("""UPDATE positions SET status='sold', settle_price=?, proceeds_usd=?, pnl_usd=?,
                           closed_at=?, mark_price=?, mark_value=?, close_reason=?, close_fee_usd=? WHERE id=?""",
                        (bid_price, proceeds, pnl, now(), bid_price, proceeds, reason, fee_usd, pid))
        self.db.execute("INSERT INTO cash_log(ts,delta_usd,reason,position_id) VALUES(?,?,?,?)",
                        (now(), proceeds, reason, pid))
        self.db.commit()
        return pnl

    def block(self, market_id=None, event_slug=None, reason=""):
        self.db.execute("INSERT INTO blocklist(ts,market_id,event_slug,reason) VALUES(?,?,?,?)",
                        (now(), market_id, event_slug, reason))
        self.db.commit()

    def is_blocked(self, market_id=None, event_slug=None):
        if market_id and self.db.execute(
            "SELECT 1 FROM blocklist WHERE market_id=? AND market_id IS NOT NULL AND market_id!=''",
            (str(market_id),),
        ).fetchone():
            return True
        if event_slug and self.db.execute(
            "SELECT 1 FROM blocklist WHERE event_slug=? AND event_slug IS NOT NULL AND event_slug!=''",
            (event_slug,),
        ).fetchone():
            return True
        return False

    def record_proposal(self, proposal_id=None, proposal_hash=None, source_file=None,
                        event_slug=None, event_id=None, market_id=None,
                        status="", reason="", group_key=None):
        """Record a filled, declined, or rejected helper proposal (paper only)."""
        self.db.execute(
            """INSERT INTO proposal_log(ts,proposal_id,proposal_hash,source_file,
                 event_slug,event_id,market_id,status,reason,group_key)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (now(), proposal_id, proposal_hash, source_file, event_slug, event_id,
             market_id, status, reason, group_key),
        )
        self.db.commit()

    def _proposal_row(self, status, proposal_id=None, proposal_hash=None,
                      event_slug=None, event_id=None):
        if proposal_id:
            r = self.db.execute(
                "SELECT * FROM proposal_log WHERE status=? AND proposal_id=? AND proposal_id!=''",
                (status, proposal_id),
            ).fetchone()
            if r:
                return r
        if proposal_hash:
            r = self.db.execute(
                "SELECT * FROM proposal_log WHERE status=? AND proposal_hash=? AND proposal_hash!=''",
                (status, proposal_hash),
            ).fetchone()
            if r:
                return r
        if event_slug and status == "declined":
            r = self.db.execute(
                "SELECT * FROM proposal_log WHERE status=? AND event_slug=? AND event_slug!=''",
                (status, event_slug),
            ).fetchone()
            if r:
                return r
        if event_id and status == "declined":
            r = self.db.execute(
                "SELECT * FROM proposal_log WHERE status=? AND event_id=? AND event_id!=''",
                (status, event_id),
            ).fetchone()
            if r:
                return r
        return None

    def proposal_is_filled(self, proposal_id=None, proposal_hash=None, **_):
        return self._proposal_row("filled", proposal_id, proposal_hash) is not None

    def proposal_is_declined(self, proposal_id=None, proposal_hash=None,
                             event_slug=None, event_id=None, title=None):
        return self._proposal_row(
            "declined", proposal_id, proposal_hash, event_slug, event_id,
        ) is not None

    def log_run(self, **r):
        self.db.execute(f"INSERT INTO runs({','.join(r)}) VALUES({','.join('?'*len(r))})", tuple(r.values()))
        self.db.commit()
