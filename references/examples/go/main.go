// Bitunix Futures — Go reference client (stdlib only).
// Run: go run main.go
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"sort"
	"strings"
)

const baseURL = "https://fapi.bitunix.com"

// signQueryString builds the sorted key+value concatenation with no '=' and no '&'.
func signQueryString(params map[string]string) string {
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var sb strings.Builder
	for _, k := range keys {
		v := params[k]
		if v == "" {
			continue
		}
		sb.WriteString(k)
		sb.WriteString(v)
	}
	return sb.String()
}

func sha256hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// sign = sha256(sha256(nonce + timestamp + apiKey + query + body) + secret)
func sign(apiKey, secret, nonce, timestamp, query, body string) string {
	message := nonce + timestamp + apiKey + query + body
	digest := sha256hex(message)
	return sha256hex(digest + secret)
}

func main() {
	// official test vector
	const apiKey = "11111111111111111"
	const secret = "22222222222222222"
	const nonce = "33333333333333333"
	const timestamp = "1747104711262"
	const want = "40e87256554e263e39201a3979469b7fbb640d1c7c41759abf5c6a18f937f922"

	got := sign(apiKey, secret, nonce, timestamp, "marginCoinUSDT", "")
	if got != want {
		fmt.Printf("FAIL sign vector: got %s\n", got)
		os.Exit(1)
	}
	wrong := sign(apiKey, secret, nonce, timestamp, "marginCoin=USDT", "")
	if wrong == want {
		fmt.Println("FAIL urlencoded-guard")
		os.Exit(1)
	}
	if signQueryString(map[string]string{"b": "2", "a": "1"}) != "a1b2" {
		fmt.Println("FAIL sort")
		os.Exit(1)
	}
	fmt.Println("PASS sign vector / urlencoded-guard / sort")
}