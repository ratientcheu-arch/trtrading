//+------------------------------------------------------------------+
//|                                                   ZmqBridge.mq5  |
//|  Phase 2 — full ZeroMQ bridge for headless bot communication.     |
//|                                                                   |
//|  - PUB socket tcp://*:5555  → ticks broadcast for subscribed syms |
//|  - REP socket tcp://*:5556  → RPC from Python bot (orders, query) |
//|                                                                   |
//|  Protocol (pipe-delimited text; no JSON dependency in MQL5):      |
//|    Request  = "method:arg1=v1;arg2=v2;..."                        |
//|    Response = "OK:k1=v1;k2=v2" | "ERR:message"                    |
//|    Tick PUB = "TICK|SYMBOL|bid|ask|time_msc"                      |
//|                                                                   |
//|  Hedging/pyramiding guards enforced in the EA:                    |
//|    - opposite-direction position on same symbol → rejected        |
//|    - same-direction pyramid → rejected unless last position of    |
//|      that symbol/side has crossed ≥5% of its TP distance          |
//|    - max N positions per symbol (default 3)                       |
//+------------------------------------------------------------------+
#property copyright "TRTP SAS"
#property version   "0.2"
#property strict

#include <Zmq/Zmq.mqh>
#include <Trade/Trade.mqh>
#include <Trade/PositionInfo.mqh>
#include <Trade/DealInfo.mqh>

//--- Inputs -----------------------------------------------------------
input string  PUB_ENDPOINT        = "tcp://*:5555";  // ticks PUB bind
input string  REP_ENDPOINT        = "tcp://*:5556";  // orders REP bind
input int     HeartbeatSec        = 30;
input int     PollMs              = 50;              // REP poll interval
input long    MagicNumber         = 20260414;        // orders magic
input int     MaxPositionsPerSym  = 3;               // pyramiding cap
input double  PyramidTpPct        = 0.01;            // 1% of TP distance (2026-04-24: 5%→1% pour permettre 2 ouvertures rapides)
input bool    BlockHedging        = true;            // reject opposite direction
input bool    RequireSlTp         = true;            // reject orders without SL/TP
//--- Trailing stop inputs (progressive step trail) --------------------
// 2026-04-24: config relaxée après backtest 31 trades → +€272 vs actuel +€61
// TrailStartPct 0.15 → 0.30 : laisse la position respirer avant de trailer
// TrailGapPct   0.05 → 0.15 : 15% de marge pour absorber retracements normaux
// TrailExtendAtPct 0.95 → 0.90 : extend TP plus tôt
// TrailExtendLockPct 0.90 → 0.75 : lock 75% à l'extend (moins serré)
input bool    TrailingEnabled     = true;            // master switch
input double  TrailStartPct       = 0.30;            // start trailing at 30% TP progress
input double  TrailStepPct        = 0.05;            // step size = 5%
input double  TrailGapPct         = 0.15;            // SL locks 15% behind progress
input double  TrailExtendAtPct    = 0.90;            // at 90% TP → extend TP
input double  TrailExtendBy       = 0.10;            // push TP by 10% of initial dist
input double  TrailExtendLockPct  = 0.75;            // SL at 75% after extend
input int     TrailMinSecBetween  = 1;               // min sec between PositionModify

//--- Globals ----------------------------------------------------------
Context g_ctx("ZmqBridge");
Socket  g_pub(g_ctx, ZMQ_PUB);
Socket  g_rep(g_ctx, ZMQ_REP);
CTrade  g_trade;
CPositionInfo g_pos;
CDealInfo     g_deal;

// Subscribed symbols for PUB ticks (MT5 only calls OnTick for charts so we
// iterate on OnTimer through the sub list to publish all of them)
string   g_subs[];
datetime g_last_hb     = 0;
datetime g_last_tick_pub = 0;
datetime g_last_trail  = 0;

// Per-position trailing state: stores initial TP distance + flag "extended"
// Keyed by ticket. MQL5 has no hashmap; we use parallel arrays kept small.
ulong    g_trail_tickets[];
double   g_trail_init_dist[];   // abs(tp_init - entry)
bool     g_trail_extended[];    // true once we've pushed TP at 95%

