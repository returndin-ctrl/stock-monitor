#!/usr/bin/env python3
"""
台股監控 Web App（雲端版）
部署到 Railway，以技術指標＋價格位置綜合判斷買賣時機
"""

import sys, os, time, json, threading, logging, requests, schedule, pytz
import pandas as pd
import yfinance as yf
from datetime import datetime, time as dtime
from flask import Flask, jsonify, request, render_template

# ── 環境變數 ──────────────────────────────────────────────────────────
PORT       = int(os.environ.get('PORT', 8080))
DATA_DIR   = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(__file__), 'data'))
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')

os.makedirs(DATA_DIR, exist_ok=True)

PORTFOLIO_FILE = os.path.join(DATA_DIR, 'portfolio.json')
CONFIG_FILE    = os.path.join(DATA_DIR, 'config.json')

DEFAULT_CONFIG = {
    "ntfy_topic": "",
    "check_interval_minutes": 1,
    "buy_threshold": 5,
    "sell_threshold": 5,
    "stocks": {
        "2330": {
            "name": "台積電",
            "budget": 50000,
            "support_price": 1760,
            "resistance_price": 2180
        },
        "2408": {
            "name": "南亞科",
            "budget": 50000,
            "support_price": 198,
            "resistance_price": 249
        }
    }
}

if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)

# ── 日誌 ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='[%H:%M:%S]',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# 持倉管理
# ══════════════════════════════════════════════════════════════════════

TW_TZ         = pytz.timezone('Asia/Taipei')
BUY_FEE_RATE  = 0.001425
SELL_FEE_RATE = 0.001425
SELL_TAX_RATE = 0.003


def _load() -> dict:
    if not os.path.exists(PORTFOLIO_FILE):
        default = {"cash": 0, "realized_pnl": 0, "holdings": {}, "transactions": []}
        _pf_save(default)
        return default
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def _pf_save(data: dict):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _fee(amount: float, is_sell: bool = False) -> int:
    fee = max(1, int(amount * SELL_FEE_RATE))
    if is_sell:
        fee += int(amount * SELL_TAX_RATE)
    return fee


def _now_str() -> str:
    return datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M")


def load_config() -> dict:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def set_cash(amount: float) -> dict:
    data = _load()
    data["cash"] = amount
    _pf_save(data)
    return data


def pf_buy(stock_code: str, stock_name: str, shares: int, price: float) -> dict:
    data  = _load()
    gross = shares * price
    fee   = _fee(gross, is_sell=False)
    total = gross + fee
    if total > data["cash"]:
        raise ValueError(f"現金不足！需要 {total:,.0f} 元，帳戶只有 {data['cash']:,.0f} 元")
    h = data["holdings"].get(stock_code, {
        "name": stock_name, "shares": 0, "avg_cost": 0.0, "total_cost": 0.0
    })
    h["name"]       = stock_name
    h["total_cost"] = h["total_cost"] + gross
    h["shares"]    += shares
    h["avg_cost"]   = h["total_cost"] / h["shares"]
    data["holdings"][stock_code] = h
    data["cash"] -= total
    data["transactions"].append({
        "time": _now_str(), "action": "buy", "code": stock_code,
        "name": stock_name, "shares": shares, "price": price,
        "fee": fee, "total": total,
    })
    _pf_save(data)
    return {"action": "buy", "stock": f"{stock_name}（{stock_code}）",
            "shares": shares, "price": price, "fee": fee,
            "total": total, "cash_left": data["cash"]}


def pf_sell(stock_code: str, shares: int, price: float) -> dict:
    data = _load()
    h = data["holdings"].get(stock_code)
    if not h:
        raise ValueError(f"你沒有持有 {stock_code}")
    if shares > h["shares"]:
        raise ValueError(f"賣出股數（{shares}）超過持有（{h['shares']}）")
    gross    = shares * price
    fee      = _fee(gross, is_sell=True)
    net      = gross - fee
    avg_cost = h["avg_cost"]
    pnl      = (price - avg_cost) * shares - fee
    h["shares"]     -= shares
    h["total_cost"] -= avg_cost * shares
    if h["shares"] == 0:
        del data["holdings"][stock_code]
    else:
        h["avg_cost"] = h["total_cost"] / h["shares"]
        data["holdings"][stock_code] = h
    data["cash"]         += net
    data["realized_pnl"] += pnl
    stock_name = h.get("name", stock_code)
    data["transactions"].append({
        "time": _now_str(), "action": "sell", "code": stock_code,
        "name": stock_name, "shares": shares, "price": price,
        "avg_cost": avg_cost, "fee": fee, "net": net, "pnl": pnl,
    })
    _pf_save(data)
    return {"action": "sell", "stock": f"{stock_name}（{stock_code}）",
            "shares": shares, "price": price, "avg_cost": avg_cost,
            "fee": fee, "net": net, "pnl": pnl,
            "cash_left": data["cash"], "realized_pnl": data["realized_pnl"]}


