"""Trading-universe selection and ranking.

Ported from the JavaScript engine (`src/scanner/scanner.js`), where the same
logic was hardened against a real incident: the old score was
``log10(volume) * (1 + range*4)``, which compressed a 2000x volume gap into
1.5x of score and let a 70% daily range erase it. BTC ($2.4B) ranked 64th,
behind coins doing $600k - which is how the bot ended up long a microcap at
10x leverage.

The rules, verbatim from the JS implementation:

  VOLUME   deepest books first; 24h movement only as a tie-breaker. The score
           is liquidity-dominated (0.75) so a thin coin with a huge range can
           never outrank BTC.
  GAINERS  ONLY coins that are actually UP on the day (chg > 0), biggest gain
           first. A flat or red coin is not a gainer and is dropped, even when
           the pool is smaller than universe_size - that ceiling must never
           pull losers into a gainers scan.
  LOSERS   the mirror: only chg < 0, biggest fall first.
  MOVERS   biggest move in EITHER direction (|chg| first).

``min_24h_volume_usd`` is applied BEFORE ranking, and it matters more in the
change-ranked modes: the biggest movers on an exchange are usually the
thinnest books, which is exactly where a stop gets swept.

Nothing here touches the network. ``build_universe`` takes the already-fetched
pair list and tickers, so the whole selection is unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: The four documented ranking modes.
UNIVERSE_RANKS = ("VOLUME", "GAINERS", "LOSERS", "MOVERS")

#: Below this many survivors the volume floor is treated as mis-set and the
#: most liquid names are used instead of returning an empty universe.
MIN_POOL = 10

#: Movement is capped at 25% before it is normalised, so one parabolic day
#: cannot dominate the score.
MOVEMENT_CAP = 0.25

#: Liquidity weight in the VOLUME score. Deliberately dominant.
LIQUIDITY_WEIGHT = 0.75
MOVEMENT_WEIGHT = 0.25


@dataclass
class UniverseItem:
    """One candidate pair, with the numbers the ranking is computed from."""

    symbol: str
    qv: float = 0.0          # 24h quote volume in USDT
    range: float = 0.0       # (high - low) / low
    price: float = 0.0
    chg: float = 0.0         # 24h change, percent, from the ticker's own open
    score: float = 0.0       # only set by the VOLUME ranking

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "qv": self.qv, "range": self.range,
                "price": self.price, "chg": self.chg, "score": self.score}


@dataclass
class UniverseResult:
    """The selected symbols plus enough context to explain the choice."""

    symbols: list[str] = field(default_factory=list)
    mode: str = "VOLUME"
    #: True when the requested mode produced nothing and VOLUME was used.
    fell_back: bool = False
    #: True when the volume floor left too few pairs and the most liquid were
    #: used instead.
    floor_relaxed: bool = False
    #: How many pairs survived the liquidity floor before ranking.
    pool_size: int = 0
    #: How many tradable USDT pairs were considered at all.
    candidates: int = 0
    #: The ranked head, for the log line / `universe` command.
    top: list[UniverseItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def report(self, limit: int = 10) -> str:
        head = ", ".join(
            f"{x.symbol} {x.chg:+.1f}%" for x in self.top[:limit])
        lines = [
            f"universe by {self.mode}: {len(self.symbols)} pairs"
            + (f" - {head}" if head else ""),
            f"candidates={self.candidates} after_volume_floor={self.pool_size}",
        ]
        lines.extend(self.notes)
        return "\n".join(lines)


def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default  # reject NaN


def rank_universe(pool: list[UniverseItem], mode: str = "VOLUME") -> list[UniverseItem]:
    """Order the pool by the requested mode. Pure, no network.

    GAINERS/LOSERS FILTER as well as sort: a coin that is flat or moving the
    other way is not a gainer/loser and is dropped. The old code sorted and
    sliced, so a ceiling larger than the qualifying set silently swept in
    every flat and red name - the literal "I set GAINERS but it isn't scanning
    gainers" report.
    """
    m = str(mode or "VOLUME").upper()

    if m == "GAINERS":
        return sorted([x for x in pool if x.chg > 0], key=lambda x: -x.chg)
    if m == "LOSERS":
        return sorted([x for x in pool if x.chg < 0], key=lambda x: x.chg)
    if m == "MOVERS":
        return sorted(pool, key=lambda x: -abs(x.chg))

    # VOLUME (default): liquidity dominates, movement is a tie-breaker.
    max_vol = max((x.qv for x in pool), default=0.0)
    scored: list[UniverseItem] = []
    for x in pool:
        liquidity = (x.qv / max_vol) if max_vol else 0.0
        movement = min(x.range, MOVEMENT_CAP) / MOVEMENT_CAP
        scored.append(UniverseItem(
            symbol=x.symbol, qv=x.qv, range=x.range, price=x.price, chg=x.chg,
            score=liquidity * LIQUIDITY_WEIGHT + movement * MOVEMENT_WEIGHT,
        ))
    return sorted(scored, key=lambda x: -x.score)


def tradable_pairs(pairs: list[dict]) -> list[dict]:
    """Pairs the exchange will actually let us trade.

    Same three conditions the JS engine uses: status OPEN, API trading
    supported, and quoted in USDT (the engine's margin coin).
    """
    out = []
    for p in pairs or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("symbolStatus", "OPEN")).upper() != "OPEN":
            continue
        if p.get("isApiSupported") is False:
            continue
        if str(p.get("quote", "USDT")).upper() != "USDT":
            continue
        if not p.get("symbol"):
            continue
        out.append(p)
    return out


def candidates_from_tickers(tickers: list[dict],
                            tradable: set[str]) -> list[UniverseItem]:
    """Turn raw tickers into ranking candidates.

    ``chg`` is measured from the ticker's own ``open`` - the same basis the
    exchange shows on its market page - not from a candle series, so a pair
    with no history still ranks correctly.
    """
    out: list[UniverseItem] = []
    for t in tickers or []:
        if not isinstance(t, dict):
            continue
        sym = t.get("symbol")
        if not sym or sym not in tradable:
            continue
        last = _num(t.get("lastPrice") or t.get("last"))
        high = _num(t.get("high"))
        low = _num(t.get("low"))
        qv = _num(t.get("quoteVol"))
        open_ = _num(t.get("open"))
        rng = ((high - low) / low) if low > 0 else 0.0
        chg = ((last - open_) / open_) * 100.0 if open_ > 0 else 0.0
        if qv <= 0 or last <= 0:
            continue
        out.append(UniverseItem(symbol=sym, qv=qv, range=rng, price=last, chg=chg))
    return out


def build_universe(pairs: list[dict], tickers: list[dict], *,
                   mode: str = "VOLUME", size: int = 40,
                   min_24h_volume_usd: float = 20_000_000.0) -> UniverseResult:
    """Select the trading universe from a pair list and a ticker snapshot.

    Mirrors ``buildUniverse()`` in the JS engine step for step:

      1. keep only OPEN / API-supported / USDT pairs
      2. build candidates, dropping zero-volume and zero-price rows
      3. apply the 24h volume floor
      4. if fewer than MIN_POOL survive, fall back to the most liquid names
      5. rank by ``mode``; if a change-ranked mode yields nothing, use VOLUME
      6. keep the first ``size``
    """
    res = UniverseResult(mode=str(mode or "VOLUME").upper())
    tradable = tradable_pairs(pairs)
    tradable_set = {p["symbol"] for p in tradable}
    cands = candidates_from_tickers(tickers, tradable_set)
    res.candidates = len(cands)

    if not cands:
        res.notes.append("no tradable USDT pair had a usable ticker")
        return res

    floor = max(0.0, _num(min_24h_volume_usd, 0.0))
    pool = [x for x in cands if x.qv >= floor]

    if len(pool) < MIN_POOL:
        keep = max(MIN_POOL, int(size or 40))
        pool = sorted(cands, key=lambda x: -x.qv)[:keep]
        res.floor_relaxed = True
        res.notes.append(
            f"min_24h_volume_usd={floor:g} left only {len(pool)} pairs - "
            f"falling back to the most liquid ones")

    res.pool_size = len(pool)
    ranked = rank_universe(pool, res.mode)

    if not ranked and res.mode in ("GAINERS", "LOSERS"):
        side = "up" if res.mode == "GAINERS" else "down"
        res.notes.append(
            f"universe_rank={res.mode}: no liquid pair is {side} today - "
            f"falling back to VOLUME ranking")
        ranked = rank_universe(pool, "VOLUME")
        res.mode = "VOLUME"
        res.fell_back = True

    n = max(1, int(size or 40))
    res.top = ranked[:n]
    res.symbols = [x.symbol for x in res.top]
    return res