//+------------------------------------------------------------------+
//| Small string helpers (MQL5 has no dict / regex)                   |
//+------------------------------------------------------------------+
string arg_get(const string args, const string key, const string def = "")
{
    string needle = key + "=";
    int start = StringFind(args, needle);
    if (start < 0) return def;
    start += StringLen(needle);
    int end = StringFind(args, ";", start);
    if (end < 0) end = StringLen(args);
    return StringSubstr(args, start, end - start);
}

double arg_getd(const string args, const string key, const double def = 0.0)
{
    string s = arg_get(args, key, "");
    if (StringLen(s) == 0) return def;
    return StringToDouble(s);
}

long arg_getl(const string args, const string key, const long def = 0)
{
    string s = arg_get(args, key, "");
    if (StringLen(s) == 0) return def;
    return (long)StringToInteger(s);
}

string quote_csv(const string s)
{
    // Escape ; and | so they don't collide with delimiters
    string r = s;
    StringReplace(r, ";", ",");
    StringReplace(r, "|", "/");
    return r;
}

//+------------------------------------------------------------------+
//| OnInit                                                            |
//+------------------------------------------------------------------+
int OnInit()
{
    // PUB socket
    if (!g_pub.bind(PUB_ENDPOINT)) {
        Print("[ZmqBridge] FATAL: PUB bind failed ", PUB_ENDPOINT);
        return INIT_FAILED;
    }
    g_pub.setLinger(0);
    g_pub.setSendHighWaterMark(1000);

    // REP socket (non-blocking via recv timeout=0)
    if (!g_rep.bind(REP_ENDPOINT)) {
        Print("[ZmqBridge] FATAL: REP bind failed ", REP_ENDPOINT);
        return INIT_FAILED;
    }
    g_rep.setLinger(0);
    g_rep.setReceiveTimeout(0);
    g_rep.setSendTimeout(1000);

    // trade config
    g_trade.SetExpertMagicNumber((ulong)MagicNumber);
    g_trade.SetAsyncMode(false);
    g_trade.SetTypeFillingBySymbol(_Symbol);
    g_trade.SetDeviationInPoints(20);

    EventSetMillisecondTimer(PollMs);

    Print("[ZmqBridge] ready. PUB=", PUB_ENDPOINT, " REP=", REP_ENDPOINT,
          " login=", AccountInfoInteger(ACCOUNT_LOGIN),
          " balance=", AccountInfoDouble(ACCOUNT_BALANCE),
          " ", AccountInfoString(ACCOUNT_CURRENCY),
          " leverage=1:", AccountInfoInteger(ACCOUNT_LEVERAGE));

    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| OnDeinit                                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();
    g_pub.unbind(PUB_ENDPOINT);
    g_rep.unbind(REP_ENDPOINT);
    Print("[ZmqBridge] deinit reason=", reason);
}

//+------------------------------------------------------------------+
//| OnTimer: poll REP + publish ticks + heartbeat                     |
//+------------------------------------------------------------------+
void OnTimer()
{
    // Poll REP non-blocking
    ZmqMsg req;
    if (g_rep.recv(req, true)) {
        string payload = req.getData();
        string resp    = handle_request(payload);
        ZmqMsg rep_msg(resp);
        g_rep.send(rep_msg, true);  // don't block; REQ peer is waiting
    }

    // Publish ticks for subscribed symbols every poll (50ms).
    // OnTick() is only called for the chart symbol so we need our own loop.
    for (int i = 0; i < ArraySize(g_subs); ++i) publish_tick(g_subs[i]);

    // Trailing stop update (throttled to TrailMinSecBetween seconds)
    datetime now = TimeCurrent();
    if (TrailingEnabled && now - g_last_trail >= TrailMinSecBetween) {
        trailing_stop_update();
        g_last_trail = now;
    }

    // Heartbeat
    if (now - g_last_hb >= HeartbeatSec) {
        Print("[ZmqBridge] hb balance=", AccountInfoDouble(ACCOUNT_BALANCE),
              " equity=", AccountInfoDouble(ACCOUNT_EQUITY),
              " positions=", PositionsTotal(),
              " subs=", ArraySize(g_subs),
              " connected=", (bool)TerminalInfoInteger(TERMINAL_CONNECTED));
        g_last_hb = now;
    }
}