def get_portfolio_summary(prices: dict) -> dict:
    data     = _load()
    cash     = data["cash"]
    realized = data["realized_pnl"]
    rows, stock_value, unrealized = [], 0.0, 0.0
    for code, h in data["holdings"].items():
        price    = prices.get(code)
        shares   = h["shares"]
        avg_cost = h["avg_cost"]
        if price is None:
            mkt_val, pnl, pnl_pct, price_s = shares*avg_cost, 0.0, 0.0, "N/A"
        else:
            mkt_val = shares * price
            pnl     = (price - avg_cost) * shares
            pnl_pct = (price - avg_cost) / avg_cost * 100
            price_s = f"{price:,.0f}"
        stock_value += mkt_val
        unrealized  += pnl
        rows.append({"code": code, "name": h.get("name", code),
                     "shares": shares, "avg_cost": avg_cost,
                     "price": price_s, "mkt_val": mkt_val,
                     "pnl": pnl, "pnl_pct": pnl_pct})
    return {"cash": cash, "stock_value": stock_value,
            "total_assets": cash + stock_value,
            "unrealized": unrealized, "realized": realized,
            "total_pnl": unrealized + realized, "rows": rows,
            "transactions": data["transactions"]}


# ══════════════════════════════════════════════════════════════════════
# 股價取得（即時盤中資訊）
# ══════════════════════════════════════════════════════════════════════

MARKET_OPEN  = dtime(9, 0)
MARKET_CLOSE = dtime(13, 30)


def is_market_hours() -> bool:
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _twse_intraday(code: str) -> dict | None:
    try:
        url = (f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
               f"?ex_ch=tse_{code}.tw&json=1&delay=0")
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        }, timeout=10)
        item = r.json()["msgArray"][0]
        def _f(k):
            v = item.get(k, "")
            return float(v) if v and v != "-" else None
        price = _f("z") or _f("y")
        prev  = _f("y")
        high  = _f("h")
        low   = _f("l")
        if not price or not prev:
            return None
        change_pct   = (price - prev) / prev * 100
        intraday_pos = ((price - low) / (high - low) * 100
                        if high and low and high > low else None)
        return {"price": price, "prev_close": prev, "open": _f("o"),
                "high": high, "low": low,
                "change_pct": change_pct, "intraday_pos": intraday_pos}
    except Exception:
        return None


def _yahoo_intraday(code: str) -> dict | None:
    try:
        hist = yf.Ticker(f"{code}.TW").history(period="2d", interval="1d")
        if len(hist) < 2:
            return None
        price = float(hist["Close"].iloc[-1])
        prev  = float(hist["Close"].iloc[-2])
        return {"price": price, "prev_close": prev, "open": None,
                "high": float(hist["High"].iloc[-1]),
                "low":  float(hist["Low"].iloc[-1]),
                "change_pct": (price - prev) / prev * 100,
                "intraday_pos": None}
    except Exception:
        return None


def get_intraday(code: str) -> dict | None:
    if is_market_hours():
        d = _twse_intraday(code)
        if d:
            return d
    return _yahoo_intraday(code)


# ══════════════════════════════════════════════════════════════════════
# 技術分析
# ══════════════════════════════════════════════════════════════════════

# 歷史資料快取（每小時更新一次，避免 yfinance 限流）
_hist_cache: dict[str, tuple[float, pd.DataFrame]] = {}  # code -> (timestamp, df)
HIST_CACHE_TTL = 3600  # 1 小時


