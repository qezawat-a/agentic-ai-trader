<!-- source: https://www.bitunix.com/api-docs/futures/websocket/public/MarketPrice%20Channel.html -->

# Public Price

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
Channel, price
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
"price"
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
> mp
String
Market price
> ip
String
Index price
> fr
String
Funding rate
> ft
String
Funding rate settlement time
> nft
String
Next funding rate settlement time
push data:
json
{
"ch"
:
"price"
,
"symbol"
:
"BNBUSDT"
,
"ts"
:
1732178884994
,
"data"
:{
"ip"
:
"0.0010"
,
"mp"
:
"10000"
,
"fr"
:
"0.013461"
,
"ft"
:
"2024-12-04T11:00:00Z"
,
"nft"
:
"2024-12-04T12:00:00Z"
}
}
Pager
Previous page
Kline channel
Next page
Ticker Channel
```
