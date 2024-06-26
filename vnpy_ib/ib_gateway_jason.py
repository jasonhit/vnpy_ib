"""
IB Symbol Rules

SPY-USD-STK   SMART
EUR-USD-CASH  IDEALPRO
XAUUSD-USD-CMDTY  SMART
ES-202002-USD-FUT  GLOBEX
SI-202006-1000-USD-FUT  NYMEX
ES-2020006-C-2430-50-USD-FOP  GLOBEX
"""


from copy import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Thread, Condition
from typing import Optional, Dict, Any, List
import shelve
from tzlocal import get_localzone_name
import pytz
from queue import Empty, Queue
import pandas as pd

from vnpy.event import EventEngine
from ibapi.client import EClient
from ibapi.common import OrderId, TickAttrib, TickerId
from ibapi.contract import Contract, ContractDetails
from ibapi.execution import Execution
from ibapi.order import Order
from ibapi.order_state import OrderState
from ibapi.ticktype import TickType, TickTypeEnum
from ibapi.wrapper import EWrapper
from ibapi.common import BarData as IbBarData

from ibapi.utils import intMaxString
from ibapi.utils import floatMaxString
from ibapi.utils import decimalMaxString

from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    TickData,
    OrderData,
    TradeData,
    PositionData,
    AccountData,
    ContractData,
    BarData,
    OrderRequest,
    CancelRequest,
    SubscribeRequest,
    HistoryRequest
)
from vnpy.trader.constant import (
    Product,
    OrderType,
    Direction,
    Exchange,
    Currency,
    Status,
    OptionType,
    Interval
)
from vnpy.trader.utility import get_file_path, get_folder_path, ZoneInfo
from vnpy.trader.event import EVENT_TIMER
from vnpy.event import Event

# 委托状态映射
STATUS_IB2VT: Dict[str, Status] = {
    "ApiPending": Status.SUBMITTING,
    "PendingSubmit": Status.SUBMITTING,
    "PreSubmitted": Status.NOTTRADED,
    "Submitted": Status.NOTTRADED,
    "ApiCancelled": Status.CANCELLED,
    "Cancelled": Status.CANCELLED,
    "Filled": Status.ALLTRADED,
    "Inactive": Status.REJECTED,
}

# 多空方向映射
DIRECTION_VT2IB: Dict[Direction, str] = {Direction.LONG: "BUY", Direction.SHORT: "SELL"}
DIRECTION_IB2VT: Dict[str, Direction] = {v: k for k, v in DIRECTION_VT2IB.items()}
DIRECTION_IB2VT["BOT"] = Direction.LONG
DIRECTION_IB2VT["SLD"] = Direction.SHORT

# 委托类型映射
ORDERTYPE_VT2IB: Dict[OrderType, str] = {
    OrderType.LIMIT: "LMT",
    OrderType.MARKET: "MKT",
    OrderType.STOP: "STP"
}
ORDERTYPE_IB2VT: Dict[str, OrderType] = {v: k for k, v in ORDERTYPE_VT2IB.items()}

# 交易所映射
EXCHANGE_VT2IB: Dict[Exchange, str] = {
    Exchange.SMART: "SMART",
    Exchange.NYMEX: "NYMEX",
    Exchange.COMEX: "COMEX",
    Exchange.GLOBEX: "GLOBEX",
    Exchange.IDEALPRO: "IDEALPRO",
    Exchange.CME: "CME",
    Exchange.CBOT: "CBOT",
    Exchange.CBOE: "CBOE",
    Exchange.ICE: "ICE",
    Exchange.SEHK: "SEHK",
    Exchange.SSE: "SEHKNTL",
    Exchange.SZSE: "SEHKSZSE",
    Exchange.HKFE: "HKFE",
    Exchange.CFE: "CFE",
    Exchange.TSE: "TSE",
    Exchange.NYSE: "NYSE",
    Exchange.NASDAQ: "NASDAQ",
    Exchange.AMEX: "AMEX",
    Exchange.ARCA: "ARCA",
    Exchange.EDGEA: "EDGEA",
    Exchange.ISLAND: "ISLAND",
    Exchange.BATS: "BATS",
    Exchange.IEX: "IEX",
    Exchange.IBKRATS: "IBKRATS",
    Exchange.OTC: "PINK",
    Exchange.SGX: "SGX"
}
EXCHANGE_IB2VT: Dict[str, Exchange] = {v: k for k, v in EXCHANGE_VT2IB.items()}

# 产品类型映射
PRODUCT_IB2VT: Dict[str, Product] = {
    "STK": Product.EQUITY,
    "CASH": Product.FOREX,
    "CMDTY": Product.SPOT,
    "FUT": Product.FUTURES,
    "OPT": Product.OPTION,
    "FOP": Product.OPTION,
    "CONTFUT": Product.FUTURES,
    "IND": Product.INDEX
}

# 期权类型映射
OPTION_VT2IB: Dict[str, OptionType] = {OptionType.CALL: "CALL", OptionType.PUT: "PUT"}

