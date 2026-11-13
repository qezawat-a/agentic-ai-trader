<!-- source: https://www.bitunix.com/api-docs/futures/websocket/private/Balance%20Channel.html -->

# Private Balance

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
Balance
Push Parameters
​
Parameter
Type
Description
ch
String
Channel name: balance
ts
Int64
Time stamp
data
<Object>
> coin
String
coin
> available
String
available
> frozen
String
frozen = isolationFrozen + crossFrozen
> isolationFrozen
String
Freeze on a per warehouse basis
> crossFrozen
String
Full warehouse entrusted freeze
> margin
String
Margin
> isolationMargin
String
Warehouse by warehouse margin.
> crossMargin
String
Full warehouse margin.
> expMoney
String
Experience Money.
Pager
Previous page
WebSocket
Next page
Order Channel
```