//+------------------------------------------------------------------+
//| OnTick                                                            |
//+------------------------------------------------------------------+
void OnTick()
{
    // Chart symbol always published (may be duplicate with OnTimer loop)
    publish_tick(_Symbol);
}

//+------------------------------------------------------------------+
//| publish_tick                                                      |
//+------------------------------------------------------------------+
void publish_tick(const string symbol)
{
    MqlTick t;
    if (!SymbolInfoTick(symbol, t)) return;
    int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
    string payload = StringFormat("TICK|%s|%s|%s|%I64d",
                                  symbol,
                                  DoubleToString(t.bid, digits),
                                  DoubleToString(t.ask, digits),
                                  t.time_msc);
    ZmqMsg m(payload);
    g_pub.send(m, true);  // true = don't block
}

//+------------------------------------------------------------------+
//| Dispatcher                                                        |
//+------------------------------------------------------------------+
string handle_request(const string req)
{
    // Split on first ":"
    int colon = StringFind(req, ":");
    string method = (colon >= 0) ? StringSubstr(req, 0, colon) : req;
    string args   = (colon >= 0) ? StringSubstr(req, colon + 1) : "";

    if (method == "ping")           return rpc_ping(args);
    if (method == "account")        return rpc_account(args);
    if (method == "positions")      return rpc_positions(args);
    if (method == "place_order")    return rpc_place_order(args);
    if (method == "close")          return rpc_close_position(args);
    if (method == "close_all")      return rpc_close_all(args);
    if (method == "modify_sltp")    return rpc_modify_sltp(args);
    if (method == "subscribe")      return rpc_subscribe(args);
    if (method == "unsubscribe")    return rpc_unsubscribe(args);
    if (method == "quote")          return rpc_quote(args);
    if (method == "bars")           return rpc_bars(args);
    if (method == "deals_by_pos")   return rpc_deals_by_position(args);
    if (method == "deals")          return rpc_deals(args);
    if (method == "symbols")        return rpc_symbols(args);

    return "ERR:unknown_method=" + method;
}

//+------------------------------------------------------------------+
//| RPC: ping                                                         |
//+------------------------------------------------------------------+
string rpc_ping(const string args)
{
    return StringFormat("OK:pong=true;time_ms=%I64d", (long)TimeGMT() * 1000);
}

//+------------------------------------------------------------------+
//| RPC: account                                                      |
//+------------------------------------------------------------------+
string rpc_account(const string args)
{
    return StringFormat(
        "OK:login=%I64d;balance=%.2f;equity=%.2f;margin=%.2f;free_margin=%.2f;"
        "currency=%s;leverage=%I64d;profit=%.2f;margin_level=%.2f;connected=%d",
        AccountInfoInteger(ACCOUNT_LOGIN),
        AccountInfoDouble(ACCOUNT_BALANCE),
        AccountInfoDouble(ACCOUNT_EQUITY),
        AccountInfoDouble(ACCOUNT_MARGIN),
        AccountInfoDouble(ACCOUNT_MARGIN_FREE),
        AccountInfoString(ACCOUNT_CURRENCY),
        AccountInfoInteger(ACCOUNT_LEVERAGE),
        AccountInfoDouble(ACCOUNT_PROFIT),
        AccountInfoDouble(ACCOUNT_MARGIN_LEVEL),
        (int)TerminalInfoInteger(TERMINAL_CONNECTED));
}

//+------------------------------------------------------------------+
//| RPC: positions (multi-position separated by "|")                  |
//+------------------------------------------------------------------+
string rpc_positions(const string args)
{
    int total = PositionsTotal();
    if (total == 0) return "OK:count=0";

    string out = StringFormat("OK:count=%d", total);
    for (int i = 0; i < total; ++i) {
        if (!g_pos.SelectByIndex(i)) continue;
        out += "|";
        out += StringFormat(
            "ticket=%I64d;symbol=%s;side=%s;volume=%.2f;price=%.5f;sl=%.5f;tp=%.5f;"
            "pnl=%.2f;swap=%.2f;magic=%I64d;time=%I64d",
            g_pos.Ticket(),
            g_pos.Symbol(),
            (g_pos.PositionType() == POSITION_TYPE_BUY ? "buy" : "sell"),
            g_pos.Volume(),
            g_pos.PriceOpen(),
            g_pos.StopLoss(),
            g_pos.TakeProfit(),
            g_pos.Profit(),
            g_pos.Swap(),
            g_pos.Magic(),
            (long)g_pos.Time());
    }
    return out;
}