# 货币类型映射
CURRENCY_VT2IB: Dict[Currency, str] = {
    Currency.USD: "USD",
    Currency.CAD: "CAD",
    Currency.CNY: "CNY",
    Currency.HKD: "HKD",
}

# 切片数据字段映射
TICKFIELD_IB2VT: Dict[int, str] = {
    0: "bid_volume_1",
    1: "bid_price_1",
    2: "ask_price_1",
    3: "ask_volume_1",
    4: "last_price",
    5: "last_volume",
    6: "high_price",
    7: "low_price",
    8: "volume",
    9: "pre_close",
    14: "open_price",
}

# 账户类型映射
ACCOUNTFIELD_IB2VT: Dict[str, str] = {
    "NetLiquidationByCurrency": "balance",
    "NetLiquidation": "balance",
    "UnrealizedPnL": "positionProfit",
    "AvailableFunds": "available",
    "MaintMarginReq": "margin",
}

# 数据频率映射
INTERVAL_VT2IB: Dict[Interval, str] = {
    Interval.MINUTE: "1 min",
    Interval.HOUR: "1 hour",
    Interval.DAILY: "1 day",
}

# 其他常量
LOCAL_TZ = ZoneInfo(get_localzone_name())
JOIN_SYMBOL: str = "-"


class IbGateway(BaseGateway):
    """
    VeighNa用于对接IB的交易接口。
    """

    default_name: str = "IB"

    default_setting: Dict[str, Any] = {
        "TWS地址": "127.0.0.1",
        "TWS端口": 7497,
        "客户号": 1,
        "交易账户": ""
    }

    exchanges: List[str] = list(EXCHANGE_VT2IB.keys())

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        """构造函数"""
        super().__init__(event_engine, gateway_name)

        self.api: "IbApi" = IbApi(self)
        self.count: int = 0

    def connect(self, setting: dict) -> None:
        """连接交易接口"""
        host: str = setting["TWS地址"]
        port: int = setting["TWS端口"]
        clientid: int = setting["客户号"]
        account: str = setting["交易账户"]

        self.api.connect(host, port, clientid, account)

        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def close(self) -> None:
        """关闭接口"""
        self.api.close()
        
        # 保存ib的合约信息文件夹，用户可读，但这个csv文件，不读回系统
        self.api.save_ib_contracts_details_to_csv()

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        self.api.subscribe(req)

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        return self.api.send_order(req)

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        self.api.cancel_order(req)

    def query_account(self) -> None:
        """查询资金"""
        pass

    def query_position(self) -> None:
        """查询持仓"""
        pass

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """查询历史数据"""
        return self.api.query_history(req)

    def process_timer_event(self, event: Event) -> None:
        """定时事件处理"""
        self.count += 1
        if self.count < 10:
            return
        self.count = 0

        self.api.check_connection()


