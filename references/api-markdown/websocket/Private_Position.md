<!-- source: https://www.bitunix.com/api-docs/futures/websocket/private/Position%20Channel.html -->

# Private Position

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
Subscribe the position channel
Data will be pushed when the following events occurred:
Open/Close orders are created
Open/Close orders are filled
Orders are canceled
Push Parameters
​
Parameter
Type
Description
ch
String
Channel name: position
ts
Int64
Time stamp
data
<Object>
Subscription data
> event
String
Event，OPEN/UPDATE/CLOSE
> positionId
String
position Id
> marginMode
String
Margin mode,ISOLATION/CROSS
> positionMode
String
Position mode,ONE_WAY/HEDGE
> side
String
Position direction,SHORT/LONG
> leverage
String
Leverage
> margin
String
margin
> ctime
String
Create time
> qty
String
Open position size
> symbol
String
Symbol
> realizedPNL
String
Realized PnL(exclude funding fee and transaction fee)
> unrealizedPNL
String
Unrealized PnL
> funding
String
total funding fee during the position
> fee
String
Deducted transaction fees: transaction fees deducted during the position
Pager
Previous page
Order Channel
Next page
Tp Sl Channel
```