//+------------------------------------------------------------------+
//| Hedging / pyramiding pre-flight check                             |
//| returns "" if ok, or "reason" string if must reject                |
//+------------------------------------------------------------------+
string check_hedging_pyramid(const string symbol, const ENUM_POSITION_TYPE new_side)
{
    int same_side = 0;
    int total = PositionsTotal();
    ulong  last_ticket_same = 0;
    double last_open_same   = 0.0;
    double last_tp_same     = 0.0;

    for (int i = 0; i < total; ++i) {
        if (!g_pos.SelectByIndex(i)) continue;
        if (g_pos.Symbol() != symbol) continue;

        if (g_pos.PositionType() != new_side) {
            if (BlockHedging) return "hedging_blocked:opposite_position_exists";
            continue;
        }
        // same side: track most-recent for pyramiding rule
        same_side++;
        if ((long)g_pos.Time() > (long)last_ticket_same) {
            last_ticket_same = g_pos.Ticket();
            last_open_same   = g_pos.PriceOpen();
            last_tp_same     = g_pos.TakeProfit();
        }
    }

    if (same_side == 0) return "";  // fresh entry, all good

    if (same_side >= MaxPositionsPerSym)
        return StringFormat("pyramid_blocked:max_%d_reached", MaxPositionsPerSym);

    // Check 5% of TP distance progress on the most recent same-side position
    if (last_tp_same == 0.0)
        return "pyramid_blocked:last_position_has_no_tp";

    MqlTick t;
    if (!SymbolInfoTick(symbol, t)) return "pyramid_blocked:no_tick_data";

    double cur_price = (new_side == POSITION_TYPE_BUY) ? t.bid : t.ask;
    double tp_dist   = MathAbs(last_tp_same - last_open_same);
    double progress  = (new_side == POSITION_TYPE_BUY)
                       ? (cur_price - last_open_same)
                       : (last_open_same - cur_price);

    if (tp_dist <= 0.0) return "pyramid_blocked:invalid_tp_distance";
    if (progress < tp_dist * PyramidTpPct)
        return StringFormat("pyramid_blocked:progress=%.2f%%_required=%.0f%%",
                            progress / tp_dist * 100.0,
                            PyramidTpPct * 100.0);

    return "";  // pyramid OK
}

//+------------------------------------------------------------------+
//| RPC: place_order                                                  |
//+------------------------------------------------------------------+
string rpc_place_order(const string args)
{
    string symbol = arg_get(args, "symbol", "");
    string side   = arg_get(args, "side", "");
    double volume = arg_getd(args, "volume", 0.0);
    double sl     = arg_getd(args, "sl", 0.0);
    double tp     = arg_getd(args, "tp", 0.0);
    string comment = arg_get(args, "comment", "bot");

    if (StringLen(symbol) == 0) return "ERR:missing=symbol";
    if (volume <= 0.0)          return "ERR:invalid=volume";
    if (side != "buy" && side != "sell") return "ERR:invalid=side";
    if (RequireSlTp && (sl == 0.0 || tp == 0.0))
        return "ERR:sl_tp_required";

    if (!SymbolSelect(symbol, true))
        return "ERR:symbol_not_found=" + symbol;

    ENUM_POSITION_TYPE new_side = (side == "buy") ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;

    // Hedging + pyramiding pre-flight
    string veto = check_hedging_pyramid(symbol, new_side);
    if (StringLen(veto) > 0)
        return "ERR:" + veto;

    // Use CTrade wrapper
    g_trade.SetTypeFillingBySymbol(symbol);
    bool ok;
    if (side == "buy") ok = g_trade.Buy(volume, symbol, 0.0, sl, tp, comment);
    else               ok = g_trade.Sell(volume, symbol, 0.0, sl, tp, comment);

    if (!ok) {
        uint retcode = g_trade.ResultRetcode();
        string reason = g_trade.ResultRetcodeDescription();
        return StringFormat("ERR:place_failed;retcode=%u;reason=%s",
                            retcode, quote_csv(reason));
    }

    return StringFormat("OK:ticket=%I64d;price=%.5f;volume=%.2f;deal=%I64d",
                        g_trade.ResultOrder(),
                        g_trade.ResultPrice(),
                        g_trade.ResultVolume(),
                        g_trade.ResultDeal());
}