class IbApi(EWrapper):
    """IB的API接口"""

    data_filename: str = "ib_contract_data.db"
    data_filepath: str = str(get_file_path(data_filename))

    def __init__(self, gateway: IbGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: IbGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.status: bool = False

        self.reqid: int = 0
        self.orderid: int = 0
        self.clientid: int = 0
        self.history_reqid: int = 0
        self.account: str = ""
        self.ticks: Dict[int, TickData] = {}
        self.orders: Dict[str, OrderData] = {}
        self.accounts: Dict[str, AccountData] = {}
        self.contracts: Dict[str, ContractData] = {}

        self.ib_contracts_details: Dict[str, ContractDetails] = {} # ib的contract details

        self.tick_exchange: Dict[int, Exchange] = {}
        self.subscribed: Dict[str, SubscribeRequest] = {}
        self.data_ready: bool = False
        self.order_ready: bool = False
        self.subscribeRequest_queue = Queue()

        self.history_req: HistoryRequest = None
        self.history_condition: Condition = Condition()
        self.history_buf: List[BarData] = []

        self.client: EClient = EClient(self)

    def connectAck(self) -> None:
        """连接成功回报"""
        self.status = True
        self.gateway.write_log("IB TWS连接成功")

        # 由于加载合约信息后，会发送on_contract事件，该事件会促使类似datarecorder订阅行情，但现在刚连接上，并一定是马上订阅行情的好时机
        self.load_contract_data()

        """  
        Code:2104
        TWS message：Market data farm connection is OK。 
        Additional notes：notification that connection to the market data server is ok. This is a notification and not a true error condition, and is expected on first establishing connection.
        
        Important: The IBApi.EWrapper.nextValidID callback is commonly used to indicate that the connection is completed and other messages can be sent from the API client to TWS. 
        There is the possibility that function calls made prior to this time could be dropped by TWS
        """
        self.data_ready = False
        self.order_ready = False

        

    def connectionClosed(self) -> None:
        """连接断开回报"""
        self.status = False
        self.gateway.write_log("IB TWS连接断开")

    def nextValidId(self, orderId: int) -> None:
        """下一个有效订单号回报"""
        super().nextValidId(orderId)

        self.client.reqCurrentTime()
        
        if not self.orderid:
            self.orderid = orderId
        
        if not self.order_ready:
            self.order_ready = True
        
        if not self.data_ready:
            self.data_ready = True

        # 断线后重连，需要把订阅过的合约重新订阅
        reqs: list = list(self.subscribed.values())
        self.subscribed.clear()
        for req in reqs:
            self.subscribe(req)
        
        # 启动负责订阅行情的线程，只在初始化成功后启动，该线程断线后会退出(self.status = False)
        self.subscribeRequest_thread = Thread(target=self.subscribeRunner)
        self.subscribeRequest_thread.start()

    def currentTime(self, time: int) -> None:
        """IB当前服务器时间回报"""
        super().currentTime(time)

        dt: datetime = datetime.fromtimestamp(time)
        time_string: str = dt.strftime("%Y-%m-%d %H:%M:%S.%f")

        msg: str = f"服务器时间: {time_string}"
        self.gateway.write_log(msg)

    def error(self, reqId: TickerId, errorCode: int, errorString: str, advancedOrderRejectJson = "") -> None:
        """具体错误请求回报"""
        super().error(reqId, errorCode, errorString)
    
        # 2000-2999信息通知不属于报错信息
        if reqId == self.history_reqid and errorCode not in range(2000, 3000):
            self.history_condition.acquire()
            self.history_condition.notify()
            self.history_condition.release()

        msg: str = f"信息通知，代码：{errorCode}，内容: {errorString}"
        self.gateway.write_log(msg)

        '''
        1100:Connectivity between IB and the TWS has been lost.Your TWS/IB Gateway has been disconnected from IB servers. This can occur because of an internet connectivity issue, a nightly reset of the IB servers, or a competing session.
        1101:Connectivity between IB and TWS has been restored- data lost.*.The TWS/IB Gateway has successfully reconnected to IB's servers. Your market data requests have been lost and need to be re-submitted.
        1102:Connectivity between IB and TWS has been restored- data maintained.The TWS/IB Gateway has successfully reconnected to IB's servers. Your market data requests have been recovered and there is no need for you to re-submit them.        
        '''
        # TWS与IB服务器已经断线
        if errorCode == 1100:
            self.order_ready = False
            self.data_ready = False
        
        # TWS与IB服务器已经重连，需要重新订阅行情
        if errorCode == 1101:
            self.order_ready = True
            self.data_ready = True

            reqs: list = list(self.subscribed.values())
            self.subscribed.clear()
            for req in reqs:
                self.subscribe(req)

        # TWS与IB服务器已经重连，不需要做任何事情
        if errorCode == 1102:
            self.order_ready = True
            self.data_ready = True

        '''
        # 行情服务器已连接
        if errorCode == 2104 and not self.data_ready:
            self.data_ready = True

            self.client.reqCurrentTime()

            reqs: list = list(self.subscribed.values())
            self.subscribed.clear()
            for req in reqs:
                self.subscribe(req)
        '''

    def tickPrice(
        self, reqId: TickerId, tickType: TickType, price: float, attrib: TickAttrib
    ) -> None:
        if reqId not in self.ticks:
            return
        
        """tick价格更新回报"""
        super().tickPrice(reqId, tickType, price, attrib)

        if tickType not in TICKFIELD_IB2VT:
            return

        tick: TickData = self.ticks[reqId]
        name: str = TICKFIELD_IB2VT[tickType]
        setattr(tick, name, price)

        # 更新tick数据name字段
        contract: ContractData = self.contracts.get(tick.vt_symbol, None)
        if contract:
            tick.name = contract.name

        # 本地计算Forex of IDEALPRO和Spot Commodity的tick时间和最新价格
        exchange: Exchange = self.tick_exchange[reqId]
        if exchange is Exchange.IDEALPRO or "CMDTY" in tick.symbol:
            if not tick.bid_price_1 or not tick.ask_price_1:
                return
            tick.last_price = round((tick.bid_price_1 + tick.ask_price_1) / 2,5)
            # 处理计算出来的last price的数字位数，简单起见，最大5位数字，因为公式计算出来的last price位数太长


        # datetime是quote update time，不是last trade time，所以每次行情变化，都修改这个time  
        tick.datetime = datetime.now(LOCAL_TZ)
        """
        Market data tick price callback. Handles all price related ticks. Every tickPrice callback is followed by a tickSize. 
        A tickPrice value of -1 or 0 followed by a tickSize of 0 indicates there is no data for this field currently available, whereas a tickPrice with a positive tickSize indicates an active quote of 0 (typically for a combo contract).
        """
        # IB API描述中，有tickPrice变化，一定会紧跟一个tickSize变化，所以，tickSize没更新之前，没必要提交on_tick，否则反而会给出错误的tickSize
        # self.gateway.on_tick(copy(tick))

    def tickSize(
        self, reqId: TickerId, tickType: TickType, size: Decimal
    ) -> None:
        if reqId not in self.ticks:
            return
        
        """tick数量更新回报"""
        super().tickSize(reqId, tickType, size)

        if tickType not in TICKFIELD_IB2VT:
            return

        tick: TickData = self.ticks[reqId]
        name: str = TICKFIELD_IB2VT[tickType]
        setattr(tick, name, size)

        # datetime是quote update time，不是last trade time，所以每次行情变化，都修改这个time  
        tick.datetime = datetime.now(LOCAL_TZ)

        self.gateway.on_tick(copy(tick))

    def tickString(
        self, reqId: TickerId, tickType: TickType, value: str
    ) -> None:
        # 因为这里这里只是更新 last trade time，已经没有必要了，我们把datetime改成quote update time了
        return
    
        """tick字符串更新回报"""
        super().tickString(reqId, tickType, value)

        if tickType != TickTypeEnum.LAST_TIMESTAMP:
            return

        tick: TickData = self.ticks[reqId]
        dt: datetime = datetime.fromtimestamp(int(value))
        tick.datetime = dt.replace(tzinfo=LOCAL_TZ)

        self.gateway.on_tick(copy(tick))

    def orderStatus(
        self,
        orderId: OrderId,
        status: str,
        filled: Decimal,
        remaining: Decimal,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        """订单状态更新回报"""
        super().orderStatus(
            orderId,
            status,
            filled,
            remaining,
            avgFillPrice,
            permId,
            parentId,
            lastFillPrice,
            clientId,
            whyHeld,
            mktCapPrice,
        )

        orderid: str = str(orderId)
        order: OrderData = self.orders.get(orderid, None)
        if not order:
            return

        order.traded = float(filled) # convert Decimal to float

        # 过滤撤单中止状态
        order_status: Status = STATUS_IB2VT.get(status, None)
        if order_status:
            order.status = order_status

        self.gateway.on_order(copy(order))

        self.gateway.write_log(f"orderStatus:{order}")

    def openOrder(
        self,
        orderId: OrderId,
        ib_contract: Contract,
        ib_order: Order,
        orderState: OrderState,
    ) -> None:
        """新订单回报"""
        super().openOrder(
            orderId, ib_contract, ib_order, orderState
        )

        orderid: str = str(orderId)
        order: OrderData = OrderData(
            symbol=generate_symbol(ib_contract),
            exchange=EXCHANGE_IB2VT.get(ib_contract.exchange, Exchange.SMART),
            type=ORDERTYPE_IB2VT[ib_order.orderType],
            orderid=orderid,
            direction=DIRECTION_IB2VT[ib_order.action],
            volume=ib_order.totalQuantity,
            gateway_name=self.gateway_name,
        )

        if order.type == OrderType.LIMIT:
            order.price = ib_order.lmtPrice
        elif order.type == OrderType.STOP:
            order.price = ib_order.auxPrice

        self.orders[orderid] = order
        # 没必要发送此事件，因为每次OnOrderStatus都会前，都会发送一次OnOpenOrder，而且OnOpenOrder的order中，status一直都是submitting，回干扰策略的逻辑
        #self.gateway.on_order(copy(order))

    def updateAccountValue(
        self, key: str, val: str, currency: str, accountName: str
    ) -> None:
        """账号更新回报"""
        super().updateAccountValue(key, val, currency, accountName)

        if not currency or key not in ACCOUNTFIELD_IB2VT:
            return

        accountid: str = f"{accountName}.{currency}"
        account: AccountData = self.accounts.get(accountid, None)
        if not account:
            account = AccountData(
                accountid=accountid,
                gateway_name=self.gateway_name
            )
            self.accounts[accountid] = account

        name: str = ACCOUNTFIELD_IB2VT[key]
        setattr(account, name, float(val))

    def updatePortfolio(
        self,
        contract: Contract,
        position: Decimal,
        marketPrice: float,
        marketValue: float,
        averageCost: float,
        unrealizedPNL: float,
        realizedPNL: float,
        accountName: str,
    ) -> None:
        """持仓更新回报"""
        super().updatePortfolio(
            contract,
            position,
            marketPrice,
            marketValue,
            averageCost,
            unrealizedPNL,
            realizedPNL,
            accountName,
        )

        if contract.exchange:
            exchange: Exchange = EXCHANGE_IB2VT.get(contract.exchange, None)
        elif contract.primaryExchange:
            exchange: Exchange = EXCHANGE_IB2VT.get(contract.primaryExchange, None)
        else:
            exchange: Exchange = Exchange.SMART   # Use smart routing for default

        if not exchange:
            msg: str = f"存在不支持的交易所持仓{generate_symbol(contract)} {contract.exchange} {contract.primaryExchange}"
            self.gateway.write_log(msg)
            return

        try:
            ib_size: int = int(contract.multiplier)
        except ValueError:
            ib_size = 1
        price = averageCost / ib_size

        pos: PositionData = PositionData(
            symbol=generate_symbol(contract),
            exchange=exchange,
            direction=Direction.NET,
            volume=float(position), # convert Decimal to float
            price=price,
            pnl=unrealizedPNL,
            gateway_name=self.gateway_name,
        )
        self.gateway.on_position(pos)

    def updateAccountTime(self, timeStamp: str) -> None:
        """账号更新时间回报"""
        super().updateAccountTime(timeStamp)
        for account in self.accounts.values():
            self.gateway.on_account(copy(account))

    def contractDetails(self, reqId: int, contractDetails: ContractDetails) -> None:
        """合约数据更新回报"""
        super().contractDetails(reqId, contractDetails)

        # 从IB合约生成vnpy代码
        ib_contract: Contract = contractDetails.contract
        if not ib_contract.multiplier:
            ib_contract.multiplier = 1

        symbol: str = generate_symbol(ib_contract)

        # 生成合约
        contract: ContractData = ContractData(
            symbol=symbol,
            exchange=EXCHANGE_IB2VT[ib_contract.exchange],
            name=contractDetails.longName,
            product=PRODUCT_IB2VT[ib_contract.secType],
            size=int(ib_contract.multiplier),
            pricetick=contractDetails.minTick,
            net_position=True,
            history_data=True,
            stop_supported=True,
            gateway_name=self.gateway_name,
        )

        # 龙胜自己额外增加 trading hours, time zone， local symbol
        contract.trading_hours = contractDetails.tradingHours
        contract.time_zone = contractDetails.timeZoneId
        contract.ib_local_symbol = contractDetails.contract.localSymbol

        # 如果是OPT或者FOP期权，需要对期权的参数赋值
        if ib_contract.secType in ["OPT", "FOP"]:
            contract.option_strike = ib_contract.strike
            #option_underlying: str = ""     # vt_symbol of underlying contract
            if ib_contract.right == "C":
                contract.option_type = OptionType.CALL
            if ib_contract.right == "P":
                contract.option_type = OptionType.PUT
            #option_listed: datetime = None
            contract.option_expiry = datetime.strptime(ib_contract.lastTradeDateOrContractMonth, "%Y%m%d")
            #option_portfolio: str = ""
            #option_index: str = ""          # for identifying options with same strike price

        if contract.vt_symbol not in self.contracts:
            self.gateway.on_contract(contract)

            self.contracts[contract.vt_symbol] = contract
            self.ib_contracts_details[contract.vt_symbol] = contractDetails
            self.save_contract_data()

    def execDetails(
        self, reqId: int, contract: Contract, execution: Execution
    ) -> None:
        """交易数据更新回报"""
        super().execDetails(reqId, contract, execution)

        strTimeList = execution.time.split(" ")
        if len(strTimeList) > 2:
            trade_tz = pytz.timezone(strTimeList[2])
            dt: datetime = trade_tz.localize(datetime.strptime(strTimeList[0] + " " + strTimeList[1], "%Y%m%d %H:%M:%S"))
            # dt: datetime = datetime.strptime(execution.time, "%Y%m%d %H:%M:%S %%")
            # dt: datetime = dt.replace(tzinfo=LOCAL_TZ)
        else:
            dt: datetime = datetime.now(LOCAL_TZ)

        trade: TradeData = TradeData(
            symbol=generate_symbol(contract),
            exchange=EXCHANGE_IB2VT.get(contract.exchange, Exchange.SMART),
            orderid=str(execution.orderId),
            tradeid=str(execution.execId),
            direction=DIRECTION_IB2VT[execution.side],
            price=execution.price,
            volume=float(execution.shares),
            datetime=dt,
            gateway_name=self.gateway_name,
        )

        self.gateway.on_trade(trade)

    def managedAccounts(self, accountsList: str) -> None:
        """所有子账户回报"""
        super().managedAccounts(accountsList)

        if not self.account:
            for account_code in accountsList.split(","):
                if account_code:
                    self.account = account_code

        self.gateway.write_log(f"当前使用的交易账号为{self.account}")
        self.client.reqAccountUpdates(True, self.account)

    def historicalData(self, reqId: int, ib_bar: IbBarData) -> None:
        """历史数据更新回报"""
        # 日级别数据和周级别日期数据的数据形式为%Y%m%d
        if len(ib_bar.date) > 8:
            dt: datetime = generate_localtime(ib_bar.date) # 时间样式：20230629 10:30:00 Hongkong。datetime.strptime(ib_bar.date, "%Y%m%d %H:%M:%S")
        else:
            dt: datetime = datetime.strptime(ib_bar.date, "%Y%m%d")
        dt: datetime = dt.replace(tzinfo=LOCAL_TZ)

        bar: BarData = BarData(
            symbol=self.history_req.symbol,
            exchange=self.history_req.exchange,
            datetime=dt,
            interval=self.history_req.interval,
            volume=ib_bar.volume,
            open_price=ib_bar.open,
            high_price=ib_bar.high,
            low_price=ib_bar.low,
            close_price=ib_bar.close,
            gateway_name=self.gateway_name
        )
        if bar.volume < 0:
            bar.volume = 0

        self.history_buf.append(bar)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        """历史数据查询完毕回报"""
        self.history_condition.acquire()
        self.history_condition.notify()
        self.history_condition.release()

    def connect(self, host: str, port: int, clientid: int, account: str) -> None:
        """连接TWS"""
        if self.status:
            return

        self.host = host
        self.port = port
        self.clientid = clientid
        self.account = account

        self.client.connect(host, port, clientid)
        self.thread = Thread(target=self.client.run)
        self.thread.start()

    def check_connection(self) -> None:
        """检查连接"""
        if self.client.isConnected():
            return

        if self.status:
            self.close()

        self.client.connect(self.host, self.port, self.clientid)

        self.thread = Thread(target=self.client.run)
        self.thread.start()

    def close(self) -> None:
        """断开TWS连接"""
        if not self.status:
            return

        self.status = False
        self.client.disconnect()
    
    def subscribe(self, req: SubscribeRequest) -> None:
        """把待请阅的合约放入队列，后面由专门负责订阅行情的线程来处理订阅"""
        self.subscribeRequest_queue.put(req)

    '''
    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅tick数据更新"""
        if not self.status:
            return

        if req.exchange not in EXCHANGE_VT2IB:
            self.gateway.write_log(f"不支持的交易所{req.exchange}")
            return

        # 过滤重复订阅
        if req.vt_symbol in self.subscribed:
            return
        self.subscribed[req.vt_symbol] = req

        # 解析IB合约详情
        ib_contract: Contract = generate_ib_contract(req.symbol, req.exchange)
        if not ib_contract:
            self.gateway.write_log("代码解析失败，请检查格式是否正确")
            return

        # 通过TWS查询合约信息
        self.reqid += 1
        self.client.reqContractDetails(self.reqid, ib_contract)

        #  订阅tick数据并创建tick对象缓冲区
        self.reqid += 1
        self.gateway.write_log(f"api订阅前reqid：{self.reqid},symbol:{req.symbol}")
        self.client.reqMktData(self.reqid, ib_contract, "", False, False, [])

        tick: TickData = TickData(
            symbol=req.symbol,
            exchange=req.exchange,
            datetime=datetime.now(LOCAL_TZ),
            gateway_name=self.gateway_name,
        )
        self.ticks[self.reqid] = tick
        self.gateway.write_log(f"api订阅后reqid：{self.reqid},symbol:{req.symbol}")
        self.tick_exchange[self.reqid] = req.exchange
    '''

    def subscribeRunner(self) -> None:
        while self.status:
            try:
                # self.gateway.write_log(f"订阅行情队列{self.subscribeRequest_queue.qsize()}")
                
                if not self.status:
                    continue

                if not self.data_ready:
                    continue

                req: SubscribeRequest = self.subscribeRequest_queue.get(block=True, timeout=1)
                if req.exchange not in EXCHANGE_VT2IB:
                    self.gateway.write_log(f"订阅行情{req.symbol}失败，不支持的交易所{req.exchange}")
                    continue

                # 过滤重复订阅
                if req.vt_symbol in self.subscribed:
                    continue
                self.subscribed[req.vt_symbol] = req

                # 解析IB合约详情
                ib_contract: Contract = generate_ib_contract(req.symbol, req.exchange)
                if not ib_contract:
                    self.gateway.write_log("订阅行情{req.symbol}失败。代码解析失败，请检查格式是否正确")
                    continue

                # 通过TWS查询合约信息
                self.reqid += 1
                self.client.reqContractDetails(self.reqid, ib_contract)

                #  订阅tick数据并创建tick对象缓冲区
                self.reqid += 1
                self.gateway.write_log(f"api订阅前reqid：{self.reqid},symbol:{req.symbol}")
                self.client.reqMktData(self.reqid, ib_contract, "", False, False, [])

                tick: TickData = TickData(
                    symbol=req.symbol,
                    exchange=req.exchange,
                    datetime=datetime.now(LOCAL_TZ),
                    gateway_name=self.gateway_name,
                )
                self.ticks[self.reqid] = tick
                self.gateway.write_log(f"api订阅后reqid：{self.reqid},symbol:{req.symbol}")
                self.tick_exchange[self.reqid] = req.exchange

            except Empty:
                pass

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        if not self.status:
            return ""
        if not self.order_ready:
            self.gateway.write_log(f"API还没有完全初始化完毕,还没有收到nextValidID,暂不能下单。symbol:{req.vt_symbol},direction:{req.direction},price:{req.price},volume:{req.volume}")
            return ""

        if req.exchange not in EXCHANGE_VT2IB:
            self.gateway.write_log(f"不支持的交易所：{req.exchange}")
            return ""

        if req.type not in ORDERTYPE_VT2IB:
            self.gateway.write_log(f"不支持的价格类型：{req.type}")
            return ""

        self.orderid += 1

        ib_contract: Contract = generate_ib_contract(req.symbol, req.exchange)
        if not ib_contract:
            return ""

        ib_order: Order = Order()
        ib_order.orderId = self.orderid
        ib_order.clientId = self.clientid
        ib_order.action = DIRECTION_VT2IB[req.direction]
        ib_order.orderType = ORDERTYPE_VT2IB[req.type]
        ib_order.totalQuantity = req.volume
        ib_order.account = self.account

        # 非常规交易时间
        ib_order.outsideRth = True

        if req.type == OrderType.LIMIT:
            ib_order.lmtPrice = req.price
        elif req.type == OrderType.STOP:
            ib_order.auxPrice = req.price

        self.client.placeOrder(self.orderid, ib_contract, ib_order)
        self.client.reqIds(1)

        order: OrderData = req.create_order_data(str(self.orderid), self.gateway_name)
        order.datetime = datetime.now(LOCAL_TZ)
        
        self.gateway.on_order(order)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        if not self.status:
            return
        if not self.order_ready:
            self.gateway.write_log(f"API还没有完全初始化完毕,还没有收到nextValidID,暂不能撤单。oderid:{req.orderid},symbol:{req.vt_symbol},direction:{req.direction},price:{req.price},volume:{req.volume}")
            return ""

        # IB API 10.9.1在撤单时，新增1个参数，撤单时间
        manualCancelOrderTime:str = datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H:%M:%S")
        self.client.cancelOrder(int(req.orderid), manualCancelOrderTime)

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """查询历史数据"""
        contract: ContractData = self.contracts[req.vt_symbol]
        if not contract:
            self.gateway.write_log(f"找不到合约：{req.vt_symbol}，请先订阅")
            return []

        self.history_req = req

        self.reqid += 1

        ib_contract: Contract = generate_ib_contract(req.symbol, req.exchange)

        if req.end:
            end: datetime = req.end
            end_str: str = end.strftime("%Y%m%d %H:%M:%S")
        else:
            end: datetime = datetime.now(LOCAL_TZ)
            end_str: str = ""

        delta: timedelta = end - req.start
        days: int = min(delta.days, 180)     # IB 只提供6个月数据
        duration: str = f"{days} D"
        bar_size: str = INTERVAL_VT2IB[req.interval]

        if contract.product in [Product.SPOT, Product.FOREX]:
            bar_type: str = "MIDPOINT"
        else:
            bar_type: str = "TRADES"

        self.history_reqid = self.reqid
        self.client.reqHistoricalData(
            self.reqid,
            ib_contract,
            end_str,
            duration,
            bar_size,
            bar_type,
            0,
            1,
            False,
            []
        )

        self.history_condition.acquire()    # 等待异步数据返回
        self.history_condition.wait()
        self.history_condition.release()

        history: List[BarData] = self.history_buf
        self.history_buf: List[BarData] = []       # 创新新的缓冲列表
        self.history_req: HistoryRequest = None

        return history

    def load_contract_data(self) -> None:
        """加载本地合约数据"""
        f = shelve.open(self.data_filepath)
        self.contracts = f.get("contracts", {})
        self.ib_contracts_details = f.get("ib_contracts_details", {})
        f.close()

        for contract in self.contracts.values():
            self.gateway.on_contract(contract)

        self.gateway.write_log("本地缓存合约信息加载成功")

    def save_contract_data(self) -> None:
        """保存合约数据至本地"""
        f = shelve.open(self.data_filepath)
        f["contracts"] = self.contracts
        f["ib_contracts_details"] = self.ib_contracts_details
        f.close()

    def save_ib_contracts_details_to_csv(self) -> None:
        """保存ib的合约信息文件夹,用户可读,但这个csv文件,不读回系统"""
        folder_path = str(get_folder_path("contracts_info"))
        contracts_info_filepath: str = folder_path + "\\ib_contracts_info.csv"
        
        header = [  'conId',
                    'symbol',
                    'secType',
                    'lastTradeDateOrContractMonth',
                    'strike',
                    'right',
                    'multiplier',
                    'exchange',
                    'primaryExchange',
                    'currency',
                    'localSymbol',
                    'tradingClass',
                    'includeExpired',
                    'secIdType',
                    'secId',
                    'description',
                    'issuerId',
                    'comboLegsDescrip',
                    'comboLegs',
                    'deltaNeutralContract',
                    'marketName',
                    'minTick',
                    'orderTypes',
                    'validExchanges',
                    'priceMagnifier',
                    'underConId',
                    'longName',
                    'contractMonth',
                    'industry',
                    'category',
                    'subcategory',
                    'timeZoneId',
                    'tradingHours',
                    'liquidHours',
                    'evRule',
                    'evMultiplier',
                    'aggGroup',
                    'underSymbol',
                    'underSecType',
                    'marketRuleIds',
                    'secIdList',
                    'realExpirationDate',
                    'lastTradeTime',
                    'stockType',
                    'minSize',
                    'sizeIncrement',
                    'suggestedSizeIncrement',
                    'cusip',
                    'ratings',
                    'descAppend',
                    'bondType',
                    'couponType',
                    'callable',
                    'putable',
                    'coupon',
                    'convertible',
                    'maturity',
                    'issueDate',
                    'nextOptionDate',
                    'nextOptionType',
                    'nextOptionPartial',
                    'notes'
                  ]
        data_list = []
        for key,value in self.ib_contracts_details.items():
            data_list.append(self.get_ib_contracts_details_str(value))
        
        df = pd.DataFrame(columns=header, data=data_list)
        df.to_csv(contracts_info_filepath, header=True, index=False)
    
    def get_ib_contracts_details_str(self, c:ContractDetails) -> List:
        str_list = []
        
        str_list.append(str(c.contract.conId))
        str_list.append(str(c.contract.symbol))
        str_list.append(str(c.contract.secType))
        str_list.append(str(c.contract.lastTradeDateOrContractMonth))
        str_list.append(floatMaxString(c.contract.strike))
        str_list.append(str(c.contract.right))
        str_list.append(str(c.contract.multiplier))
        str_list.append(str(c.contract.exchange))
        str_list.append(str(c.contract.primaryExchange))
        str_list.append(str(c.contract.currency))
        str_list.append(str(c.contract.localSymbol))
        str_list.append(str(c.contract.tradingClass))
        str_list.append(str(c.contract.includeExpired))
        str_list.append(str(c.contract.secIdType))
        str_list.append(str(c.contract.secId))
        str_list.append(str(c.contract.description))
        str_list.append(str(c.contract.issuerId))
        str_list.append("combo:" + c.contract.comboLegsDescrip)

        comboLegs = ""
        if c.contract.comboLegs:
            for leg in c.contract.comboLegs:
                comboLegs += ";" + str(leg)
        str_list.append(comboLegs)
    
        deltaNeutralContract = ""
        if c.contract.deltaNeutralContract:
            deltaNeutralContract += ";" + str(c.contract.deltaNeutralContract)
        str_list.append(deltaNeutralContract)

        str_list.append(str(c.marketName))
        str_list.append(floatMaxString(c.minTick))
        str_list.append(str(c.orderTypes))
        str_list.append(str(c.validExchanges))
        str_list.append(intMaxString(c.priceMagnifier))
        str_list.append(intMaxString(c.underConId))
        str_list.append(str(c.longName))
        str_list.append(str(c.contractMonth))
        str_list.append(str(c.industry))
        str_list.append(str(c.category))
        str_list.append(str(c.subcategory))
        str_list.append(str(c.timeZoneId))
        str_list.append(str(c.tradingHours))
        str_list.append(str(c.liquidHours))
        str_list.append(str(c.evRule))
        str_list.append(intMaxString(c.evMultiplier))
        str_list.append(intMaxString(c.aggGroup))
        str_list.append(str(c.underSymbol))
        str_list.append(str(c.underSecType))
        str_list.append(str(c.marketRuleIds))       
        str_list.append(str(c.secIdList))
        str_list.append(str(c.realExpirationDate))
        str_list.append(str(c.lastTradeTime))
        str_list.append(str(c.stockType))
        str_list.append(decimalMaxString(c.minSize))
        str_list.append(decimalMaxString(c.sizeIncrement))
        str_list.append(decimalMaxString(c.suggestedSizeIncrement))
        str_list.append(str(c.cusip))
        str_list.append(str(c.ratings))
        str_list.append(str(c.descAppend))
        str_list.append(str(c.bondType))
        str_list.append(str(c.couponType))
        str_list.append(str(c.callable))
        str_list.append(str(c.putable))
        str_list.append(str(c.coupon))
        str_list.append(str(c.convertible))
        str_list.append(str(c.maturity))
        str_list.append(str(c.issueDate))
        str_list.append(str(c.nextOptionDate))
        str_list.append(str(c.nextOptionType))
        str_list.append(str(c.nextOptionPartial))
        str_list.append(str(c.notes))     

        return str_list


def generate_ib_contract(symbol: str, exchange: Exchange) -> Optional[Contract]:
    """生产IB合约"""
    try:
        fields: list = symbol.split(JOIN_SYMBOL)

        ib_contract: Contract = Contract()
        ib_contract.exchange = EXCHANGE_VT2IB[exchange]
        ib_contract.secType = fields[-1]
        ib_contract.currency = fields[-2]
        ib_contract.symbol = fields[0]

        if ib_contract.secType in ["FUT", "OPT", "FOP"]:
            ib_contract.lastTradeDateOrContractMonth = fields[1]

        if ib_contract.secType == "FUT":
            if len(fields) == 5:
                ib_contract.multiplier = int(fields[2])

        if ib_contract.secType in ["OPT", "FOP"]:
            ib_contract.right = fields[2]
            ib_contract.strike = float(fields[3])
            ib_contract.multiplier = int(fields[4])
    except IndexError:
        ib_contract = None

    return ib_contract


def generate_symbol(ib_contract: Contract) -> str:
    """生成vnpy代码"""
    fields: list = [ib_contract.symbol]

    if ib_contract.secType in ["FUT", "OPT", "FOP"]:
        fields.append(ib_contract.lastTradeDateOrContractMonth)

    if ib_contract.secType in ["OPT", "FOP"]:
        fields.append(ib_contract.right)
        fields.append(str(ib_contract.strike))
        fields.append(str(ib_contract.multiplier))

    fields.append(ib_contract.currency)
    fields.append(ib_contract.secType)

    symbol: str = JOIN_SYMBOL.join(fields)

    return symbol

def generate_localtime(str_datetime: str) -> datetime | None:
    # 把"20230406 09:39:00 Hongkong" 变成 本地时区的 时间
    strTimeList = str_datetime.split(" ")
    if len(strTimeList) > 2:
        bar_tz = pytz.timezone(strTimeList[2])
        dt: datetime = bar_tz.localize(datetime.strptime(strTimeList[0] + " " + strTimeList[1], "%Y%m%d %H:%M:%S"))
        
        dt_local = dt.astimezone(pytz.timezone("Asia/Shanghai"))
        return dt_local
    else:
        return None