"""Bitunix Futures — Python reference client (no third-party deps).

Run: python3 bitunix_client.py
Mirrors the exact signing in jrock/trader/client.py.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

BASE_URL = "https://fapi.bitunix.com"


def sign_query_string(params: dict | None) -> str:
    """sorted key+value concatenation, no '=' and no '&', empty skipped."""
    if not params:
        return ""
    parts = []
    for k in sorted(params):
        v = params[k]
        if v is None or v == "":
            continue
        parts.append(f"{k}{v}")
    return "".join(parts)


def sign(api_key: str, secret: str, nonce: str, timestamp: str,
         query: str = "", body: str = "") -> str:
    """sign = sha256(sha256(nonce + timestamp + apiKey + query + body) + secret)."""
    message = f"{nonce}{timestamp}{api_key}{query}{body}"
    digest = hashlib.sha256(message.encode()).hexdigest()
    return hashlib.sha256((digest + secret).encode()).hexdigest()


def headers(api_key: str, secret: str, params: dict | None = None,
            body: dict | None = None) -> dict:
    nonce = str(uuid.uuid4())
    timestamp = str(int(time.time() * 1000))  # milliseconds
    qs = sign_query_string(params)
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")) if body else ""
    return {
        "api-key": api_key,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": sign(api_key, secret, nonce, timestamp, qs, payload),
        "language": "en-US",
        "Content-Type": "application/json",
    }


# --- self-check against the official test vector ----------------------------
def _self_check() -> None:
    vec = dict(
        api_key="11111111111111111",
        secret="22222222222222222",
        nonce="33333333333333333",
        timestamp="1747104711262",
        want="40e87256554e263e39201a3979469b7fbb640d1c7c41759abf5c6a18f937f922",
    )
    got = sign(vec["api_key"], vec["secret"], vec["nonce"],
               vec["timestamp"], "marginCoinUSDT", "")
    assert got == vec["want"], f"sign vector FAIL: {got}"
    wrong = sign(vec["api_key"], vec["secret"], vec["nonce"],
                 vec["timestamp"], "marginCoin=USDT", "")
    assert wrong != vec["want"], "urlencoded-guard FAIL"
    assert sign_query_string({"marginCoin": "USDT"}) == "marginCoinUSDT"
    assert sign_query_string({"b": "2", "a": "1"}) == "a1b2"  # sorted
    print("PASS sign vector / urlencoded-guard / sort")


if __name__ == "__main__":
    _self_check()