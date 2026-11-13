<!-- source: https://www.bitunix.com/api-docs/futures/websocket/public/kline%20channel.html -->

# Public Kline

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
Retrieve the candlesticks data of a symbol. Data will be pushed every 500 ms.
The channel will push a snapshot after successful subscription, followed by subsequent updates.
To switch K-line intervals without disconnecting the WebSocket, you must first send an "unsubscribe" command to cancel the previous K-line subscription before subscribing to the new interval. For example, if you are currently subscribed to
mark_kline_1min
and want to switch to
mark_kline_15min
, you must first unsubscribe from
mark_kline_1min
and then subscribe to
mark_kline_15min
.
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
List of channels to request subscription
> ch
String
Yes
Channel name, The subscription channel is: Price Type
klineTime Interval；The price types include market price and marked price；market
_
kline
_
1min,mark
_
kline
_
1min,market
_
kline
_
3min,mark
_
kline
_
3min,market
_
kline
_
5min,mark
_
kline
_
5min,market
_
kline
_
15min,mark
_
kline
_
15min,market
_
kline
_
30min,mark
_
kline
_
30min,market
_
kline
_
60min,mark
_
kline
_
60min,market
_
kline
_
2h,mark
_
kline
_
2h,market
_
kline
_
4h,mark
_
kline
_
4h,market
_
kline
_
6h,mark
_
kline
_
6h,market
_
kline
_
8h,mark
_
kline
_
8h,market
_
kline
_
12h,mark
_
kline
_
12h,market
_
kline
_
1day,mark
_
kline
_
1day,market
_
kline
_
3day,mark
_
kline
_
3day,market
_
kline
_
1week,mark
_
kline
_
1week,market
_
kline
_
1month,mark
_
kline
_
1month
> symbol
String
Yes
Product ID E.g. ETHUSDT
request example:
json
{
"op"
:
"subscribe"
,
"args"
:[
{
"symbol"
:
"BTCUSDT"
,
"ch"
:
"market_kline_1min"
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
symbol
String
Product ID E.g. ETHUSDT
ts
int64
Time stamp
data
List<String>
Subscription data
> o
String
Opening price
> h
String
Highest price
> l
String
Lowest price
> c
String
Closing price
> b
String
Trading volume of the coin
> q
String
Trading volume of quote currency
push data:
json
{
"ch"
:
"market_kline_1min"
,
"symbol"
:
"BTCUSDT"
,
"ts"
:
1775541412718
,
"data"
:{
"o"
:
"68581.4"
,
"c"
:
"68583.4"
,
"h"
:
"68590"
,
"l"
:
"68579.5"
,
"b"
:
"5.2395"
,
"q"
:
"359348.14078"
}
}
Pager
Previous page
Depth channel
Next page
MarketPrice Channel
```