//+------------------------------------------------------------------+
//| RPC: close                                                        |
//+------------------------------------------------------------------+
string rpc_close_position(const string args)
{
    long ticket = arg_getl(args, "ticket", 0);
    if (ticket == 0) return "ERR:missing=ticket";

    if (!g_pos.SelectByTicket((ulong)ticket))
        return "ERR:position_not_found";

    string symbol = g_pos.Symbol();
    double volume_before = g_pos.Volume();
    double price_before  = g_pos.PriceOpen();
    double pnl_before    = g_pos.Profit() + g_pos.Swap() + g_pos.Commission();

    bool ok = g_trade.PositionClose((ulong)ticket, 20);
    if (!ok) {
        uint retcode = g_trade.ResultRetcode();
        return StringFormat("ERR:close_failed;retcode=%u;reason=%s",
                            retcode, quote_csv(g_trade.ResultRetcodeDescription()));
    }

    return StringFormat("OK:ticket=%I64d;symbol=%s;volume=%.2f;pnl=%.2f;close_price=%.5f",
                        ticket, symbol, volume_before, pnl_before, g_trade.ResultPrice());
}

//+------------------------------------------------------------------+
//| RPC: close_all                                                    |
//+------------------------------------------------------------------+
string rpc_close_all(const string args)
{
    string symbol_filter = arg_get(args, "symbol", "");
    int closed = 0, failed = 0;
    int total = PositionsTotal();
    for (int i = total - 1; i >= 0; --i) {
        if (!g_pos.SelectByIndex(i)) continue;
        if (StringLen(symbol_filter) > 0 && g_pos.Symbol() != symbol_filter) continue;
        if (g_trade.PositionClose(g_pos.Ticket(), 20)) closed++;
        else                                           failed++;
    }
    return StringFormat("OK:closed=%d;failed=%d", closed, failed);
}

//+------------------------------------------------------------------+
//| RPC: modify_sltp                                                  |
//+------------------------------------------------------------------+
string rpc_modify_sltp(const string args)
{
    long   ticket = arg_getl(args, "ticket", 0);
    double sl     = arg_getd(args, "sl", 0.0);
    double tp     = arg_getd(args, "tp", 0.0);
    if (ticket == 0) return "ERR:missing=ticket";

    if (!g_pos.SelectByTicket((ulong)ticket))
        return "ERR:position_not_found";

    // 0.0 means "keep current" per our convention
    if (sl == 0.0) sl = g_pos.StopLoss();
    if (tp == 0.0) tp = g_pos.TakeProfit();

    bool ok = g_trade.PositionModify((ulong)ticket, sl, tp);
    if (!ok) {
        uint retcode = g_trade.ResultRetcode();
        return StringFormat("ERR:modify_failed;retcode=%u;reason=%s",
                            retcode, quote_csv(g_trade.ResultRetcodeDescription()));
    }
    return StringFormat("OK:ticket=%I64d;sl=%.5f;tp=%.5f", ticket, sl, tp);
}

//+------------------------------------------------------------------+
//| RPC: subscribe                                                    |
//+------------------------------------------------------------------+
string rpc_subscribe(const string args)
{
    string symbol = arg_get(args, "symbol", "");
    if (StringLen(symbol) == 0) return "ERR:missing=symbol";
    if (!SymbolSelect(symbol, true))
        return "ERR:symbol_not_found=" + symbol;

    // Dedup
    for (int i = 0; i < ArraySize(g_subs); ++i)
        if (g_subs[i] == symbol) return "OK:already_subscribed=" + symbol;

    int n = ArraySize(g_subs);
    ArrayResize(g_subs, n + 1);
    g_subs[n] = symbol;
    return "OK:subscribed=" + symbol + ";count=" + IntegerToString(n + 1);
}

