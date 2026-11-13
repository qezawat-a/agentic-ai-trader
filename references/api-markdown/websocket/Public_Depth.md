<!-- source: https://www.bitunix.com/api-docs/futures/websocket/public/depth%20channel.html -->

# Public Depth

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
Use
books
for snapshot data,
book1
for 1 depth level,
book5
for 5 depth levels,
book15
for 15 depth levels.
books
: Push the full
snapshot
data for the first time, push all afterwards, that is, if there is a change in depth, the depth data that has changed will be pushed.
book1
: 1 depth level will be pushed every time.
book5
: 5 depth levels will be pushed every time.
book15
: 15 depth levels will be pushed every time.
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
Channel name: depth_
books,depth_book1,depth_book5,depth_book15
> symbol
String
Yes
Product ID
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
"depth_book1"
}
]
}
Push Parameters
​
Parameter
Type
Description
ch
Object
Channel name
symbol
String
Product ID
ts
Int64
Time stamp
data
String
Subscription data
> a
List<String>
Seller depth
> b
List<String>
Buyer depth
push data:
json
{
"ch"
:
"depth_book1"
,
"symbol"
:
"BTCUSDT"
,
"ts"
:
1775541541009
,
"data"
:{
"b"
:[
[
"7403.89"
,
"0.002"
]
],
"a"
: [
[
"7405.96"
,
"3.340"
]
]
}
}
Pager
Previous page
Tp Sl Channel
Next page
Kline channel
```
