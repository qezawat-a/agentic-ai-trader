# Bitunix Futures — Multi-language open-api examples

The official Bitunix demo repo (`Demo/` under
`https://github.com/qezawat-a/open-api` and the canonical
`https://github.com/BitunixOfficial/open-api`) ships reference clients in
Node, Python, Go, Java and PHP. This folder mirrors that intent against **this**
project's contract so the signing behaves identically everywhere.

The critical, non-negotiable signing rule (error 10007 guard):

```
digest = sha256(nonce + timestamp + apiKey + sortedQueryString + body)
sign   = sha256(digest + secretKey)

sortedQueryString = keys sorted, then key+value concatenated, NO '=' and NO '&'
body              = compact JSON, keys sorted, byte-identical to what is sent
```

Files:

| File | Language | Notes |
|---|---|---|
| `node/index.js` | Node (ESM) | fetch-based, async |
| `python/bitunix_client.py` | Python | `hashlib` only, no deps |
| `go/main.go` | Go | stdlib `crypto/sha256` |
| `java/BitunixSign.java` | Java | `MessageDigest` |
| `php/bitunix_client.php` | PHP | `hash()` |

## Official test vector

Every file self-checks against the vector from the demo repo:

```
apiKey     = 11111111111111111
secretKey  = 22222222222222222
nonce      = 33333333333333333
timestamp  = 1747104711262
queryParams= marginCoinUSDT   (sorted key+value, no '=')
body       = ""
want       = 40e87256554e263e39201a3979469b7fbb640d1c7c41759abf5c6a18f937f922
```

Signing the urlencoded form (`marginCoin=USDT`) yields a different digest and the
exchange answers **error 10007**.