string rpc_unsubscribe(const string args)
{
    string symbol = arg_get(args, "symbol", "");
    int n = ArraySize(g_subs);
    for (int i = 0; i < n; ++i) {
        if (g_subs[i] == symbol) {
            for (int j = i; j < n - 1; ++j) g_subs[j] = g_subs[j+1];
            ArrayResize(g_subs, n - 1);
            return "OK:unsubscribed=" + symbol;
        }
    }
    return "OK:not_subscribed=" + symbol;
}

//+------------------------------------------------------------------+
//| RPC: quote                                                        |
//+------------------------------------------------------------------+
string rpc_quote(const string args)
{
    string symbol = arg_get(args, "symbol", "");
    if (StringLen(symbol) == 0) return "ERR:missing=symbol";
    MqlTick t;
    if (!SymbolInfoTick(symbol, t)) return "ERR:no_tick=" + symbol;
    int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
    return StringFormat("OK:symbol=%s;bid=%s;ask=%s;time_ms=%I64d;digits=%d",
                        symbol,
                        DoubleToString(t.bid, digits),
                        DoubleToString(t.ask, digits),
                        t.time_msc, digits);
}

//+------------------------------------------------------------------+
//| RPC: bars (historical OHLCV)                                      |
//+------------------------------------------------------------------+
string rpc_bars(const string args)
{
    string symbol     = arg_get(args, "symbol", "");
    string tf_str     = arg_get(args, "timeframe", "M1");
    long   count      = arg_getl(args, "count", 100);
    if (StringLen(symbol) == 0) return "ERR:missing=symbol";
    if (count <= 0 || count > 5000) return "ERR:invalid=count";

    ENUM_TIMEFRAMES tf = PERIOD_M1;
    if (tf_str == "M1")  tf = PERIOD_M1;
    else if (tf_str == "M5")  tf = PERIOD_M5;
    else if (tf_str == "M15") tf = PERIOD_M15;
    else if (tf_str == "M30") tf = PERIOD_M30;
    else if (tf_str == "H1")  tf = PERIOD_H1;
    else if (tf_str == "H4")  tf = PERIOD_H4;
    else if (tf_str == "D1")  tf = PERIOD_D1;
    else if (tf_str == "W1")  tf = PERIOD_W1;
    else return "ERR:unknown_timeframe=" + tf_str;

    MqlRates rates[];
    int got = CopyRates(symbol, tf, 0, (int)count, rates);
    if (got <= 0) return "ERR:copy_rates_failed=" + symbol;

    int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
    string out = StringFormat("OK:count=%d", got);
    for (int i = 0; i < got; ++i) {
        out += "|";
        out += StringFormat("t=%I64d;o=%s;h=%s;l=%s;c=%s;v=%I64d",
                            (long)rates[i].time,
                            DoubleToString(rates[i].open, digits),
                            DoubleToString(rates[i].high, digits),
                            DoubleToString(rates[i].low, digits),
                            DoubleToString(rates[i].close, digits),
                            rates[i].tick_volume);
    }
    return out;
}

//+------------------------------------------------------------------+
//| RPC: deals_by_position                                            |
//+------------------------------------------------------------------+
string rpc_deals_by_position(const string args)
{
    long pos_id = arg_getl(args, "position_id", 0);
    if (pos_id == 0) return "ERR:missing=position_id";

    if (!HistorySelectByPosition((ulong)pos_id))
        return "ERR:history_select_failed";

    int n = HistoryDealsTotal();
    string out = StringFormat("OK:count=%d", n);
    for (int i = 0; i < n; ++i) {
        if (!g_deal.SelectByIndex(i)) continue;
        out += "|";
        out += StringFormat("deal=%I64d;order=%I64d;ticket_pos=%I64d;symbol=%s;type=%d;"
                            "entry=%d;volume=%.2f;price=%.5f;profit=%.2f;commission=%.2f;"
                            "swap=%.2f;time=%I64d",
                            g_deal.Ticket(),
                            g_deal.Order(),
                            g_deal.PositionId(),
                            g_deal.Symbol(),
                            (int)g_deal.DealType(),
                            (int)g_deal.Entry(),
                            g_deal.Volume(),
                            g_deal.Price(),
                            g_deal.Profit(),
                            g_deal.Commission(),
                            g_deal.Swap(),
                            (long)g_deal.Time());
    }
    return out;
}