def _fetch_history(code: str, current_price: float | None = None) -> pd.DataFrame | None:
    now = time.time()
    cached_ts, cached_df = _hist_cache.get(code, (0, None))
    if cached_df is not None and now - cached_ts < HIST_CACHE_TTL:
        df = cached_df.copy()
    else:
        try:
            df = yf.Ticker(f"{code}.TW").history(period="90d", interval="1d")
            if len(df) < 30:
                log.warning(f"  {code} 歷史資料不足（{len(df)} 筆）")
                return None
            _hist_cache[code] = (now, df.copy())
            log.info(f"  {code} 歷史資料更新（{len(df)} 筆）")
        except Exception as e:
            log.error(f"  {code} 歷史資料下載失敗：{e}")
            if cached_df is not None:
                log.info(f"  {code} 使用舊快取（{int((now-cached_ts)/60)} 分鐘前）")
                df = cached_df.copy()
            else:
                return None
    if current_price:
        df = df.copy()
        df.loc[df.index[-1], "Close"] = current_price
    return df


def _rsi(close: pd.Series, n: int = 14) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(n).mean()
    loss  = (-delta.clip(upper=0)).rolling(n).mean()
    rs    = gain / loss.replace(0, float("inf"))
    return float(100 - 100 / (1 + rs.iloc[-1]))


