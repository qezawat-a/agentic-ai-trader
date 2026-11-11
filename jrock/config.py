"""Configuration loading for J+Rock."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .thinking import level_from_env

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*a, **k):
        return False

ROOT = Path(__file__).resolve().parent.parent

#: One message per `.env` line python-dotenv refused to parse, from the last
#: `_load_env`. `scripts/check_env.py` reads the file itself; this is kept so a
#: diagnostic (or a test) can see what the loader saw without loading twice.
ENV_PARSE_WARNINGS: list[str] = []


class _WarningCollector(logging.Handler):
    """Hold python-dotenv's parse warnings instead of printing them.

    It emits ONE warning per unparseable line, so a `.env` carrying a pasted
    block (JSON, YAML, a multi-line value) prints dozens of identical lines at
    every start-up and buries the real output. Collecting them lets us print a
    single actionable message naming the line numbers.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102 - Handler API
        self.messages.append(record.getMessage())


def _line_list(numbers: list[int], limit: int = 6) -> str:
    """'267, 276, 280, ... (+18 more)' - enough to find the block."""
    uniq = sorted(set(numbers))
    shown = ", ".join(str(n) for n in uniq[:limit])
    if len(uniq) > limit:
        shown += f", ... (+{len(uniq) - limit} more)"
    return shown


def _load_env(env_path: Path) -> None:
    """Load `.env`, reporting parse failures as ONE line instead of a wall.

    python-dotenv keeps the lines it understands and drops the rest, so a bad
    line is not just noise: the setting it carried is silently missing. The
    message therefore says how many lines were dropped and where they start.
    """
    ENV_PARSE_WARNINGS.clear()
    logger = logging.getLogger("dotenv.main")
    collector = _WarningCollector()
    level, propagate = logger.level, logger.propagate
    logger.addHandler(collector)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        load_dotenv(env_path, override=False)
    finally:
        logger.removeHandler(collector)
        logger.setLevel(level)
        logger.propagate = propagate

    bad = [m for m in collector.messages if "could not parse" in m]
    if not bad:
        return
    ENV_PARSE_WARNINGS.extend(bad)
    numbers = []
    for m in bad:
        hit = re.search(r"line (\d+)", m)
        if hit:
            numbers.append(int(hit.group(1)))
    where = f" at line{'s' if len(numbers) != 1 else ''} {_line_list(numbers)}" if numbers else ""
    logging.getLogger("jrock.config").warning(
        "%s: %d line%s could not be parsed%s and were ignored. A value that "
        "spans lines must be quoted (KEY=\"line1\\nline2\"); a pasted block must "
        "be removed. Run `python scripts/check_env.py` to list them.",
        env_path.name, len(bad), "" if len(bad) == 1 else "s", where)


