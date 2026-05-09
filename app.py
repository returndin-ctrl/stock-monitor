#!/usr/bin/env python3
"""
台股監控 Web App（雲端版）
部署到 Railway（GitHub auto-deploy via webhook），以技術指標＋價格位置綜合判斷買賣時機
"""

import sys, os, time, json, threading, logging, requests, schedule, pytz, sqlite3
import pandas as pd
import yfinance as yf
from datetime import datetime, time as dtime, timedelta
from flask import Flask, jsonify, request, render_template

# ── 環境變數 ──────────────────────────────────────────────────────────
PORT          = int(os.environ.get('PORT', 8080))
DATA_DIR      = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(__file__), 'data'))
TG_BOT_TOKEN  = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT_ID    = os.environ.get('TELEGRAM_CHAT_ID', '')

os.makedirs(DATA_DIR, exist_ok=True)

PORTFOLIO_FILE = os.path.join(DATA_DIR, 'portfolio.json')
CONFIG_FILE    = os.path.join(DATA_DIR, 'config.json')

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "check_interval_minutes": 1,
    "buy_threshold": 5,
    "sell_threshold": 3,
    "initial_cash": 50000,
    "stocks": {
        "0050": {"name": "元大台灣50", "budget": 50000, "support_price": 70,   "resistance_price": 90,   "no_sell_alert": True},
        "2303": {"name": "聯電",       "budget": 50000, "support_price": 53,   "resistance_price": 80},
        "2308": {"name": "台達電",     "budget": 50000, "support_price": 2000, "resistance_price": 2280, "peer_group": "AI_SERVER"},
        "2324": {"name": "仁寶",       "budget": 50000, "support_price": 28,   "resistance_price": 33},
        "2330": {"name": "台積電",     "budget": 50000, "support_price": 1760, "resistance_price": 2180},
        "2408": {"name": "南亞科",     "budget": 50000, "support_price": 198,  "resistance_price": 249,  "peer_group": "DRAM"},
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


# ══════════════════════════════════════════════════════════════════════
# 訊號資料庫（SQLite）— 用於回測 + 週報
# ══════════════════════════════════════════════════════════════════════

SIGNAL_DB = os.path.join(DATA_DIR, 'signals.db')
_db_lock = threading.Lock()


def _db_conn():
    return sqlite3.connect(SIGNAL_DB, timeout=10)


def _db_init():
    with _db_lock, _db_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                signal_type TEXT NOT NULL,
                score INTEGER,
                threshold INTEGER,
                price REAL,
                pnl_pct REAL,
                risk_flag INTEGER,
                risk_reasons TEXT,
                message TEXT
            )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_signals_code ON signals(code)')


