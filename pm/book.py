"""Order-book helpers."""


def levels(book, side):
    lv = [(float(x["price"]), float(x["size"])) for x in book.get(side, [])]
    # asks: cheapest first; bids: highest first
    return sorted(lv, key=lambda x: x[0], reverse=(side == "bids"))


def best(book, side):
    lv = levels(book, side)
    return lv[0] if lv else (None, 0.0)


def walk_asks(book, usd_budget, max_price, max_shares=None):
    """Simulate a taker BUY: consume asks up to max_price until budget is used.
    Returns (shares, avg_price, usd_spent)."""
    shares = spent = 0.0
    for px, sz in levels(book, "asks"):
        if px > max_price or spent >= usd_budget:
            break
        if max_shares is not None and shares >= max_shares:
            break
        take = min(sz, (usd_budget - spent) / px)
        if max_shares is not None:
            take = min(take, max_shares - shares)
        shares += take
        spent += take * px
    return shares, (spent / shares if shares else None), spent


def taker_fee(shares, price, rate):
    """Polymarket doc formula: fee = C * feeRate * p * (1-p) (USDC)."""
    return round(shares * rate * price * (1 - price), 5)
