<!-- source: https://www.bitunix.com/api-docs/futures/websocket/public/Trade%20Channel.html -->

# Public Trade

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
Get the public trade data
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
op list
> symbol
String
Yes
Product ID e.g: ETHUSDT
> ch
String
Yes
Channel, trade
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
"trade"
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
Channel,
trade
symbol
String
Symbol: ETHUSDT
ts
String
Time stamp
data
List<Object>
Data
> p
String
Filled price
> v
String
Filled amount
> s
String
Filled side, sell/buy
> t
String
Time stamp
push data:
json
{
"ch"
:
"trade"
,
"symbol"
:
"BTCUSDT"
,
"ts"
:
1775540872598
,
"data"
: [
{
"t"
:
"2026-04-07T05:47:52Z"
,
"p"
:
"68621.4"
,
"v"
:
"0.7142"
,
"s"
:
"buy"
},
{
"t"
:
"2026-04-07T05:47:52Z"
,
"p"
:
"68621.4"
,
"v"
:
"0.0018"
,
"s"
:
"sell"
}
]
}
Pager
Previous page
Tickers Channel
```
