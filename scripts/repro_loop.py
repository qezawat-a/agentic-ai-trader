"""Run the REAL manage loop (_manage_loop) and see if breakeven/trailing fire."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jrock.config import Settings                      # noqa: E402
from jrock.trader.engine import TraderEngine           # noqa: E402

from repro_breakeven import CANDLES, PAIR_ROW          # noqa: E402


class FakeClient:
    """LONG at entry, currently at `price`. Counts modify calls."""

    def __init__(self, entry: float, price: float):
        self.entry, self.price = entry, price
        self.modified: list[dict] = []
        self.pos_calls = 0

    async def start(self): ...
    async def close(self): ...

    async def get_tickers(self, sym):
        return [{"symbol": sym, "lastPrice": str(self.price),
                 "markPrice": str(self.price)}]

    async def get_pending_positions(self, sym=None):
        self.pos_calls += 1
        return [{"positionId": "P1", "symbol": "AKEUSDT", "side": "LONG",
                 "avgOpenPrice": str(self.entry), "qty": "15656",
                 "leverage": "10", "liqPrice": str(self.entry * 0.9),
                 "markPrice": str(self.price)}]

    async def get_trading_pairs(self, symbols=None):
        return [PAIR_ROW]

    async def modify_position_tp_sl_order(self, sym, pid, tp_price=None,
                                          sl_price=None, tp_stop_type=None,
                                          sl_stop_type=None):
        self.modified.append({"slPrice": sl_price})
        return {"orderId": "T1"}

    async def get_kline(self, *a, **k):
        return CANDLES


async def main():
    s = Settings()
    s.market_data_source = "rest"
    s.breakeven_pct = 0.6
    s.trailing_trigger_roi_pct = 25.0
    s.scan_interval = 5

    import tempfile
    e = TraderEngine(s, data_dir=Path(tempfile.mkdtemp()))
    e.client = FakeClient(0.005943, 0.006100)   # 26.4% ROI -> both should fire
    e.state.symbols = ["AKEUSDT"]
    e.state.scan_on = False   # keep the scanner out of this test
    await e.load_rules(["AKEUSDT"])

    print("startup state.engine_on =", e.state.engine_on)
    msg = await e.engine_on()
    print("engine_on() ->", msg)
    print("tasks after engine_on:", [(t.get_name(), t.done()) for t in e._tasks])

    await asyncio.sleep(2)   # let the manage loop take its first pass

    print("pos_calls   :", e.client.pos_calls)
    print("modify calls:", len(e.client.modified))
    for m in e.client.modified:
        print("   ", m)
    print("errors      :", e.state.errors[-5:])
    print("positions   :", {k: (v.breakeven_done, v.trailing_active)
                            for k, v in e.positions.items()})

    await e.engine_off()


asyncio.run(main())