def _macd(close: pd.Series):
    ema12  = close.ewm(span=12, adjust=False).mean()
    ema26  = close.ewm(span=26, adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist   = macd - signal
    return float(macd.iloc[-1]), float(signal.iloc[-1]), float(hist.iloc[-1]), float(hist.iloc[-2])


def _kd(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9):
    ll  = low.rolling(n).min()
    hh  = high.rolling(n).max()
    rsv = (close - ll) / (hh - ll).replace(0, float("nan")) * 100
    k   = rsv.ewm(com=2, adjust=False).mean()
    d   = k.ewm(com=2, adjust=False).mean()
    return float(k.iloc[-1]), float(d.iloc[-1]), float(k.iloc[-2]), float(d.iloc[-2])


def _ma_data(close: pd.Series) -> dict:
    ma5  = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    return {
        "ma5": float(ma5.iloc[-1]), "ma5_prev": float(ma5.iloc[-2]),
        "ma20": float(ma20.iloc[-1]),
        "ma60": float(ma60.iloc[-1]) if not pd.isna(ma60.iloc[-1]) else None,
        "price": float(close.iloc[-1]), "prev_close": float(close.iloc[-2]),
    }


def _vol_ratio(volume: pd.Series) -> float:
    avg = volume.rolling(20).mean().iloc[-1]
    return float(volume.iloc[-1] / avg) if avg > 0 else 1.0


def _build_result(signals: list, direction: str) -> dict:
    score     = sum(1 for _, b, _ in signals if b)
    max_score = len(signals)
    if direction == "buy":
        if score >= 4:   level, icon = "強烈建議買進", "🟢🟢"
        elif score == 3: level, icon = "可考慮買進",  "🟢"
        elif score == 2: level, icon = "建議觀望",    "🟡"
        else:            level, icon = "暫不建議買進", "⚪"
    else:
        if score >= 4:   level, icon = "強烈建議賣出",    "🔴🔴"
        elif score == 3: level, icon = "考慮停利/減碼",   "🔴"
        elif score == 2: level, icon = "留意風險",        "🟠"
        else:            level, icon = "趨勢尚可，續抱",  "⚪"
    summary = f"{icon} {level}（{score}/{max_score} 指標{'看多' if direction=='buy' else '看空'}）"
    return {"signals": signals, "score": score, "max_score": max_score,
            "level": level, "level_icon": icon, "summary": summary, "direction": direction}


def buy_analysis(code: str, price: float, scfg: dict, intraday: dict | None = None) -> dict:
    df = _fetch_history(code, price)
    if df is None:
        return {"error": "無法取得歷史資料"}
    close, high, low, volume = df["Close"], df["High"], df["Low"], df["Volume"]
    signals = []

    try:
        rsi = _rsi(close)
        bullish = rsi < 35
        detail  = (f"RSI {rsi:.1f}（{'嚴重超賣 ✦' if rsi<30 else '超賣區' if rsi<35 else '偏弱，未超賣' if rsi<50 else '強勢區，追高風險高'}）")
        signals.append(("RSI", bullish, detail))
    except Exception: pass

    try:
        k, d, k_prev, d_prev = _kd(high, low, close)
        oversold = k < 30; golden = (k_prev < d_prev) and (k > d)
        bullish  = oversold or golden
        detail   = (f"K={k:.1f} D={d:.1f}（{'超賣＋黃金交叉 ✦' if oversold and golden else '超賣區' if oversold else '黃金交叉' if golden else '無超賣訊號'}）")
        signals.append(("KD", bullish, detail))
    except Exception: pass

    try:
        _, _, hist, hist_prev = _macd(close)
        macd_v, signal_v, _, _ = _macd(close)
        bottom = (hist < 0) and (hist > hist_prev); above = macd_v > signal_v
        bullish = bottom or above
        detail  = ("MACD 多頭＋底部收縮" if bottom and above else
                   f"MACD 底部收縮（hist={hist:.2f}）" if bottom else
                   "MACD 在 Signal 上方（多頭）" if above else "MACD 仍在 Signal 下方（空頭）")
        signals.append(("MACD", bullish, detail))
    except Exception: pass

    try:
        ma = _ma_data(close)
        bullish = ma["ma5"] > ma["ma5_prev"]
        pos     = "站上MA20" if ma["price"] > ma["ma20"] else "在MA20下方"
        detail  = f"MA5 {ma['ma5']:,.1f}（{'上彎' if bullish else '下彎'}），{pos}（MA20={ma['ma20']:,.1f}）"
        signals.append(("均線趨勢", bullish, detail))
    except Exception: pass

    try:
        vr = _vol_ratio(volume)
        bullish = vr >= 1.0
        detail  = f"今日量 {vr:.1f}x 均量（{'明顯放量' if vr>=1.5 else '量能正常' if vr>=1.0 else '縮量，動能不足'}）"
        signals.append(("成交量", bullish, detail))
    except Exception: pass

    try:
        price_  = float(close.iloc[-1])
        ma      = _ma_data(close)
        support = scfg.get("support_price")
        parts, bullish = [], False
        if support:
            diff = (price_ - support) / support * 100
            if price_ <= support:
                parts.append(f"現價 {price_:,.0f} 在支撐 {support:,.0f} 以下（{diff:+.1f}%）✦"); bullish = True
            elif diff <= 3:
                parts.append(f"現價 {price_:,.0f} 接近支撐 {support:,.0f}（{diff:+.1f}%）"); bullish = True
            else:
                parts.append(f"現價 {price_:,.0f} 距支撐 {support:,.0f} 尚有 {diff:.1f}%")
        if ma["ma60"] and price_ < ma["ma60"]:
            parts.append(f"跌破MA60({ma['ma60']:,.1f})，長線低估區"); bullish = True
        elif price_ < ma["ma20"]:
            parts.append(f"在MA20({ma['ma20']:,.1f})下方")
            if not support: bullish = True
        else:
            parts.append(f"在MA20上方 {(price_-ma['ma20'])/ma['ma20']*100:.1f}%")
        signals.append(("價格位置", bullish, "、".join(parts)))
    except Exception: pass

    if intraday:
        try:
            cp = intraday.get("change_pct", 0); pos = intraday.get("intraday_pos")
            if cp <= -3:
                bullish = True; detail = f"今日重跌 {cp:.1f}%，超賣機會"
            elif cp <= -1.5 and pos is not None and pos <= 35:
                bullish = True; detail = f"今日跌 {cp:.1f}%，盤中接近低點（位置 {pos:.0f}%）"
            elif pos is not None and pos <= 25:
                bullish = True; detail = f"盤中貼近今日低點（位置 {pos:.0f}%），可能止跌"
            else:
                bullish = False
                detail  = f"今日 {cp:+.1f}%{f'，盤中位置 {pos:.0f}%' if pos is not None else ''}，無明顯超賣"
            signals.append(("盤中走勢", bullish, detail))
        except Exception: pass

    return _build_result(signals, "buy")


def sell_analysis(code: str, price: float, scfg: dict, intraday: dict | None = None) -> dict:
    df = _fetch_history(code, price)
    if df is None:
        return {"error": "無法取得歷史資料"}
    close, high, low, volume = df["Close"], df["High"], df["Low"], df["Volume"]
    signals = []

    try:
        rsi = _rsi(close)
        bearish = rsi > 70
        detail  = (f"RSI {rsi:.1f}（{'嚴重超買 ✦' if rsi>80 else '超買區' if rsi>70 else '偏強，未超買' if rsi>55 else '無超買訊號'}）")
        signals.append(("RSI", bearish, detail))
    except Exception: pass

    try:
        k, d, k_prev, d_prev = _kd(high, low, close)
        over = k > 80; dead = (k_prev > d_prev) and (k < d)
        bearish = over or dead
        detail  = (f"K={k:.1f} D={d:.1f}（{'超買＋死亡交叉 ✦' if over and dead else '超買區' if over else '死亡交叉' if dead else '無超買訊號'}）")
        signals.append(("KD", bearish, detail))
    except Exception: pass

    try:
        macd_v, signal_v, hist, hist_prev = _macd(close)
        top = (hist > 0) and (hist < hist_prev); below = macd_v < signal_v
        bearish = top or below
        detail  = ("MACD 空頭＋頂部收縮" if top and below else
                   f"MACD 頂部收縮（hist={hist:.2f}）" if top else
                   "MACD 跌破 Signal（空頭）" if below else "MACD 仍在 Signal 上方（多頭）")
        signals.append(("MACD", bearish, detail))
    except Exception: pass

    try:
        ma = _ma_data(close)
        bearish = ma["ma5"] < ma["ma5_prev"]
        parts   = []
        if ma["price"] < ma["ma20"]: parts.append(f"跌破MA20({ma['ma20']:,.1f})")
        if ma["ma60"] and ma["price"] < ma["ma60"]: parts.append(f"跌破MA60({ma['ma60']:,.1f})⚠")
        detail  = f"MA5 {ma['ma5']:,.1f}（{'下彎' if bearish else '上彎'}），{'、'.join(parts) if parts else '仍在MA20上方'}"
        signals.append(("均線趨勢", bearish, detail))
    except Exception: pass

    try:
        vr = _vol_ratio(volume); ma = _ma_data(close)
        drop = ma["price"] < ma["prev_close"]
        bearish = (vr >= 1.2) and drop
        detail  = (f"今日量 {vr:.1f}x 均量且下跌（疑似出貨）" if bearish else
                   f"今日量 {vr:.1f}x 均量但收漲（多頭放量）" if vr >= 1.5 else
                   f"今日量 {vr:.1f}x 均量（無異常）")
        signals.append(("成交量", bearish, detail))
    except Exception: pass

    try:
        price_     = float(close.iloc[-1])
        ma         = _ma_data(close)
        resistance = scfg.get("resistance_price")
        parts, bearish = [], False
        if resistance:
            diff = (price_ - resistance) / resistance * 100
            if price_ >= resistance:
                parts.append(f"現價 {price_:,.0f} 已達壓力 {resistance:,.0f}（{diff:+.1f}%）✦"); bearish = True
            elif diff >= -3:
                parts.append(f"現價 {price_:,.0f} 接近壓力 {resistance:,.0f}（{diff:+.1f}%）"); bearish = True
            else:
                parts.append(f"現價 {price_:,.0f} 距壓力 {resistance:,.0f} 尚有 {-diff:.1f}%")
        pct_above = (price_ - ma["ma20"]) / ma["ma20"] * 100
        if pct_above >= 10:
            parts.append(f"高於MA20 {pct_above:.1f}%（偏離過大）"); bearish = bearish or (not resistance)
        else:
            parts.append(f"在MA20{'上方' if pct_above>=0 else '下方'} {abs(pct_above):.1f}%")
        signals.append(("價格位置", bearish, "、".join(parts)))
    except Exception: pass

    if intraday:
        try:
            cp = intraday.get("change_pct", 0); pos = intraday.get("intraday_pos")
            if cp >= 3:
                bearish = True; detail = f"今日大漲 {cp:.1f}%，超買風險高"
            elif cp >= 1.5 and pos is not None and pos >= 65:
                bearish = True; detail = f"今日漲 {cp:.1f}%，盤中貼近高點（位置 {pos:.0f}%）"
            elif pos is not None and pos >= 75:
                bearish = True; detail = f"盤中在今日高點附近（位置 {pos:.0f}%），動能可能衰退"
            else:
                bearish = False
                detail  = f"今日 {cp:+.1f}%{f'，盤中位置 {pos:.0f}%' if pos is not None else ''}，無明顯超買"
            signals.append(("盤中走勢", bearish, detail))
        except Exception: pass

    return _build_result(signals, "sell")


def decision_line(result: dict) -> str:
    if "error" in result:
        return f"⚠ {result['error']}\n"
    active = [name for name, b, _ in result["signals"] if b]
    return f"根據：{' + '.join(active[:3]) if active else '無明顯訊號'}\n"


# ══════════════════════════════════════════════════════════════════════
# 股價快取（Web API 用）
# ══════════════════════════════════════════════════════════════════════

_cache: dict       = {}
_cache_lock        = threading.Lock()
_last_fetch: float = 0.0


def get_prices() -> dict:
    global _last_fetch, _cache
    now = time.time()
    if now - _last_fetch > 30:
        cfg   = load_config()
        fresh = {}
        for code in cfg.get("stocks", {}):
            d = get_intraday(code)
            if d:
                fresh[code] = d["price"]
        with _cache_lock:
            if fresh:
                _cache = fresh
            _last_fetch = now
    with _cache_lock:
        return dict(_cache)


# ══════════════════════════════════════════════════════════════════════
# 通知
# ══════════════════════════════════════════════════════════════════════

_notified: dict[str, float] = {}
NOTIFY_COOLDOWN_SEC = 3600

# 監控執行緒狀態追蹤
_monitor_status = {
    "running": False,
    "last_check": None,
    "last_error": None,
    "checks_today": 0,
    "notifications_sent": 0,
}


def send_ntfy(title: str, message: str, priority: str = "default") -> bool:
    topic = NTFY_TOPIC or load_config().get("ntfy_topic", "")
    if not topic:
        log.warning("ntfy topic 未設定，跳過推播")
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "chart_increasing"},
            timeout=10,
        )
        ok = r.status_code == 200
        if ok:
            _monitor_status["notifications_sent"] += 1
        else:
            log.warning(f"ntfy 回應異常：{r.status_code}")
        return ok
    except Exception as e:
        log.error(f"ntfy 通知失敗：{e}")
        return False


