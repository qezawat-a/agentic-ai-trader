"""Reproduce: breakeven + trailing never fire on a live position.

Run:  python scripts/repro_breakeven.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TRADE_SYMBOLS", "AKEUSDT")

from jrock.config import Settings                        # noqa: E402
from jrock.trader.engine import TraderEngine             # noqa: E402
from jrock.trader.risk import PairRules                  # noqa: E402

PAIR_ROW = {
    "symbol": "AKEUSDT", "minTradeVolume": "1",
    "maxMarketOrderVolume": "100000000", "maxLimitOrderVolume": "1500000000",
    "basePrecision": 0, "quotePrecision": 6, "maxLeverage": 50, "minLeverage": 1,
    "defaultLeverage": 10, "priceProtectScope": "0.05", "symbolStatus": "OPEN",
    "isApiSupported": True,
}

CANDLES = [{"close": 0.005943, "open": 0.005943, "high": 0.00595,
            "low": 0.00594, "time": 1_700_000_000 + i * 60}
           for i in range(60)]


class FakeClient:
    """Fake client that reports a LONG position at the given ROI."""

    def __init__(self, entry: float, price: float, leverage: int = 10,
                 position_mode: str = "HEDGE", qty: str = "15656"):
        self.entry, self.price = entry, price
        self.leverage, self.qty = leverage, qty
        self.position_mode = position_mode
        self.modified: list[dict] = []

    async def get_tickers(self, sym):
        return [{"symbol": sym, "lastPrice": str(self.price),
                 "markPrice": str(self.price)}]

    async def get_pending_positions(self, sym=None):
        return [{"positionId": "P1", "symbol": "AKEUSDT", "side": "LONG",
                 "avgOpenPrice": str(self.entry), "qty": self.qty,
                 "leverage": str(self.leverage),
                 "liqPrice": str(self.entry * 0.9),
                 "markPrice": str(self.price)}]

    async def get_trading_pairs(self, symbols=None):
        return [PAIR_ROW]

    async def modify_position_tp_sl_order(self, sym, pid, tp_price=None,
                                          sl_price=None, tp_stop_type=None,
                                          sl_stop_type=None):
        self.modified.append({"symbol": sym, "positionId": pid,
                              "tpPrice": tp_price, "slPrice": sl_price,
                              "tpStopType": tp_stop_type,
                              "slStopType": sl_stop_type})
        return {"orderId": "T1"}

    async def get_kline(self, *a, **k):
        return CANDLES


def run(entry: float, price: float, *, breakeven_pct: float = 0.6,
        trail_roi: float = 25.0, label: str = ""):
    s = Settings()
    s.market_data_source = "rest"
    s.breakeven_pct = breakeven_pct
    s.trailing_trigger_roi_pct = trail_roi
    tmp = tempfile.mkdtemp()
    e = TraderEngine(s, data_dir=Path(tmp))
    e.client = FakeClient(entry, price)

    async def go():
        await e.load_rules(["AKEUSDT"])
        return await e.manage_positions()

    actions = asyncio.run(go())
    roi = (price - entry) / entry * 10 * 100
    print(f"\n--- {label or ''} entry={entry} price={price} "
          f"roi={roi:.2f}% lev=10 ---")
    print(f"  actions : {[a['action'] for a in actions]}")
    print(f"  modify  : {len(e.client.modified)} call(s)")
    for m in e.client.modified:
        print(f"           sl={m['slPrice']} tp={m['tpPrice']}")
    print(f"  errors  : {e.state.errors}")
    print(f"  be_done : {[ (st.breakeven_done, st.trailing_active) for st in e.positions.values() ]}")
    return actions, e


if __name__ == "__main__":
    # ROI at 10x: 0.1% move = 1% ROI.
    # breakeven fires at 0.6% ROI -> 0.06% price move.
    run(0.005943, 0.0059465, label="breakeven should fire (0.6% ROI)")
    print("\n" + "=" * 70)
    # trailing fires at 25% ROI -> 2.5% price move
    run(0.005943, 0.006100, label="trailing should fire (27% ROI)")