def _b(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    bitunix_api_key: str = ""
    bitunix_api_secret: str = ""
    bitunix_base_url: str = "https://fapi.bitunix.com"
    bitunix_ws_public: str = "wss://fapi.bitunix.com/public/"
    bitunix_ws_private: str = "wss://fapi.bitunix.com/private/"
    #: Postgres DSN for the durable state. Empty -> the file store
    #: (data/state.json). Set -> the database IS the state, so a redeploy that
    #: recreates the checkout cannot lose it.
    database_url: str = ""
    auto_accept_edit: bool = False
    full_device_access: bool = False
    block_destructive: bool = True
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT"])
    leverage: int = 10
    margin_mode: str = "ISOLATION"
    position_mode: str = "ONE_WAY"
    max_open_positions: int = 3
    risk_pct: float = 1.0
    breakeven_pct: float = 0.6
    trailing_trigger_roi_pct: float = 25.0
    liq_sl_distance_pct: float = 50.0
    min_agreeing: int = 3
    timeframes: list[str] = field(default_factory=lambda: ["1m", "3m", "5m", "15m"])
    min_confidence: float = 70.0
    tf_min_confidence: float = 60.0
    scan_interval: int = 30
    report_interval: int = 30
    thinking_level: str = "medium"
    # rest | ws. Defaults to rest: the public WS layer is live-verified, but the
    # private (position/order/balance) login handshake has never been verified
    # against the real server, so we do not silently switch the engine onto it.
    market_data_source: str = "rest"
    #: When the configured leverage leaves no room for a stop-loss between the
    #: liquidation price and the mark price, clamp the leverage for that order
    #: to the highest value that still fits instead of refusing the trade.
    #:
    #: A stop must sit strictly between the liquidation price and the mark. The
    #: liquidation price is derived from the ENTRY, and a MARKET order fills at
    #: the last price, which can be on the far side of the mark. At 100x with a
    #: 0.5% maintenance margin the liquidation price is only ~0.5% below the
    #: entry, so a mark 1.3% below the last price puts the liquidation price
    #: ABOVE the mark - no stop can exist, and the engine refused the symbol on
    #: every scan forever. With this on, the order is placed at the highest
    #: leverage that still leaves room for a stop.
    leverage_auto_reduce: bool = True
    #: Which unit the engine sizes an order in, per the official Bitunix help
    #: article "Explanation of the Order Units in Futures Trading":
    #:   QUANTITY - base-coin quantity (what place_order's `qty` field takes;
    #:              the engine's historical behaviour)
    #:   NOTIONAL - position value in USDT; qty = notional / price
    #:   COST     - margin paid in USDT; qty = cost * leverage / price
    #: The wire format is always `qty` (base coin) - the unit only changes how
    #: the size is specified and converted.
    order_unit: str = "QUANTITY"
    #: Order-size bounds, in USDT. 0 disables the bound. These are the engine's
    #: OWN limits, checked before the request is sent; the exchange has its own
    #: (minTradeVolume / maxMarketOrderVolume) which are enforced separately.
    min_margin_usdt: float = 0.0
    max_margin_usdt: float = 0.0
    max_notional_usdt: float = 0.0
    #: How much of the SPENDABLE balance one position may lock as margin, in
    #: percent. This is the user-facing size knob: "use at most this share of my
    #: available balance per trade". 10 = a single position may lock 10% of the
    #: available balance as margin, so ten positions could be open at once.
    #:
    #: It is a MARGIN budget, not a notional budget, because margin is what the
    #: exchange actually deducts from `available` - so the number set here is
    #: the number the user sees leave their balance. The notional it implies is
    #: `margin * leverage`, minus the taker fee.
    #:
    #: It composes with the balance cap by taking the MINIMUM of the two
    #: notionals, so it can only ever make an order smaller; it can never talk
    #: the engine into an order the balance cannot pay for. 0 disables it and
    #: restores the pre-existing behaviour (size purely by risk_pct, capped by
    #: the whole spendable balance).
    margin_amount_pct: float = 10.0
    #: Taker fee rate charged on a MARKET order, as a fraction of notional.
    #: Bitunix USDT-M futures VIP 0 is 0.0006 (0.06%). The engine must reserve
    #: this on top of the initial margin, because the exchange deducts both from
    #: the available balance at once - sizing to margin == balance is what
    #: produced 20003 Insufficient balance.
    taker_fee_rate: float = 0.0006
    #: Extra headroom reserved when sizing, as a percent of available balance.
    #: Covers the price move between sizing and the request landing. 0 disables.
    margin_safety_pct: float = 2.0

    # ---------------------------------------------------------------- TP/SL methods
    #: Which of the two NATIVE Bitunix TP/SL methods the engine uses at entry.
    #: The other two documented methods are always armed and need no selector:
    #:   TRAILING - the stop moves to breakeven at `TRADE_BREAKEVEN_PCT` ROI and
    #:              then trails behind price from `TRADE_TRAILING_TRIGGER_ROI_PCT`
    #:              ROI (software ratchet, runs in the manage loop)
    #:   ACCOUNT  - closes ALL positions when total unrealized PnL crosses
    #:              ACCOUNT_TP_USDT / ACCOUNT_SL_USDT (software, manage loop)
    #: Only POSITION and PARTIAL exist as real endpoints, so only they are a
    #: choice:
    #:   POSITION - one TP/SL closes the ENTIRE position (tpsl/position/place_order)
    #:   PARTIAL  - scale out in stages: every rung of PARTIAL_TP_LADDER is its
    #:              own native tpsl/place_order with tpQty, parked at entry.
    tpsl_method: str = "POSITION"
    #: PARTIAL ladder, "share@R" pairs: close share% of the position once price
    #: runs R x the entry-to-stop distance. Example: "40@1,35@2,25@3" closes
    #: 40% at 1R, 35% at 2R, 25% at 3R; the remainder rides the trailing stop.
    partial_tp_ladder: str = "40@1,35@2,25@3"
    #: ACCOUNT: close every position when total unrealized PnL reaches these
    #: USDT thresholds. 0 disables that side. Both 0 = account TP/SL is off.
    account_tp_usdt: float = 0.0
    account_sl_usdt: float = 0.0

    # ---------------------------------------------------------------- self-repair
    #: How often the self-repair loop runs (hours). 0 keeps the loop OFF; the
    #: on-demand command still works.
    self_heal_interval_hours: float = 6.0
    #: Let the self-repair loop commit and push its own fixes to origin main
    #: once the tests are green. 0 keeps it report-only (fixes stay uncommitted).
    self_heal_auto_push: bool = True

    @property
    def data_dir(self) -> Path:
        p = ROOT / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    env_path = Path(env_file) if env_file else ROOT / ".env"
    if env_path.exists():
        _load_env(env_path)
    else:
        load_dotenv(override=False)

    tfs = [t.strip() for t in os.getenv("TRADE_TIMEFRAMES", "1m,3m,5m,15m").split(",") if t.strip()]
    syms = [s.strip().upper() for s in os.getenv("TRADE_SYMBOLS", "BTCUSDT").split(",") if s.strip()]

    return Settings(
        base_url=os.getenv("BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        api_key=os.getenv("API_KEY", ""),
        model=os.getenv("MODEL", ""),
        bitunix_api_key=os.getenv("BITUNIX_API_KEY", ""),
        bitunix_api_secret=os.getenv("BITUNIX_API_SECRET", ""),
        bitunix_base_url=os.getenv("BITUNIX_BASE_URL", "https://fapi.bitunix.com").rstrip("/"),
        bitunix_ws_public=os.getenv("BITUNIX_WS_PUBLIC", "wss://fapi.bitunix.com/public/"),
        bitunix_ws_private=os.getenv("BITUNIX_WS_PRIVATE", "wss://fapi.bitunix.com/private/"),
        auto_accept_edit=_b("AUTO_ACCEPT_EDIT", False),
        full_device_access=_b("FULL_DEVICE_ACCESS", False),
        block_destructive=_b("BLOCK_DESTRUCTIVE", True),
        symbols=syms or ["BTCUSDT"],
        leverage=_i("TRADE_LEVERAGE", 10),
        margin_mode=os.getenv("TRADE_MARGIN_MODE", "ISOLATION").upper(),
        position_mode=os.getenv("TRADE_POSITION_MODE", "ONE_WAY").upper(),
        max_open_positions=_i("TRADE_MAX_OPEN_POSITIONS", 3),
        risk_pct=_f("TRADE_RISK_PCT", 1.0),
        breakeven_pct=_f("TRADE_BREAKEVEN_PCT", 0.6),
        trailing_trigger_roi_pct=_f("TRADE_TRAILING_TRIGGER_ROI_PCT", 25.0),
        liq_sl_distance_pct=_f("TRADE_LIQ_SL_DISTANCE_PCT", 50.0),
        min_agreeing=_i("TRADE_MIN_AGREEING", 3),
        timeframes=tfs or ["1m", "3m", "5m", "15m"],
        min_confidence=_f("TRADE_MIN_CONFIDENCE", 70.0),
        tf_min_confidence=_f("TRADE_TF_MIN_CONFIDENCE", 60.0),
        scan_interval=_i("TRADE_SCAN_INTERVAL", 30),
        report_interval=_i("TRADE_REPORT_INTERVAL", 30),
        thinking_level=level_from_env(),
        market_data_source=_market_data_source(),
        leverage_auto_reduce=_b("LEVERAGE_AUTO_REDUCE", True),
        order_unit=_order_unit(),
        min_margin_usdt=_f("MIN_MARGIN_USDT", 0.0),
        max_margin_usdt=_f("MAX_MARGIN_USDT", 0.0),
        max_notional_usdt=_f("MAX_NOTIONAL_USDT", 0.0),
        margin_amount_pct=_f("MARGIN_AMOUNT_PCT", 10.0),
        taker_fee_rate=_f("TAKER_FEE_RATE", 0.0006),
        margin_safety_pct=_f("MARGIN_SAFETY_PCT", 2.0),
        tpsl_method=_tpsl_method(),
        partial_tp_ladder=os.getenv("PARTIAL_TP_LADDER", "40@1,35@2,25@3").strip(),
        account_tp_usdt=_f("ACCOUNT_TP_USDT", 0.0),
        account_sl_usdt=_f("ACCOUNT_SL_USDT", 0.0),
        self_heal_interval_hours=_f("SELF_HEAL_INTERVAL_HOURS", 6.0),
        self_heal_auto_push=_b("SELF_HEAL_AUTO_PUSH", True),
        database_url=(os.getenv("DATABASE_URL") or "").strip(),
    )


def _tpsl_method() -> str:
    """TPSL_METHOD=POSITION|PARTIAL, default POSITION.

    Trailing and Account TP/SL are always armed (they need no native endpoint
    and no selector), so the only real choice is which NATIVE exit shape a new
    position gets. Anything unrecognised falls back to POSITION.
    """
    v = (os.getenv("TPSL_METHOD") or "POSITION").strip().upper()
    return v if v in ("POSITION", "PARTIAL") else "POSITION"


def _order_unit() -> str:
    """ORDER_UNIT=QUANTITY|NOTIONAL|COST, default QUANTITY.

    QUANTITY is what place_order's `qty` field takes and what the engine has
    always used, so an unset or unrecognised value keeps it.
    """
    v = (os.getenv("ORDER_UNIT") or "QUANTITY").strip().upper()
    return v if v in ("QUANTITY", "NOTIONAL", "COST") else "QUANTITY"


def _market_data_source() -> str:
    """MARKET_DATA_SOURCE=rest|ws, default rest.

    Anything unrecognised falls back to rest rather than guessing.
    """
    v = (os.getenv("MARKET_DATA_SOURCE") or "rest").strip().lower()
    return v if v in ("rest", "ws") else "rest"