def record_signal(code: str, name: str, signal_type: str,
                  score: int = None, threshold: int = None,
                  price: float = None, pnl_pct: float = None,
                  risk_flag: bool = False, risk_reasons: dict = None,
                  message: str = ""):
    try:
        ts = datetime.now(TW_TZ).isoformat()
        with _db_lock, _db_conn() as conn:
            conn.execute('''
                INSERT INTO signals (timestamp, code, name, signal_type, score, threshold,
                                     price, pnl_pct, risk_flag, risk_reasons, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (ts, code, name, signal_type, score, threshold,
                  price, pnl_pct, int(bool(risk_flag)),
                  json.dumps(risk_reasons or {}, ensure_ascii=False),
                  (message or "")[:500]))
    except Exception as e:
        log.debug(f"record_signal failed ({code} {signal_type}): {e}")


def query_signals(start_ts: str = None, end_ts: str = None) -> list:
    sql = "SELECT * FROM signals"
    where, params = [], []
    if start_ts:
        where.append("timestamp >= ?"); params.append(start_ts)
    if end_ts:
        where.append("timestamp <= ?"); params.append(end_ts)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY timestamp"
    with _db_lock, _db_conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


_db_init()


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
        def _first_nonzero(stack: str):
            for p in (stack or "").split("_"):
                try:
                    v = float(p)
                    if v > 0:
                        return v
                except Exception:
                    pass
            return None
        prev = _f("y")
        high = _f("h")
        low  = _f("l")
        # 撮合空檔（z 空）時依序 fallback：上一筆成交 → 買一/賣一中點 → 今日高低中點 → 昨收
        price = _f("z") or _f("pz")
        if price is None:
            bid = _first_nonzero(item.get("b", ""))
            ask = _first_nonzero(item.get("a", ""))
            if bid and ask:   price = (bid + ask) / 2
            elif bid:         price = bid
            elif ask:         price = ask
        if price is None and high and low:
            price = (high + low) / 2
        if price is None:
            price = prev
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
    # 上市股用 .TW，上櫃股用 .TWO；先試 .TW 沒資料再試 .TWO
    for suffix in (".TW", ".TWO"):
        try:
            hist = yf.Ticker(f"{code}{suffix}").history(period="2d", interval="1d")
            if len(hist) < 2:
                continue
            price = float(hist["Close"].iloc[-1])
            prev  = float(hist["Close"].iloc[-2])
            return {"price": price, "prev_close": prev, "open": None,
                    "high": float(hist["High"].iloc[-1]),
                    "low":  float(hist["Low"].iloc[-1]),
                    "change_pct": (price - prev) / prev * 100,
                    "intraday_pos": None}
        except Exception:
            continue
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
            df = None
            for suffix in (".TW", ".TWO"):  # 上市 / 上櫃 fallback
                df_try = yf.Ticker(f"{code}{suffix}").history(period="90d", interval="1d")
                if len(df_try) >= 30:
                    df = df_try
                    break
            if df is None:
                log.warning(f"  {code} 歷史資料不足（< 30 筆，TW/TWO 都試過）")
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
        last = df.index[-1]
        df.loc[last, "Close"] = current_price
        # high/low 也同步擴張，避免 KD/盤中位置等指標用過期區間
        df.loc[last, "High"] = max(float(df.loc[last, "High"]), current_price)
        df.loc[last, "Low"]  = min(float(df.loc[last, "Low"]),  current_price)
    return df


def _rsi(close: pd.Series, n: int = 14) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(n).mean()
    loss  = (-delta.clip(upper=0)).rolling(n).mean()
    last_gain = float(gain.iloc[-1])
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    rs = last_gain / last_loss
    return float(100 - 100 / (1 + rs))


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
        # 監控清單 ∪ 持倉
        codes = set(cfg.get("stocks", {}).keys())
        codes.update(_load().get("holdings", {}).keys())
        fresh = {}
        for code in codes:
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


def _tg_credentials() -> tuple[str, str]:
    cfg = load_config()
    token = TG_BOT_TOKEN or cfg.get("telegram_bot_token", "")
    chat  = TG_CHAT_ID   or cfg.get("telegram_chat_id", "")
    return token, chat


def send_telegram(title: str, message: str, priority: str = "default") -> bool:
    token, chat = _tg_credentials()
    if not token or not chat:
        log.warning("Telegram 憑證未設定，跳過推播")
        return False
    try:
        text = f"*{title}*\n{message}" if title else message
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "disable_notification": (priority == "low"),
            },
            timeout=10,
        )
        ok = r.ok
        if ok:
            _monitor_status["notifications_sent"] += 1
        else:
            log.warning(f"Telegram 回應異常：{r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        log.error(f"Telegram 通知失敗：{e}")
        return False


def notify(alert_key: str, message: str, title: str = "股市通知", priority: str = "default"):
    now_ts = time.time()
    if now_ts - _notified.get(alert_key, 0) < NOTIFY_COOLDOWN_SEC:
        return
    log.info(f"  ➜ 推播：{message[:60]}…")
    if send_telegram(title, message, priority):
        _notified[alert_key] = now_ts
    # 失敗時不更新 cooldown，下一輪會重試


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


def _suggest_shares(code: str, scfg: dict, price: float, cfg: dict | None = None,
                     score: int | None = None) -> str:
    """依可用現金 × 訊號強度比例算建議股數
       score 4 → 50%、5 → 75%、≥6 → 100%"""
    if price <= 0:
        return ""
    cash = _load().get("cash", 0)
    if cash <= 0:
        return f"\n💰 現金不足（目前餘額 {cash:,.0f}）"

    if score is None or score >= 6:
        fraction, label = 1.0, "全額（極強訊號）"
    elif score == 5:
        fraction, label = 0.75, "75%（強訊號）"
    else:
        fraction, label = 0.5, "50%（標準強訊號）"

    allocate = cash * fraction
    shares = int(allocate // price)
    if shares <= 0:
        return f"\n💰 可用現金 {cash:,.0f} 元，買不到 1 股 @ {price:,.0f}"
    return (f"\n📌 建議買入：{shares} 股 — {label}：{shares*price:,.0f} 元 @ {price:,.0f}"
            f"\n   可用現金 {cash:,.0f}")


def _suggest_sell_shares(code: str, price: float, fraction: float, hint: str = "") -> str:
    """依持倉 × 比例算出建議賣出股數"""
    h = _load()["holdings"].get(code, {})
    if not h:
        return ""
    shares = h["shares"]
    if shares <= 0:
        return ""
    sell_n   = max(1, int(shares * fraction))
    pct      = int(round(fraction * 100))
    proceeds = sell_n * price
    note     = f"（{hint}）" if hint else ""
    return (f"\n📌 建議賣出：{sell_n} 股{note}"
            f"\n   佔持倉 {pct}%（共 {shares} 股），約可入帳 {proceeds:,.0f} 元（未扣稅費）")


def _holding_note(code: str, price: float) -> str:
    h = _load()["holdings"].get(code, {})
    if not h:
        return ""
    pnl  = (price - h["avg_cost"]) * h["shares"]
    sign = "▲" if pnl >= 0 else "▼"
    return (f"\n── 持倉 ──\n"
            f"持有 {h['shares']} 股  均價 {h['avg_cost']:,.0f} 元\n"
            f"未實現損益：{sign}{abs(pnl):,.0f} 元")


# ══════════════════════════════════════════════════════════════════════
# 族群連動偵測（DRAM 同業）
# ══════════════════════════════════════════════════════════════════════

PEER_GROUPS = {
    "DRAM": {
        "label": "DRAM 族群",
        "peers": {"2344": "華邦電", "3260": "威剛", "4967": "十銓",
                  "6770": "力積電", "8299": "群聯"},
    },
    "AI_SERVER": {
        "label": "AI 伺服器族群",
        "peers": {"2317": "鴻海", "2382": "廣達", "3231": "緯創",
                  "6669": "緯穎", "2308": "台達電"},
    },
}

_peer_cache: dict[str, dict] = {}  # group_name -> {"data": ..., "ts": ...}
_PEER_TTL = 180  # 3 分鐘


def get_peer_pulse(group: str = "DRAM") -> dict:
    """族群連動狀態：跌幅 >5% 家數、平均漲跌、明細"""
    if group not in PEER_GROUPS:
        return {"down_5": 0, "down_3": 0, "up_5": 0, "avg_pct": 0.0,
                "rows": [], "alarm_down": False, "alarm_up": False,
                "label": ""}
    now = time.time()
    cached = _peer_cache.get(group)
    if cached and now - cached["ts"] < _PEER_TTL:
        return cached["data"]
    peers = PEER_GROUPS[group]["peers"]
    label = PEER_GROUPS[group]["label"]
    rows = []
    for c, n in peers.items():
        d = get_intraday(c)
        if d:
            rows.append({"code": c, "name": n, "change_pct": d["change_pct"]})
    if not rows:
        result = {"down_5": 0, "up_5": 0, "down_3": 0, "avg_pct": 0.0, "rows": [],
                  "alarm_down": False, "alarm_up": False, "label": label}
    else:
        down_5 = sum(1 for r in rows if r["change_pct"] <= -5)
        down_3 = sum(1 for r in rows if r["change_pct"] <= -3)
        up_5   = sum(1 for r in rows if r["change_pct"] >=  5)
        avg    = sum(r["change_pct"] for r in rows) / len(rows)
        # 族群弱勢：≥3 檔重挫 OR ≥4 檔跌3% OR 平均跌4%
        alarm_down = down_5 >= 3 or down_3 >= 4 or avg <= -4
        alarm_up   = up_5 >= 3
        result = {"down_5": down_5, "down_3": down_3, "up_5": up_5,
                  "avg_pct": avg, "rows": rows,
                  "alarm_down": alarm_down, "alarm_up": alarm_up,
                  "label": label}
    _peer_cache[group] = {"data": result, "ts": now}
    return result


def peer_summary_line(pulse: dict) -> str:
    if not pulse["rows"]:
        return ""
    sign  = "↑" if pulse["avg_pct"] >= 0 else "↓"
    label = pulse.get("label", "族群")
    head = f"{label}均 {sign}{abs(pulse['avg_pct']):.1f}%（{len(pulse['rows'])} 檔）"
    if pulse["alarm_down"]:
        bad = [r["name"] for r in pulse["rows"] if r["change_pct"] <= -3]
        return f"⚠ {head}｜{pulse['down_3']} 檔走弱：{'、'.join(bad)}"
    if pulse["alarm_up"]:
        good = [r["name"] for r in pulse["rows"] if r["change_pct"] >= 5]
        return f"🔥 {head}｜{pulse['up_5']} 檔強漲：{'、'.join(good)}"
    return f"📊 {head}"


# ══════════════════════════════════════════════════════════════════════
# 新聞關鍵字偵測（Yahoo 個股新聞）
# ══════════════════════════════════════════════════════════════════════

NEWS_KEYWORDS_BAD = [
    "跌停", "重挫", "崩跌", "暴跌", "失守", "警示股", "處置股",
    "關稅", "利空", "減產", "降評", "下修", "違規", "停牌",
]
NEWS_KEYWORDS_HOT = ["漲停", "創高", "突破", "上修", "利多"]

_news_cache: dict[str, dict] = {}        # code -> {"news": [...], "ts": ...}
_NEWS_TTL = 1800                          # 30 分鐘抓一次
_NEWS_SEEN_FILE = os.path.join(DATA_DIR, 'news_seen.json')
_NEWS_SEEN_MAX_PER_CODE = 200             # 每檔最多保留最近 200 條已推過的標題


def _news_seen_load() -> dict[str, set]:
    if not os.path.exists(_NEWS_SEEN_FILE):
        return {}
    try:
        with open(_NEWS_SEEN_FILE, encoding='utf-8') as f:
            raw = json.load(f)
        return {k: set(v) for k, v in raw.items()}
    except Exception:
        return {}


def _news_seen_save(seen: dict[str, set]):
    try:
        out = {k: list(v)[-_NEWS_SEEN_MAX_PER_CODE:] for k, v in seen.items()}
        with open(_NEWS_SEEN_FILE, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception as e:
        log.debug(f"news_seen 寫檔失敗: {e}")


_news_seen: dict[str, set] = _news_seen_load()  # code -> 已推過的標題集合（持久化）


def fetch_yahoo_news(code: str) -> list[str]:
    """抓 Yahoo 個股新聞標題（從頁面 JSON payload 解析）"""
    import re
    now = time.time()
    cached = _news_cache.get(code)
    if cached and now - cached["ts"] < _NEWS_TTL:
        return cached["news"]
    try:
        r = requests.get(
            f"https://tw.stock.yahoo.com/quote/{code}.TW/news",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        if not r.ok:
            return []
        raw = re.findall(r'"title":"([^"]{10,120})"', r.text)
        titles = []
        for t in raw:
            # 只解 \uXXXX 跟 \/，不要動到 UTF-8 中文
            t = re.sub(r'\\u([0-9a-fA-F]{4})',
                       lambda m: chr(int(m.group(1), 16)), t)
            t = t.replace("\\/", "/")
            if "Yahoo股市" in t or "相關新聞" in t:
                continue
            titles.append(t)
        news = list(dict.fromkeys(titles))[:20]  # 去重保序、取前 20
        _news_cache[code] = {"news": news, "ts": now}
        return news
    except Exception as e:
        log.debug(f"Yahoo 新聞抓取失敗 ({code}): {e}")
        return []


def scan_news(code: str, name: str) -> dict:
    """掃描新聞。
    bad_hits/hot_hits: 只有「未推過 alert」的新標題（推播用、避免洗版）
    has_bad_news/has_hot_news: 當前頁面是否有任何負/正面標題（risk gating 用）
    """
    titles = fetch_yahoo_news(code)
    seen = _news_seen.setdefault(code, set())
    bad_hits, hot_hits = [], []
    has_bad, has_hot = False, False
    new_titles = False
    for t in titles:
        # 標題沒指名該標的（名稱或代號）就忽略，避免大盤/類股新聞誤觸
        if name not in t and code not in t:
            continue
        b = [k for k in NEWS_KEYWORDS_BAD if k in t]
        h = [k for k in NEWS_KEYWORDS_HOT if k in t]
        if b: has_bad = True
        if h: has_hot = True
        if t in seen:
            continue
        if b or h:
            seen.add(t)
            new_titles = True
            if b: bad_hits.append({"title": t, "kw": b})
            if h: hot_hits.append({"title": t, "kw": h})
    if new_titles:
        _news_seen_save(_news_seen)
    alert_msg = None
    if bad_hits:
        lines = [f"📰 {name}（{code}）出現 {len(bad_hits)} 條負面新聞"]
        for x in bad_hits[:3]:
            lines.append(f"  ⚠ {x['title']}（{'/'.join(x['kw'])}）")
        alert_msg = "\n".join(lines)
    return {"bad_hits": bad_hits, "hot_hits": hot_hits,
            "has_bad_news": has_bad, "has_hot_news": has_hot,
            "alert_msg": alert_msg}


# ══════════════════════════════════════════════════════════════════════
# 大盤即時 + 美股隔夜（宏觀情勢）
# ══════════════════════════════════════════════════════════════════════

_market_cache    = {"data": None, "ts": 0.0}
_overnight_cache = {"data": None, "ts": 0.0}
_MARKET_TTL      = 180     # 大盤 3 分鐘
_OVERNIGHT_TTL   = 1800    # 美股隔夜 30 分鐘（盤中不會變）


def _yf_change(symbol: str) -> dict | None:
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="1d")
        if len(df) < 2:
            return None
        today = float(df["Close"].iloc[-1])
        prev  = float(df["Close"].iloc[-2])
        return {"close": today, "change_pct": (today - prev) / prev * 100}
    except Exception as e:
        log.debug(f"yfinance 失敗 ({symbol}): {e}")
        return None


def get_market_pulse() -> dict:
    """加權指數 + 台積電即時狀態。alarm: 加權跌>1% OR 2330 跌>2%"""
    now = time.time()
    if _market_cache["data"] and now - _market_cache["ts"] < _MARKET_TTL:
        return _market_cache["data"]
    twii = _yf_change("^TWII")
    tsmc = get_intraday("2330")  # 用既有的雙來源（TWSE + yfinance）
    twii_cp = twii["change_pct"] if twii else None
    tsmc_cp = tsmc["change_pct"] if tsmc else None
    alarm = (twii_cp is not None and twii_cp <= -1) or (tsmc_cp is not None and tsmc_cp <= -2)
    boost = (twii_cp is not None and twii_cp >=  1) and (tsmc_cp is not None and tsmc_cp >= 1)
    result = {"twii_cp": twii_cp, "tsmc_cp": tsmc_cp, "alarm": alarm, "boost": boost}
    _market_cache["data"] = result
    _market_cache["ts"]   = now
    return result


def market_summary_line(mp: dict) -> str:
    if mp["twii_cp"] is None and mp["tsmc_cp"] is None:
        return ""
    parts = []
    if mp["twii_cp"] is not None:
        s = "↑" if mp["twii_cp"] >= 0 else "↓"
        parts.append(f"加權 {s}{abs(mp['twii_cp']):.2f}%")
    if mp["tsmc_cp"] is not None:
        s = "↑" if mp["tsmc_cp"] >= 0 else "↓"
        parts.append(f"台積電 {s}{abs(mp['tsmc_cp']):.2f}%")
    head = "｜".join(parts)
    if mp["alarm"]:
        return f"⚠ 大盤 {head}"
    if mp["boost"]:
        return f"🔥 大盤 {head}"
    return f"📊 大盤 {head}"


def get_overnight_us() -> dict:
    """美股隔夜表現：SOX、TSM ADR、NVDA。big_drop: 任一檔 ≤ -2.5%"""
    now = time.time()
    if _overnight_cache["data"] and now - _overnight_cache["ts"] < _OVERNIGHT_TTL:
        return _overnight_cache["data"]
    syms = {"^SOX": "SOX", "TSM": "TSM-ADR", "NVDA": "NVDA"}
    rows = {}
    for sym, label in syms.items():
        d = _yf_change(sym)
        if d:
            rows[sym] = {"label": label, "change_pct": d["change_pct"]}
    changes  = [r["change_pct"] for r in rows.values()]
    avg      = sum(changes) / len(changes) if changes else 0.0
    big_drop = any(c <= -2.5 for c in changes)
    big_rally = any(c >=  2.5 for c in changes)
    result = {"rows": rows, "avg": avg, "big_drop": big_drop, "big_rally": big_rally}
    _overnight_cache["data"] = result
    _overnight_cache["ts"]   = now
    return result


# ══════════════════════════════════════════════════════════════════════
# 法說會行事曆（抓 cmoney 個股行事曆頁）
# ══════════════════════════════════════════════════════════════════════

_event_cache: dict[str, dict] = {}
_EVENT_TTL = 86400  # 法說會日期 1 天抓一次


def fetch_earnings_call(code: str) -> dict | None:
    """抓 cmoney 該股最近一次法說會。回傳 {date, topic} 或 None"""
    import re
    try:
        r = requests.get(
            f"https://www.cmoney.tw/forum/stock/{code}?s=calendar",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        if not r.ok:
            return None
        m = re.search(r'allTableData:(\[\[.*?\]\])', r.text)
        if not m:
            return None
        block = m.group(1)
        d = re.search(r'"開會日期","([^"]+)"', block)
        t = re.search(r'"法說會 擇要訊息","([^"]+)"', block)
        if not d:
            return None
        topic = (t.group(1) if t else "").replace("\\u002F", "/")
        # 主題太長截斷
        if len(topic) > 50:
            topic = topic[:50] + "…"
        return {"date": d.group(1), "topic": topic}
    except Exception as e:
        log.debug(f"法說會抓取失敗 ({code}): {e}")
        return None


def get_event_window(code: str) -> dict | None:
    """回傳法說會視窗狀態：None 或 {date, topic, days_until, phase}
    phase: pre（前 1-7 天）/ today / post（後 1-5 天）"""
    now = time.time()
    cached = _event_cache.get(code)
    if cached and now - cached["ts"] < _EVENT_TTL:
        info = cached["info"]
    else:
        info = fetch_earnings_call(code)
        _event_cache[code] = {"info": info, "ts": now}
    if not info:
        return None
    try:
        from datetime import date as _date
        y, m, d = info["date"].split("-")
        ev_date = _date(int(y), int(m), int(d))
        today = datetime.now(TW_TZ).date()
        days_until = (ev_date - today).days
        if -5 <= days_until <= 7:
            if days_until > 0:
                phase = "pre"
            elif days_until == 0:
                phase = "today"
            else:
                phase = "post"
            return {**info, "days_until": days_until, "phase": phase}
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════
# 三大法人籌碼（證交所 T86）
# ══════════════════════════════════════════════════════════════════════

_T86_FILE   = os.path.join(DATA_DIR, 't86_history.json')
_t86_cache  = {"data": None, "ts": 0.0}
_T86_TTL    = 21600  # 6 小時抓一次（盤後資料盤後公布）


def _parse_int(s) -> int:
    try:
        return int(str(s).replace(',', '').strip())
    except Exception:
        return 0


def _t86_load() -> dict:
    if not os.path.exists(_T86_FILE):
        return {}
    try:
        with open(_T86_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _t86_save(data: dict):
    keys = sorted(data.keys())[-10:]  # 只保留最近 10 個交易日
    with open(_T86_FILE, 'w', encoding='utf-8') as f:
        json.dump({k: data[k] for k in keys}, f, ensure_ascii=False)


def fetch_twse_t86(date_str: str) -> dict | None:
    """抓某日三大法人買賣超。date_str: YYYYMMDD"""
    try:
        r = requests.get(
            f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALL&response=json",
            timeout=15,
        )
        if not r.ok:
            return None
        d = r.json()
        if d.get("stat") != "OK":
            return None
        result = {}
        for row in d.get("data", []):
            if len(row) < 19:
                continue
            code = row[0].strip()
            result[code] = {
                "fii":    _parse_int(row[4]),    # 外陸資買賣超
                "trust":  _parse_int(row[10]),   # 投信
                "dealer": _parse_int(row[11]),   # 自營商
                "total":  _parse_int(row[18]),   # 三大法人合計
            }
        return result
    except Exception as e:
        log.debug(f"T86 抓取失敗 ({date_str}): {e}")
        return None


def update_t86() -> dict:
    """抓最近 5 個交易日的 T86 並更新 history（缺漏才抓）。
    智能 TTL：盤後 17:30 後若今日資料還沒抓到，強制重抓。"""
    now = time.time()
    today_dt   = datetime.now(TW_TZ)
    today_date = today_dt.date()
    today_str  = today_date.strftime("%Y%m%d")
    history    = _t86_load()

    after_release = (today_dt.hour, today_dt.minute) >= (17, 30)
    is_weekday    = today_date.weekday() < 5
    needs_today   = after_release and is_weekday and today_str not in history

    if (_t86_cache["data"] and
        now - _t86_cache["ts"] < _T86_TTL and
        not needs_today):
        return _t86_cache["data"]

    found = 0
    for n in range(10):  # 倒回最多 10 天找 5 個交易日
        d = today_date - timedelta(days=n)
        if d.weekday() >= 5:
            continue
        date_str = d.strftime("%Y%m%d")
        if date_str not in history:
            data = fetch_twse_t86(date_str)
            if data:
                history[date_str] = data
        if date_str in history:
            found += 1
        if found >= 5:
            break
    _t86_save(history)
    _t86_cache["data"] = history
    _t86_cache["ts"]   = now
    return history


def get_inst_pulse(code: str) -> dict | None:
    """個股三大法人動向：連續日數、警示旗標"""
    history = update_t86()
    dates = sorted(history.keys())[-5:]
    rows = []
    for d in dates:
        rec = history[d].get(code)
        if rec:
            rows.append({"date": d, **rec})
    if len(rows) < 3:
        return None
    # 連續日數（從最新往前數，看 fii 是否連續同方向）
    fii_sell = fii_buy = 0
    for r in reversed(rows):
        if r["fii"] < 0:
            if fii_buy == 0: fii_sell += 1
            else: break
        elif r["fii"] > 0:
            if fii_sell == 0: fii_buy += 1
            else: break
        else:
            break
    return {
        "recent": rows,
        "fii_consec_sell": fii_sell,
        "fii_consec_buy":  fii_buy,
        "alarm_sell": fii_sell >= 3,
        "alarm_buy":  fii_buy  >= 3,
    }


def inst_summary_line(pulse: dict | None) -> str:
    if not pulse:
        return ""
    last = pulse["recent"][-1]
    fii_k = last["fii"] / 1000
    sign = "+" if fii_k >= 0 else ""
    base = f"昨日外資 {sign}{fii_k:,.0f}K"
    if pulse["alarm_sell"]:
        return f"⚠ 外資連 {pulse['fii_consec_sell']} 日賣超｜{base}"
    if pulse["alarm_buy"]:
        return f"🔥 外資連 {pulse['fii_consec_buy']} 日買超｜{base}"
    return f"📊 {base}"


def event_summary_line(ev: dict | None) -> str:
    if not ev:
        return ""
    if ev["phase"] == "pre":
        return f"📅 距 {ev['date']} 法說會 {ev['days_until']} 天（慎追利多）"
    if ev["phase"] == "today":
        return f"📅 今天 {ev['date']} 法說會"
    return f"📅 法說會已過 {-ev['days_until']} 天（{ev['date']}），留意利多兌現賣壓"


def overnight_summary_line(ov: dict) -> str:
    if not ov["rows"]:
        return ""
    parts = []
    for sym in ("^SOX", "TSM", "NVDA"):
        r = ov["rows"].get(sym)
        if r:
            s = "+" if r["change_pct"] >= 0 else ""
            parts.append(f"{r['label']} {s}{r['change_pct']:.1f}%")
    head = "隔夜 " + "｜".join(parts)
    if ov["big_drop"]:
        return f"⚠ {head}（半導體重挫，跳空風險）"
    if ov["big_rally"]:
        return f"🔥 {head}（半導體大漲）"
    return f"📊 {head}"


def check_stock(code: str, scfg: dict, intraday: dict, cfg: dict):
    price    = intraday["price"]
    name     = scfg.get("name", code)
    now_s    = datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M")
    buy_thr  = cfg.get("buy_threshold", 5)
    sell_thr = cfg.get("sell_threshold", 5)

    log.info(f"  分析技術指標（{name} {code}）…")
    buy_r  = buy_analysis(code, price, scfg, intraday)
    sell_r = sell_analysis(code, price, scfg, intraday)

    # 族群 + 新聞 + 大盤 + 美股隔夜 + 法說會 + 三大法人籌碼（皆有 cache）
    peer_grp  = scfg.get("peer_group")  # None 則不查族群
    pulse     = get_peer_pulse(peer_grp) if peer_grp else None
    news      = scan_news(code, name)
    market    = get_market_pulse()
    overnight = get_overnight_us()
    event     = get_event_window(code)
    inst      = get_inst_pulse(code)
    in_event  = event is not None
    peer_down = pulse is not None and pulse["alarm_down"]
    peer_up   = pulse is not None and pulse["alarm_up"]
    # 新聞純推播，不再參與 risk 評分（避免誤觸抑制買賣訊）
    risk      = (peer_down or
                 market["alarm"] or overnight["big_drop"] or in_event or
                 (inst is not None and inst["alarm_sell"]))
    boost     = (peer_up or market["boost"] or overnight["big_rally"] or
                 (inst is not None and inst["alarm_buy"]))
    peer_ln   = peer_summary_line(pulse) if pulse else ""
    mkt_ln    = market_summary_line(market)
    ov_ln     = overnight_summary_line(overnight)
    ev_ln     = event_summary_line(event)
    inst_ln   = inst_summary_line(inst)
    context   = "\n".join(x for x in (mkt_ln, peer_ln, ov_ln, ev_ln, inst_ln) if x)
    if context:
        context += "\n"

    # 風險旗標：抑制買訊、加強賣訊
    eff_buy_thr  = buy_thr  + (1 if risk  else 0)
    eff_sell_thr = sell_thr - (1 if risk  else 0)
    # 族群同步噴出時，買訊門檻降 1（順勢）
    if boost and not risk:
        eff_buy_thr = max(buy_thr - 1, 3)

    holding      = _load()["holdings"].get(code, {})
    has_position = bool(holding)

    b_score = buy_r.get("score", 0) if "error" not in buy_r else 0
    s_score = sell_r.get("score", 0) if "error" not in sell_r else 0

    risk_reasons = {
        "peer_down":   peer_down,
        "news_bad":    news["has_bad_news"],
        "market":      market["alarm"],
        "overnight":   overnight["big_drop"],
        "event":       in_event,
        "inst_sell":   bool(inst and inst["alarm_sell"]),
    }

    # 獨立的負面新聞警示（不受買賣訊冷卻影響、有自己的 alert_key）
    if news["alert_msg"]:
        notify(f"{code}_news", news["alert_msg"] + f"\n⏰ {now_s}",
               f"新聞警示｜{name}", "high")
        record_signal(code, name, "news", price=price, risk_flag=True,
                      risk_reasons=risk_reasons, message=news["alert_msg"])

    # 買訊：達（調整後）門檻、買分 > 賣分、且非風險狀態（sell_only 標的跳過買訊）
    sell_only = scfg.get("sell_only", False)
    if (not sell_only and "error" not in buy_r and b_score >= eff_buy_thr
            and b_score > s_score):
        msg = (f"🟢 {name}（{code}）{buy_r['level']}\n"
               f"{context}"
               f"{_price_line(intraday)}\n"
               f"{decision_line(buy_r)}"
               f"{_suggest_shares(code, scfg, price, cfg, b_score)}"
               f"{_holding_note(code, price)}\n"
               f"⏰ {now_s}")
        notify(f"{code}_buy", msg, f"買進訊號｜{name}", "high")
        record_signal(code, name, "buy", score=b_score, threshold=eff_buy_thr,
                      price=price, risk_flag=False,
                      risk_reasons=risk_reasons, message=msg)
    elif not sell_only and "error" not in buy_r and b_score >= buy_thr and risk:
        # 達原始門檻但被 risk 抑制：仍記錄（但不推播）— 用於回測「有 risk 抑制 vs 沒抑制」對比
        record_signal(code, name, "buy_suppressed", score=b_score, threshold=buy_thr,
                      price=price, risk_flag=True, risk_reasons=risk_reasons,
                      message=f"buy_score={b_score} 達門檻但 risk=True 抑制")

    if has_position and not scfg.get("no_sell_alert") and "error" not in sell_r and s_score >= eff_sell_thr and s_score > b_score:
        avg_cost = holding["avg_cost"]
        pnl_pct  = (price - avg_cost) / avg_cost * 100
        if pnl_pct <= -5:
            icon, action = "🚨", "建議停損出場"
            sell_hint = _suggest_sell_shares(code, price, 1.0, "全數出場")
        elif pnl_pct >= 3:
            icon, action = "🔴", "建議停利出場"
            sell_hint = _suggest_sell_shares(code, price, 0.5, "先賣一半")
        else:
            icon, action = "🟠", "建議減碼觀察"
            sell_hint = _suggest_sell_shares(code, price, 1/3, "先賣 1/3")
        risk_tag = "（族群/大盤風險，門檻已下調）" if risk else ""
        msg = (f"{icon} {name}（{code}）{action}{risk_tag}\n"
               f"{context}"
               f"{_price_line(intraday)}\n"
               f"{decision_line(sell_r)}"
               f"{sell_hint}"
               f"{_holding_note(code, price)}\n"
               f"⏰ {now_s}")
        notify(f"{code}_sell", msg, f"賣出訊號｜{name}", "urgent" if pnl_pct <= -5 else "high")
        record_signal(code, name, "sell", score=s_score, threshold=eff_sell_thr,
                      price=price, pnl_pct=pnl_pct, risk_flag=risk,
                      risk_reasons=risk_reasons, message=msg)


# ══════════════════════════════════════════════════════════════════════
# 訊號回測 + quantstats 週報
# ══════════════════════════════════════════════════════════════════════

def backtest_signals(start_ts: str = None, end_ts: str = None) -> dict:
    """重放 signal DB 訊號當作交易，產出每筆 closed trade 與每日報酬序列"""
    rows = query_signals(start_ts, end_ts)
    positions: dict = {}     # code -> {entry_price, entry_ts}
    trades: list = []
    for r in rows:
        sig = r["signal_type"]
        code = r["code"]
        price = r["price"]
        if price is None:
            continue
        if sig == "buy" and code not in positions:
            positions[code] = {"entry_price": price, "entry_ts": r["timestamp"]}
        elif sig == "sell" and code in positions:
            pos = positions.pop(code)
            ret = (price - pos["entry_price"]) / pos["entry_price"]
            trades.append({
                "code": code, "name": r["name"],
                "open_ts": pos["entry_ts"], "close_ts": r["timestamp"],
                "entry_price": pos["entry_price"], "exit_price": price,
                "return": ret,
            })
    return {"trades": trades, "open_positions": positions, "raw_count": len(rows)}


def _trades_to_daily_returns(trades: list):
    """把交易序列轉成每日報酬 pandas Series"""
    if not trades:
        return None
    df = pd.DataFrame(trades)
    df["close_date"] = pd.to_datetime(df["close_ts"]).dt.tz_localize(None).dt.date
    daily = df.groupby("close_date")["return"].sum()
    daily.index = pd.to_datetime(daily.index)
    # 補齊缺日（沒交易那天 = 0 報酬）
    full_range = pd.date_range(daily.index.min(), daily.index.max(), freq='D')
    daily = daily.reindex(full_range, fill_value=0)
    return daily


def generate_weekly_report(weeks: int = 1) -> str | None:
    """產生 quantstats HTML 報表，回傳檔案路徑"""
    try:
        import quantstats as qs
    except ImportError:
        log.warning("quantstats 未安裝，無法產生週報")
        return None
    end   = datetime.now(TW_TZ)
    start = end - timedelta(weeks=weeks)
    bt    = backtest_signals(start.isoformat(), end.isoformat())
    if not bt["trades"]:
        log.info(f"近 {weeks} 週無已完成交易，跳過週報")
        return None
    daily = _trades_to_daily_returns(bt["trades"])
    if daily is None or len(daily) < 2:
        return None
    out_path = os.path.join(DATA_DIR, f'report_{end.strftime("%Y%m%d")}.html')
    try:
        qs.reports.html(daily, output=out_path,
                        title=f"Stock Monitor 週報 ({start.strftime('%m/%d')}-{end.strftime('%m/%d')})")
    except Exception as e:
        log.error(f"quantstats 報表生成失敗：{e}")
        return None
    return out_path


def send_telegram_document(file_path: str, caption: str = "") -> bool:
    """送檔案（HTML/PDF）到 Telegram"""
    token, chat = _tg_credentials()
    if not (token and chat):
        return False
    try:
        with open(file_path, 'rb') as f:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat, "caption": caption},
                files={"document": f},
                timeout=60,
            )
        if not r.ok:
            log.warning(f"Telegram sendDocument 異常：{r.status_code} {r.text[:200]}")
        return r.ok
    except Exception as e:
        log.error(f"Telegram 檔案上傳失敗：{e}")
        return False


def _trade_summary_text(bt: dict) -> str:
    """產出交易摘要文字（搭配 HTML 報表寄出）"""
    trades = bt["trades"]
    if not trades:
        return "本週無已完成交易"
    n = len(trades)
    wins = sum(1 for t in trades if t["return"] > 0)
    avg_ret = sum(t["return"] for t in trades) / n
    total_ret = sum(t["return"] for t in trades)
    best = max(trades, key=lambda t: t["return"])
    worst = min(trades, key=lambda t: t["return"])
    lines = [
        f"📊 *Stock Monitor 週報*",
        f"• 交易數：{n} 筆（勝 {wins}）",
        f"• 平均單筆報酬：{avg_ret*100:+.2f}%",
        f"• 總累計報酬：{total_ret*100:+.2f}%",
        f"• 最佳：{best['name']}({best['code']}) {best['return']*100:+.1f}%",
        f"• 最差：{worst['name']}({worst['code']}) {worst['return']*100:+.1f}%",
        f"• 仍持倉：{len(bt['open_positions'])} 檔",
    ]
    return "\n".join(lines)


def _run_weekly_report():
    """產生週報並推送（無條件跑）"""
    log.info("執行週報生成…")
    end   = datetime.now(TW_TZ)
    start = end - timedelta(weeks=1)
    bt    = backtest_signals(start.isoformat(), end.isoformat())
    if not bt["trades"]:
        send_telegram("週報", "本週無已完成交易（沒有觸發過完整 buy→sell 週期）", "low")
        return
    summary = _trade_summary_text(bt)
    send_telegram("Stock Monitor 週報", summary, "high")
    path = generate_weekly_report(weeks=1)
    if path and os.path.exists(path):
        send_telegram_document(path, "📊 完整 quantstats 報表")
        log.info("週報已寄出")
    else:
        log.info("quantstats 詳細報表生成失敗，僅推文字摘要")


def weekly_report_job():
    """排程用：每週日台北時間 21:00 執行"""
    if datetime.now(TW_TZ).weekday() != 6:  # 0=Mon, 6=Sun
        return
    _run_weekly_report()


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
    market    = get_market_pulse()
    overnight = get_overnight_us()
    if (mkt := market_summary_line(market)):       log.info(f"  {mkt}")
    if (ov  := overnight_summary_line(overnight)): log.info(f"  {ov}")
    # 收集本次 cfg 用到的所有 peer group，每個只 log 一次
    used_groups = {scfg.get("peer_group") for scfg in cfg.get("stocks", {}).values()
                   if scfg.get("peer_group")}
    for g in used_groups:
        p = get_peer_pulse(g)
        if p["rows"]:
            log.info(f"  {peer_summary_line(p)}")
    prices = {}
    for code, scfg in cfg.get("stocks", {}).items():
        # 個股事件 / 籌碼提示
        ev = get_event_window(code)
        if ev:
            log.info(f"  {scfg.get('name', code)} {event_summary_line(ev)}")
        inst = get_inst_pulse(code)
        if inst and (inst["alarm_sell"] or inst["alarm_buy"]):
            log.info(f"  {scfg.get('name', code)} {inst_summary_line(inst)}")
        # 抓即時價並執行檢查
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


def initial_cash_check():
    """首次啟動：若 cash 為 0 且尚未初始化過，就把 cash 設為 initial_cash。
    之後 cash 隨買賣自然變動，不再做月度重置。"""
    cfg = load_config()
    initial_cash = cfg.get("initial_cash")
    if not initial_cash:
        return
    data = _load()
    if data.get("cash_initialized"):
        return
    if data.get("cash", 0) == 0 and not data.get("transactions"):
        data["cash"] = initial_cash
        log.info(f"首次啟動：cash 設為起始金額 {initial_cash:,.0f}")
    data["cash_initialized"] = True
    _pf_save(data)


def _monitor_thread():
    crash_count = 0
    while True:
        try:
            _monitor_status["running"] = True
            cfg      = load_config()
            interval = cfg.get("check_interval_minutes", 1)
            token, chat = _tg_credentials()
            tg_ok = bool(token and chat)
            kind = "重啟" if crash_count > 0 else "啟動"
            log.info(f"監控執行緒{kind}（每 {interval} 分鐘，Telegram={'已設定' if tg_ok else '未設定⚠'}）")
            if tg_ok and crash_count == 0:
                send_telegram("股市監控啟動",
                              f"監控執行緒已啟動，每 {interval} 分鐘檢查一次\n"
                              f"監控股票：{', '.join(cfg.get('stocks', {}).keys())}",
                              "low")
            schedule.clear()  # 重啟時清除舊 jobs，避免重複註冊
            initial_cash_check()  # 首次啟動時設定起始現金
            run_check()
            schedule.every(interval).minutes.do(run_check)
            # 週報：UTC 13:00 = 台北時間 21:00；每天觸發但 job 內部判斷只在週日跑
            schedule.every().day.at("13:00").do(weekly_report_job)
            while True:
                try:
                    schedule.run_pending()
                except Exception as e:
                    log.error(f"排程執行錯誤：{e}")
                    _monitor_status["last_error"] = str(e)
                time.sleep(5)
        except Exception as e:
            crash_count += 1
            _monitor_status["running"] = False
            _monitor_status["last_error"] = f"crash#{crash_count}: {e}"
            log.error(f"監控執行緒崩潰（第 {crash_count} 次）：{e}，30 秒後重啟")
            time.sleep(30)


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


@app.route("/api/import_holding", methods=["POST"])
def api_import_holding():
    """直接寫入既有持倉（不扣現金、不建交易紀錄）。
    用於匯入月初前就買進的部位。
    payload: {code, name, shares, avg_cost}"""
    d = request.json or {}
    code     = str(d["code"])
    name     = str(d.get("name", code))
    shares   = int(d["shares"])
    avg_cost = float(d["avg_cost"])
    data = _load()
    data["holdings"][code] = {
        "name": name,
        "shares": shares,
        "avg_cost": avg_cost,
        "total_cost": shares * avg_cost,
    }
    _pf_save(data)
    return jsonify({"ok": True, "holdings": data["holdings"][code]})


@app.route("/api/delete_holding", methods=["POST"])
def api_delete_holding():
    """刪除指定持倉（不影響現金、不建交易紀錄）。"""
    d = request.json or {}
    code = str(d["code"])
    data = _load()
    removed = data["holdings"].pop(code, None)
    _pf_save(data)
    return jsonify({"ok": True, "removed": removed})


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
    token, chat = _tg_credentials()
    return jsonify({
        "time_tw": datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M:%S"),
        "market_open": is_market_hours(),
        "telegram": "已設定" if (token and chat) else "(未設定)",
        "buy_threshold": cfg.get("buy_threshold", 5),
        "stocks": result,
    })


@app.route("/api/status")
def api_status():
    cfg = load_config()
    token, chat = _tg_credentials()
    src = "env" if (TG_BOT_TOKEN and TG_CHAT_ID) else \
          ("config" if (cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")) else "none")
    return jsonify({
        "monitor": _monitor_status,
        "telegram": "已設定" if (token and chat) else "(未設定)",
        "telegram_source": src,
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


@app.route("/api/signals")
def api_signals():
    """查詢訊號歷史。?days=7 限定最近 N 天。"""
    days = int(request.args.get("days", 7))
    start = (datetime.now(TW_TZ) - timedelta(days=days)).isoformat()
    rows = query_signals(start_ts=start)
    return jsonify({"count": len(rows), "rows": rows})


@app.route("/api/backtest")
def api_backtest():
    days = int(request.args.get("days", 7))
    start = (datetime.now(TW_TZ) - timedelta(days=days)).isoformat()
    bt = backtest_signals(start_ts=start)
    return jsonify({
        "trades": bt["trades"],
        "open_positions": bt["open_positions"],
        "raw_signal_count": bt["raw_count"],
    })


@app.route("/api/weekly_report", methods=["POST"])
def api_weekly_report():
    """手動觸發週報（不限週日）"""
    threading.Thread(target=_run_weekly_report, daemon=True).start()
    return jsonify({"ok": True, "message": "週報生成中，完成後會推 Telegram"})


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