def notify(alert_key: str, message: str, title: str = "股市通知", priority: str = "default"):
    now_ts = time.time()
    if now_ts - _notified.get(alert_key, 0) < NOTIFY_COOLDOWN_SEC:
        return
    _notified[alert_key] = now_ts
    log.info(f"  ➜ 推播：{message[:60]}…")
    send_ntfy(title, message, priority)


# ══════════════════════════════════════════════════════════════════════
# 監控主邏輯
# ══════════════════════════════════════════════════════════════════════

def _price_line(intraday: dict) -> str:
    price = intraday["price"]
    cp    = intraday["change_pct"]
    sign  = "↑" if cp >= 0 else "↓"
    pos   = intraday.get("intraday_pos")
    pos_s = f"  盤中位置 {pos:.0f}%" if pos is not None else ""
    h, l  = intraday.get("high"), intraday.get("low")
    hl_s  = f"  今日 {l:,.0f}～{h:,.0f}" if h and l else ""
    return f"現價 {price:,.0f}｜今日 {sign}{abs(cp):.1f}%{pos_s}{hl_s}"


def _suggest_shares(code: str, scfg: dict, price: float) -> str:
    budget = scfg.get("budget")
    if not budget or price <= 0:
        return ""
    invested  = _load()["holdings"].get(code, {}).get("total_cost", 0.0)
    remaining = budget - invested
    if remaining <= 0:
        return f"\n💰 預算已用完（已投入 {invested:,.0f} / {budget:,.0f} 元）"
    shares = int(remaining // price)
    if shares <= 0:
        return f"\n💰 剩餘預算不足買入 1 股（剩 {remaining:,.0f} 元）"
    return (f"\n📌 建議買入：{shares} 股零股（現價 {price:,.0f} 元，約需 {shares*price:,.0f} 元）"
            f"\n   剩餘預算 {remaining:,.0f} 元（預算 {budget:,.0f}，已投入 {invested:,.0f}）")


def _holding_note(code: str, price: float) -> str:
    h = _load()["holdings"].get(code, {})
    if not h:
        return ""
    pnl  = (price - h["avg_cost"]) * h["shares"]
    sign = "▲" if pnl >= 0 else "▼"
    return (f"\n── 持倉 ──\n"
            f"持有 {h['shares']} 股  均價 {h['avg_cost']:,.0f} 元\n"
            f"未實現損益：{sign}{abs(pnl):,.0f} 元")


def check_stock(code: str, scfg: dict, intraday: dict, cfg: dict):
    price    = intraday["price"]
    name     = scfg.get("name", code)
    now_s    = datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M")
    buy_thr  = cfg.get("buy_threshold", 5)
    sell_thr = cfg.get("sell_threshold", 5)

    log.info(f"  分析技術指標（{name} {code}）…")
    buy_r  = buy_analysis(code, price, scfg, intraday)
    sell_r = sell_analysis(code, price, scfg, intraday)

    holding      = _load()["holdings"].get(code, {})
    has_position = bool(holding)

    b_score = buy_r.get("score", 0) if "error" not in buy_r else 0
    s_score = sell_r.get("score", 0) if "error" not in sell_r else 0

    # 買訊：達門檻，且買分 > 賣分（避免訊號衝突）
    if "error" not in buy_r and b_score >= buy_thr and b_score > s_score:
        msg = (f"🟢 {name}（{code}）{buy_r['level']}\n"
               f"{_price_line(intraday)}\n"
               f"{decision_line(buy_r)}"
               f"{_suggest_shares(code, scfg, price)}"
               f"{_holding_note(code, price)}\n"
               f"⏰ {now_s}")
        notify(f"{code}_buy", msg, f"買進訊號｜{name}", "high")

    if has_position and not scfg.get("no_sell_alert") and "error" not in sell_r and s_score >= sell_thr and s_score > b_score:
        avg_cost = holding["avg_cost"]
        pnl_pct  = (price - avg_cost) / avg_cost * 100
        if pnl_pct <= -5:
            icon, action = "🚨", "建議停損出場"
        elif pnl_pct >= 5:
            icon, action = "🔴", "建議停利出場"
        else:
            icon, action = "🟠", "建議減碼觀察"
        msg = (f"{icon} {name}（{code}）{action}\n"
               f"{_price_line(intraday)}\n"
               f"{decision_line(sell_r)}"
               f"{_holding_note(code, price)}\n"
               f"⏰ {now_s}")
        notify(f"{code}_sell", msg, f"賣出訊號｜{name}", "urgent" if pnl_pct <= -5 else "high")


def run_check():
    cfg   = load_config()
    now_s = datetime.now(TW_TZ).strftime("%H:%M")
    _monitor_status["last_check"] = datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M:%S")
    if not is_market_hours():
        log.info(f"[{now_s}] 非交易時段，等待中…")
        return
    log.info(f"{'─'*40}")
    log.info(f"[{now_s}] 開始檢查股價")
    _monitor_status["checks_today"] += 1
    prices = {}
    for code, scfg in cfg.get("stocks", {}).items():
        intraday = get_intraday(code)
        if not intraday:
            log.warning(f"  {scfg.get('name', code)} ({code})：無法取得股價")
            continue
        price = intraday["price"]
        prices[code] = price
        cp   = intraday["change_pct"]
        sign = "↑" if cp >= 0 else "↓"
        log.info(f"  {scfg.get('name',code)} ({code})：{price:,.0f} 元（{sign}{abs(cp):.1f}%）")
        check_stock(code, scfg, intraday, cfg)
    now = datetime.now(TW_TZ)
    if now.minute < 6:
        summary = get_portfolio_summary(prices)
        lines   = ["\n📊 即時持倉摘要"]
        for r in summary["rows"]:
            lines.append(f"  {r['name']}({r['code']}) {r['shares']}股"
                         f" | 均{r['avg_cost']:,.0f} 現{r['price']}"
                         f" | {'▲' if r['pnl']>=0 else '▼'}{abs(r['pnl']):,.0f}元({r['pnl_pct']:+.1f}%)")
        lines.append(f"  現金：{summary['cash']:,.0f}  總資產：{summary['total_assets']:,.0f}")
        notify(f"portfolio_hourly_{now.hour}", "\n".join(lines), "持倉摘要")


def _monitor_thread():
    _monitor_status["running"] = True
    try:
        cfg      = load_config()
        interval = cfg.get("check_interval_minutes", 1)
        topic    = NTFY_TOPIC or cfg.get("ntfy_topic", "")
        log.info(f"監控執行緒啟動（每 {interval} 分鐘，ntfy={'已設定 '+topic if topic else '未設定⚠'}）")
        # 啟動確認通知
        if topic:
            send_ntfy("股市監控啟動", f"監控執行緒已啟動，每 {interval} 分鐘檢查一次\n"
                      f"監控股票：{', '.join(cfg.get('stocks', {}).keys())}", "low")
        run_check()
        schedule.every(interval).minutes.do(run_check)
        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                log.error(f"排程執行錯誤：{e}")
                _monitor_status["last_error"] = str(e)
            time.sleep(5)
    except Exception as e:
        _monitor_status["running"] = False
        _monitor_status["last_error"] = str(e)
        log.error(f"監控執行緒崩潰：{e}")


# ══════════════════════════════════════════════════════════════════════
# Flask API
# ══════════════════════════════════════════════════════════════════════

app = Flask(__name__)


@app.route("/api/portfolio")
def api_portfolio():
    prices     = get_prices()
    summary    = get_portfolio_summary(prices)
    cfg        = load_config()
    stocks_cfg = {
        code: {"name": s.get("name", code),
               "support_price": s.get("support_price"),
               "resistance_price": s.get("resistance_price")}
        for code, s in cfg.get("stocks", {}).items()
    }
    return jsonify({"summary": summary, "prices": prices,
                    "stocks_cfg": stocks_cfg, "market": is_market_hours()})


@app.route("/api/buy", methods=["POST"])
def api_buy():
    d = request.json or {}
    try:
        result = pf_buy(d["code"], d.get("name", d["code"]),
                        int(d["shares"]), float(d["price"]))
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/sell", methods=["POST"])
def api_sell():
    d = request.json or {}
    try:
        result = pf_sell(d["code"], int(d["shares"]), float(d["price"]))
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/cash", methods=["POST"])
def api_cash():
    d = request.json or {}
    set_cash(float(d["amount"]))
    return jsonify({"ok": True})


@app.route("/api/history")
def api_history():
    return jsonify(list(reversed(_load().get("transactions", []))))


@app.route("/api/debug_check")
def api_debug_check():
    """手動觸發一次檢查，回傳每支股票的分析結果"""
    cfg    = load_config()
    result = {}
    for code, scfg in cfg.get("stocks", {}).items():
        intraday = get_intraday(code)
        if not intraday:
            result[code] = {"error": "無法取得股價"}
            continue
        price = intraday["price"]
        buy_r  = buy_analysis(code, price, scfg, intraday)
        sell_r = sell_analysis(code, price, scfg, intraday)
        buy_thr  = cfg.get("buy_threshold", 5)
        sell_thr = cfg.get("sell_threshold", 5)
        has_pos  = bool(_load()["holdings"].get(code))
        result[code] = {
            "name":       scfg.get("name", code),
            "price":      price,
            "change_pct": round(intraday.get("change_pct", 0), 2),
            "buy_score":  buy_r.get("score"),
            "sell_score": sell_r.get("score"),
            "buy_triggered":  (buy_r.get("score", 0) >= buy_thr) if "error" not in buy_r else False,
            "sell_triggered": (has_pos and not scfg.get("no_sell_alert") and
                               sell_r.get("score", 0) >= sell_thr) if "error" not in sell_r else False,
            "buy_error":  buy_r.get("error"),
            "sell_error": sell_r.get("error"),
            "hist_cached": code in _hist_cache,
        }
    topic = NTFY_TOPIC or cfg.get("ntfy_topic", "")
    return jsonify({
        "time_tw": datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M:%S"),
        "market_open": is_market_hours(),
        "ntfy_topic": topic or "(未設定)",
        "buy_threshold": cfg.get("buy_threshold", 5),
        "stocks": result,
    })


@app.route("/api/status")
def api_status():
    cfg   = load_config()
    topic = NTFY_TOPIC or cfg.get("ntfy_topic", "")
    return jsonify({
        "monitor": _monitor_status,
        "ntfy_topic": topic if topic else "(未設定)",
        "ntfy_topic_source": "env" if NTFY_TOPIC else ("config" if cfg.get("ntfy_topic") else "none"),
        "market_open": is_market_hours(),
        "now_tw": datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M:%S"),
        "stocks": list(cfg.get("stocks", {}).keys()),
    })


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def api_set_config():
    new_cfg = request.json or {}
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_cfg, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/static/manifest.json")
def manifest():
    return app.send_static_file("manifest.json")


# ══════════════════════════════════════════════════════════════════════
# 啟動
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    threading.Thread(target=_monitor_thread, daemon=True).start()
    log.info(f"台股監控雲端版啟動，PORT={PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
