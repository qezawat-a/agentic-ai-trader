<!-- source: https://www.bitunix.com/api-docs/futures/websocket/private/Order%20Channel.html -->

# Private Order

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
Subscribe the order channel
Data will be pushed when the following events occured:
Open/Close orders are created
Open/Close orders are filled
Orders canceled
Push Parameters
​
Parameter
Type
Description
ch
String
Channel name: order
ts
Int64
Gateway send time in milliseconds. Not business event time; do not confuse with
ctime
/
mtime
.
data
<Object>
Subscription data
> event
String
Event，CREATE/UPDATE/CLOSE
> orderId
String
order Id
> symbol
String
Symbol
> positionType
String
Margin mode,ISOLATION/CROSS
> positionMode
String
Position mode,ONE_WAY/HEDGE
> side
String
Sell,BUY/SELL
> effect
String
Order expiration date.Required if the orderType is limit.IOC: Immediate or cancel;FOK: Fill or kill;GTC: Good till canceled(default value);POST_ONLY: POST only;
> type
String
LIMIT/MARKET
> qty
String
Amount (base coin)
> price
String
Price of the order. Required if the order type is
LIMIT
> ctime
String
Create time, ISO-8601 nanosecond string, e.g.
2024-05-16T08:13:09.123456789Z
> mtime
String
Last modify time, ISO-8601 nanosecond string. Same format as
ctime
. REST order APIs use millisecond integers.
> leverage
String
Leverage
> orderStatus
String
INIT:prepare status;NEW:pending;PART_FILLED:partially filled;CANCELED:canceled;FILLEDAll filled;PART_FILLED_CANCELED:part filled canceled;
> fee
String
Deducted transaction fees: transaction fees deducted during the position
> averagePrice
String
Average price
> dealAmount
String
Deal amount
> clientId
String
Client ID
> tpStopType
String
Take profit trigger type.MARK_PRICE/LAST_PRICE
> tpPrice
String
Take profit trigger price
> tpOrderType
String
Take profit trigger place order type.LIMIT/MARKET
> tpOrderPrice
String
Take profit trigger place order price.LIMIT/MARKET
> slStopType
String
Stop loss trigger type.MARK_PRICE/LAST_PRICE
> slPrice
String
Stop loss trigger price
> slOrderType
String
Stop loss trigger place order type.LIMIT/MARKET
> slOrderPrice
String
Stop loss trigger place order price.LIMIT/MARKET
Pager
Previous page
Balance Channel
Next page
Position Channel
```