//+------------------------------------------------------------------+
//| RPC: deals (range by timestamp)                                   |
//+------------------------------------------------------------------+
string rpc_deals(const string args)
{
    long from_ts = arg_getl(args, "from_ts", 0);
    long to_ts   = arg_getl(args, "to_ts", 0);
    long max_rows = arg_getl(args, "max_rows", 1000);
    if (to_ts == 0) to_ts = (long)TimeCurrent();
    if (from_ts == 0) from_ts = to_ts - 86400;  // default: last 24h

    if (!HistorySelect((datetime)from_ts, (datetime)to_ts))
        return "ERR:history_select_failed";

    int n = HistoryDealsTotal();
    if (n > max_rows) n = (int)max_rows;
    string out = StringFormat("OK:count=%d", n);
    for (int i = 0; i < n; ++i) {
        if (!g_deal.SelectByIndex(i)) continue;
        out += "|";
        out += StringFormat("deal=%I64d;pos=%I64d;symbol=%s;type=%d;entry=%d;"
                            "volume=%.2f;price=%.5f;profit=%.2f;time=%I64d",
                            g_deal.Ticket(),
                            g_deal.PositionId(),
                            g_deal.Symbol(),
                            (int)g_deal.DealType(),
                            (int)g_deal.Entry(),
                            g_deal.Volume(),
                            g_deal.Price(),
                            g_deal.Profit(),
                            (long)g_deal.Time());
    }
    return out;
}

//+------------------------------------------------------------------+
//| RPC: symbols (list tradable symbols)                              |
//+------------------------------------------------------------------+
string rpc_symbols(const string args)
{
    int total = SymbolsTotal(true);  // true = in MarketWatch only
    string out = StringFormat("OK:count=%d", total);
    for (int i = 0; i < total; ++i) {
        string s = SymbolName(i, true);
        out += "|" + s;
    }
    return out;
}

//+==================================================================+
//|  TRAILING STOP                                                    |
//|  Rules (from user, 2026-04-14):                                   |
//|    - Start trailing at 15 % progress toward TP                    |
//|    - Lock SL 5 % behind current progress (floored to 5 % step)    |
//|      i.e. at 15 % → SL 10 %, at 20 % → SL 15 %, ... 90 % → SL 85 %|
//|    - At 95 % progress: push TP +5 % of initial distance, lock SL  |
//|      at 90 % of initial distance (one-shot, then resume normal)   |
//|    - SL only moves in favorable direction (never retreat)         |
//+==================================================================+

int trail_state_find(const ulong ticket)
{
    for (int i = 0; i < ArraySize(g_trail_tickets); ++i)
        if (g_trail_tickets[i] == ticket) return i;
    return -1;
}

void trail_state_add(const ulong ticket, const double init_dist)
{
    int n = ArraySize(g_trail_tickets);
    ArrayResize(g_trail_tickets,  n + 1);
    ArrayResize(g_trail_init_dist, n + 1);
    ArrayResize(g_trail_extended, n + 1);
    g_trail_tickets[n]   = ticket;
    g_trail_init_dist[n] = init_dist;
    g_trail_extended[n]  = false;
}

void trail_state_remove(const int idx)
{
    int n = ArraySize(g_trail_tickets);
    if (idx < 0 || idx >= n) return;
    for (int j = idx; j < n - 1; ++j) {
        g_trail_tickets[j]   = g_trail_tickets[j+1];
        g_trail_init_dist[j] = g_trail_init_dist[j+1];
        g_trail_extended[j]  = g_trail_extended[j+1];
    }
    ArrayResize(g_trail_tickets,  n - 1);
    ArrayResize(g_trail_init_dist, n - 1);
    ArrayResize(g_trail_extended, n - 1);
}

// Prune trail state for tickets that are no longer open
void trail_state_prune()
{
    int i = 0;
    while (i < ArraySize(g_trail_tickets)) {
        if (!PositionSelectByTicket(g_trail_tickets[i])) {
            trail_state_remove(i);
        } else {
            i++;
        }
    }
}

