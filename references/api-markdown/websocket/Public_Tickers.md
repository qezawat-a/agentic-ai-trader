<!-- source: https://www.bitunix.com/api-docs/futures/websocket/public/Tickers%20Channel.html -->

# Public Tickers

```text
OpenApi ｜ Bitunix
Skip to content
OpenApi ｜ Bitunix
Appearance
Menu
Return to top
Sidebar Navigation
Log
Change log
Account
Adjust position margin
Change leverage
Change margin mode
Change position mode
Get leverage and margin mode
Get position mode
Get single account
Get trading settings
Common
Introduction
Sign
CopyTrading
Asset
Asset query
Transfer asset from main account to sub account
Transfer asset from subaccount to main account
ErrorCode
Error code
Market
Get depth
Get funding rate batch
Get funding rate history
Get funding rate
Get kline
Get tickers
Get trading pairs
Position
Get history positions
Get pending positions
Get position tiers
Tp sl
Cancel tp sl order
Get history tp sl order
Get pending tp sl order
Modify position tp sl order
Modify tp sl order
Place position tp sl order
Place tp sl order
Trade
Batch order
Cancel all orders
Cancel orders
Close all position
Flash close position
Get history orders
Get history trades
Get order detail
Get pending orders
Modify order
Place order
Websocket
Prepare
WebSocket
Private
Balance Channel
Order Channel
Position Channel
Tp Sl Channel
Public
Depth channel
Kline channel
MarketPrice Channel
Ticker Channel
Tickers Channel
Trade Channel
On this page
Description
​
24-hour rolling window mini-ticker statistics for all symbols (this is aggregated stream ticker data, with a different data structure compared to individual ticker subscription). These statistics are not based on UTC daily data, but rather a 24-hour rolling window from the request time backward.
Request Parameters
​
Parameter
Type
Required
Description
op
String
Yes
Operation, subscribe unsubscribe
args
List<Object>
Yes
> symbol
String
Yes
Product ID e.g: ETHUSDT
> ch
String
Yes
Channel, tickers
request example:
json
{
"op"
:
"subscribe"
,
"args"
: [{
"symbol"
:
"BTCUSDT"
,
"ch"
:
"tickers"
},
{
"symbol"
:
"ETHUSDT"
,
"ch"
:
"tickers"
}
]
}
Push Parameters
​
Parameter
Type
Description
ch
String
Channel name
ts
int64
Time stamp
data
List<String>
Subscription data
> s
String
symbol, Product ID E.g. ETHUSDT
> o
String
Opening price
> h
String
Highest price
> l
String
Lowest price
> la
String
Last price
> b
String
Trading volume of the coin
> q
String
Trading volume of quote currency
> r
String
24-hour fluctuations
> bd
String
Best Bid Price
> ak
String
Best Ask Price
> bv
String
Best Bid Volume
> av
String
Best Ask Volume
push data:
json
{
"ch"
:
"tickers"
,
"ts"
:
1732178884994
,
"data"
:[
{
"s"
:
"BTCUSDT"
,
"la"
:
"68650.9"
,
"o"
:
"69141.6"
,
"h"
:
"70319.9"
,
"l"
:
"68241.9"
,
"b"
:
"26295.3977"
,
"q"
:
"1823374525.0193"
,
"r"
:
"-0.7097029863"
,
"bd"
:
"68650.9"
,
"ak"
:
"68651"
,
"bv"
:
"0.9747"
,
"av"
:
"2.3606"
},
{
"s"
:
"ETHUSDT"
,
"la"
:
"2104.61"
,
"o"
:
"2128.49"
,
"h"
:
"2173.75"
,
"l"
:
"2086.79"
,
"b"
:
"945498.652"
,
"q"
:
"2018286647.13588"
,
"r"
:
"-1.1219221138"
,
"bd"
:
"2104.6"
,
"ak"
:
"2104.61"
,
"bv"
:
"27.789"
,
"av"
:
"7.905"
}
]
}
Pager
Previous page
Ticker Channel
Next page
Trade Channel
```
