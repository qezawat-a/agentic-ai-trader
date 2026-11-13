// Bitunix Futures — Node (ESM) reference client.
// Double-SHA256 signing. Run: node index.js
import { createHash, randomUUID } from "node:crypto";

const BASE = "https://fapi.bitunix.com";

// sorted key+value concatenation, no '=' and no '&', empty skipped
function signQueryString(params = {}) {
  return Object.keys(params)
    .sort()
    .map((k) => {
      const v = params[k];
      if (v === null || v === undefined || v === "") return null;
      return `${k}${v}`;
    })
    .filter(Boolean)
    .join("");
}

// sign = sha256(sha256(nonce + timestamp + apiKey + query + body) + secret)
function sign(apiKey, secret, nonce, timestamp, query, body) {
  const message = `${nonce}${timestamp}${apiKey}${query}${body}`;
  const digest = createHash("sha256").update(message).digest("hex");
  return createHash("sha256").update(digest + secret).digest("hex");
}

async function signedGet(apiKey, secret, path, params) {
  const nonce = randomUUID();
  const timestamp = String(Date.now());
  const signStr = signQueryString(params);
  const sig = sign(apiKey, secret, nonce, timestamp, signStr, "");
  const qs = new URLSearchParams(params).toString();
  const url = `${BASE}${path}${qs ? "?" + qs : ""}`;
  const res = await fetch(url, {
    headers: {
      "api-key": apiKey,
      nonce,
      timestamp,
      sign: sig,
      language: "en-US",
      "Content-Type": "application/json",
    },
  });
  return res.json();
}

// --- self-check against the official test vector --------------------------
function selfCheck() {
  const apiKey = "11111111111111111";
  const secret = "22222222222222222";
  const nonce = "33333333333333333";
  const timestamp = "1747104711262";
  const want = "40e87256554e263e39201a3979469b7fbb640d1c7c41759abf5c6a18f937f922";
  const got = sign(apiKey, secret, nonce, timestamp, "marginCoinUSDT", "");
  const ok = got === want;
  console.log(ok ? "PASS sign vector" : `FAIL sign vector: got ${got}`);
  // urlencoded form must NOT match
  const wrong = sign(apiKey, secret, nonce, timestamp, "marginCoin=USDT", "");
  console.log(wrong !== want ? "PASS urlencoded-guard" : "FAIL urlencoded-guard");
  if (!ok) process.exit(1);
}

selfCheck();
export { BASE, sign, signQueryString, signedGet };