void trailing_stop_update()
{
    trail_state_prune();

    int total = PositionsTotal();
    for (int i = 0; i < total; ++i) {
        if (!g_pos.SelectByIndex(i)) continue;
        ulong ticket = g_pos.Ticket();
        string symbol = g_pos.Symbol();
        ENUM_POSITION_TYPE side = (ENUM_POSITION_TYPE)g_pos.PositionType();

        double entry = g_pos.PriceOpen();
        double cur_sl = g_pos.StopLoss();
        double cur_tp = g_pos.TakeProfit();
        if (cur_tp == 0.0) continue;  // no TP → no trailing

        // Get or create state for this ticket. Initial distance is captured
        // the first time we see this position (so TP extensions don't reset it).
        int idx = trail_state_find(ticket);
        if (idx < 0) {
            double d = MathAbs(cur_tp - entry);
            if (d <= 0.0) continue;
            trail_state_add(ticket, d);
            idx = trail_state_find(ticket);
        }
        double init_dist = g_trail_init_dist[idx];
        bool   extended  = g_trail_extended[idx];

        MqlTick t;
        if (!SymbolInfoTick(symbol, t)) continue;
        double cur_price = (side == POSITION_TYPE_BUY) ? t.bid : t.ask;

        // Progress as % of initial TP distance
        double signed_progress = (side == POSITION_TYPE_BUY)
                                 ? (cur_price - entry)
                                 : (entry - cur_price);
        double progress_pct = signed_progress / init_dist;
        if (progress_pct < TrailStartPct) continue;  // < 15 % → nothing yet

        double new_sl = cur_sl;
        double new_tp = cur_tp;

        if (progress_pct >= TrailExtendAtPct && !extended) {
            // 95 % rule: push TP by +5 % of INITIAL distance, lock SL at 90 %
            double tp_offset = init_dist * (1.0 + TrailExtendBy);  // 105 % of init
            double sl_offset = init_dist * TrailExtendLockPct;     // 90  % of init
            if (side == POSITION_TYPE_BUY) {
                new_tp = entry + tp_offset;
                new_sl = entry + sl_offset;
            } else {
                new_tp = entry - tp_offset;
                new_sl = entry - sl_offset;
            }
            g_trail_extended[idx] = true;
        } else {
            // Normal step trail: floor(progress/step)*step - gap
            double step       = TrailStepPct;
            double milestone  = MathFloor(progress_pct / step) * step;   // 0.15, 0.20, …, 0.90
            double sl_pct     = milestone - TrailGapPct;                 // always 5 % behind
            if (sl_pct < 0.0) continue;  // safety (shouldn't happen with start=0.15)
            double sl_offset  = init_dist * sl_pct;
            if (side == POSITION_TYPE_BUY) new_sl = entry + sl_offset;
            else                           new_sl = entry - sl_offset;
        }

        // Never move SL in the unfavorable direction
        if (side == POSITION_TYPE_BUY  && cur_sl > 0.0 && new_sl <= cur_sl) new_sl = cur_sl;
        if (side == POSITION_TYPE_SELL && cur_sl > 0.0 && new_sl >= cur_sl) new_sl = cur_sl;

        // Skip if nothing actually changed
        int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
        double pt  = SymbolInfoDouble(symbol, SYMBOL_POINT);
        if (MathAbs(new_sl - cur_sl) < pt && MathAbs(new_tp - cur_tp) < pt) continue;

        if (g_trade.PositionModify(ticket, new_sl, new_tp)) {
            PrintFormat("[ZmqBridge] trail ticket=%I64d progress=%.1f%% new_sl=%.*f new_tp=%.*f%s",
                        ticket, progress_pct * 100.0,
                        digits, new_sl, digits, new_tp,
                        g_trail_extended[idx] ? " (extended)" : "");
        } else {
            PrintFormat("[ZmqBridge] trail_FAIL ticket=%I64d retcode=%u reason=%s",
                        ticket, g_trade.ResultRetcode(),
                        g_trade.ResultRetcodeDescription());
        }
    }
}
