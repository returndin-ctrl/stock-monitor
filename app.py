#!/usr/bin/env python3
"""
台股監控 Web App（雲端版）
部署到 Railway，手機隨時可用，筆電不需開著
"""

import sys, os, time, json, threading, logging, requests, schedule, pytz
from datetime import datetime, time as dtime
from flask import Flask, jsonify, request, render_template

# ── 環境變數 ──────────────────────────────────────────────────────────
PORT        = int(os.environ.get('PORT', 8080))
DATA_DIR    = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(__file__), 'data'))
NTFY_TOPIC  = os.environ.get('NTFY_TOPIC', '')

os.makedirs(DATA_DIR, exist_ok=True)

PORTFOLIO_FILE = os.path.join(DATA_DIR, 'portfolio.json')
CONFIG_FILE    = os.path.join(DATA_DIR, 'config.json')

# ── 預設 config（首次啟動時寫入）────────────────────────────────────
DEFAULT_CONFIG = {
    "line_notify_token": "",
    "check_interval_minutes": 5,
    "stocks": {
        "2330": {
            "name": "台積電",
            "budget": 50000,
            "conditions": [
                {"type": "buy_below",  "price": 2000, "shares": 24,
                 "label": "買進訊號：建議買入 24 股零股（約 48,000 元）"},
                {"type": "sell_above", "price": 2200,
                 "label": "停利訊號：建議賣出，預估獲利 +4,800 元"},
                {"type": "stop_loss",  "price": 1920,
                 "label": "停損警報：建議立即賣出，控制虧損在 -3,800 元內"}
            ]
        },
        "2408": {
            "name": "南亞科",
            "budget": 50000,
            "conditions": [
                {"type": "buy_below",  "price": 190, "shares": 260,
                 "label": "買進訊號：建議買入 260 股零股（約 49,400 元）"},
                {"type": "sell_above", "price": 240,
                 "label": "停利訊號：建議賣出，預估獲利 +13,000 元"},
                {"type": "stop_loss",  "price": 175,
                 "label": "停損警報：建議立即賣出"}
            ]
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

TW_TZ = pytz.timezone('Asia/Taipei')
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


def set_cash(amount: float) -> dict:
    data = _load()
    data["cash"] = amount
    _pf_save(data)
    return data


def buy(stock_code: str, stock_name: str, shares: int, price: float) -> dict:
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


def sell(stock_code: str, shares: int, price: float) -> dict:
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
    data      = _load()
    cash      = data["cash"]
    realized  = data["realized_pnl"]
    holdings  = data["holdings"]
    rows, stock_value, unrealized = [], 0.0, 0.0

    for code, h in holdings.items():
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
# 股價取得
# ══════════════════════════════════════════════════════════════════════

MARKET_OPEN  = dtime(9, 0)
MARKET_CLOSE = dtime(13, 30)


def is_market_hours() -> bool:
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def _twse_price(code: str) -> float | None:
    try:
        url = (f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
               f"?ex_ch=tse_{code}.tw&json=1&delay=0")
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        }, timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("rtmessage") == "OK" and d.get("msgArray"):
            raw = d["msgArray"][0].get("z") or d["msgArray"][0].get("y") or ""
            if raw and raw != "-":
                return float(raw)
    except Exception:
        pass
    return None


def _yahoo_price(code: str) -> float | None:
    try:
        import yfinance as yf
        hist = yf.Ticker(f"{code}.TW").history(period="1d", interval="5m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


def get_price(code: str) -> float | None:
    if is_market_hours():
        p = _twse_price(code)
        if p:
            return p
    return _yahoo_price(code)


# ══════════════════════════════════════════════════════════════════════
# 股價快取
# ══════════════════════════════════════════════════════════════════════

_cache: dict      = {}
_cache_lock       = threading.Lock()
_last_fetch: float = 0.0


def get_prices() -> dict:
    global _last_fetch, _cache
    now = time.time()
    if now - _last_fetch > 30:
        cfg   = load_config()
        fresh = {}
        for code in cfg.get("stocks", {}):
            p = get_price(code)
            if p:
                fresh[code] = p
        with _cache_lock:
            if fresh:
                _cache = fresh
            _last_fetch = now
    with _cache_lock:
        return dict(_cache)


# ══════════════════════════════════════════════════════════════════════
# 監控 & LINE 通知
# ══════════════════════════════════════════════════════════════════════

_notified: dict[str, float] = {}
NOTIFY_COOLDOWN_SEC = 1800

CONDITION_META = {
    "buy_below":  ("🟢", "【買進訊號】", "跌至 {target} 以下，現價 {price:.0f} 元"),
    "sell_above": ("🔴", "【停利訊號】", "漲至 {target} 以上，現價 {price:.0f} 元"),
    "stop_loss":  ("🚨", "【停損警報】", "跌破 {target}，現價 {price:.0f} 元  立刻賣出！"),
}


def load_config() -> dict:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def send_ntfy(title: str, message: str, priority: str = "default") -> bool:
    topic = NTFY_TOPIC or load_config().get("ntfy_topic", "")
    if not topic:
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "chart_increasing"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"ntfy 通知失敗：{e}")
        return False


def notify(token: str, alert_key: str, message: str, title: str = "📈 台股監控", priority: str = "default"):
    now_ts = time.time()
    if now_ts - _notified.get(alert_key, 0) < NOTIFY_COOLDOWN_SEC:
        return
    _notified[alert_key] = now_ts
    log.info(f"  ➜ 推播：{message[:60]}…")
    send_ntfy(title, message, priority)


def check_stock(code: str, scfg: dict, token: str, price: float):
    name  = scfg.get("name", code)
    now_s = datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M")

    for cond in scfg.get("conditions", []):
        ctype, target = cond["type"], cond["price"]
        triggered = (
            (ctype == "buy_below"  and price <= target) or
            (ctype == "sell_above" and price >= target) or
            (ctype == "stop_loss"  and price <= target)
        )
        if not triggered:
            continue
        icon, title, desc_tpl = CONDITION_META.get(ctype, ("⚡", f"[{ctype}]", "{price:.0f}"))
        desc = desc_tpl.format(price=price, target=target)
        holding = _load()["holdings"].get(code, {})
        holding_note = ""
        if holding:
            holding_note = (f"\n── 你的持倉 ──\n"
                            f"持有 {holding['shares']} 股  均價 {holding['avg_cost']:,.0f} 元")
        body = (f"{desc}\n{cond.get('label','')}{holding_note}\n⏰ {now_s}")
        ntfy_priority = "urgent" if ctype == "stop_loss" else "high"
        notify(token, f"{code}_{ctype}_{target}", body,
               title=f"{icon} {name}（{code}）{title}", priority=ntfy_priority)


def run_check():
    cfg   = load_config()
    now_s = datetime.now(TW_TZ).strftime("%H:%M")
    if not is_market_hours():
        log.info(f"[{now_s}] 非交易時段，等待中…")
        return
    log.info(f"{'─'*40}")
    log.info(f"[{now_s}] 開始檢查股價")
    prices = {}
    for code, scfg in cfg.get("stocks", {}).items():
        p = get_price(code)
        if p is None:
            log.warning(f"  {scfg.get('name',code)} ({code})：無法取得股價")
            continue
        prices[code] = p
        log.info(f"  {scfg.get('name',code)} ({code})：{p:,.0f} 元")
        check_stock(code, scfg, "", p)
    summary = get_portfolio_summary(prices)
    now = datetime.now(TW_TZ)
    if now.minute < 6:
        lines = ["\n📊 即時持倉摘要"]
        for r in summary["rows"]:
            lines.append(f"  {r['name']}({r['code']}) {r['shares']}股"
                         f" | 均{r['avg_cost']:,.0f} 現{r['price']}"
                         f" | {'▲' if r['pnl']>=0 else '▼'}{abs(r['pnl']):,.0f}元({r['pnl_pct']:+.1f}%)")
        lines.append(f"  現金：{summary['cash']:,.0f} 元")
        lines.append(f"  總資產：{summary['total_assets']:,.0f} 元")
        notify("", f"portfolio_hourly_{now.hour}", "\n".join(lines),
               title="📊 每小時持倉摘要")


def _monitor_thread():
    cfg      = load_config()
    interval = cfg.get("check_interval_minutes", 5)
    run_check()
    schedule.every(interval).minutes.do(run_check)
    log.info(f"監控執行緒啟動（每 {interval} 分鐘）")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ══════════════════════════════════════════════════════════════════════
# Flask API
# ══════════════════════════════════════════════════════════════════════

app = Flask(__name__)


@app.route("/api/portfolio")
def api_portfolio():
    prices  = get_prices()
    summary = get_portfolio_summary(prices)
    cfg     = load_config()
    stocks_cfg = {
        code: {"name": s.get("name", code), "conditions": s.get("conditions", [])}
        for code, s in cfg.get("stocks", {}).items()
    }
    return jsonify({"summary": summary, "prices": prices,
                    "stocks_cfg": stocks_cfg, "market": is_market_hours()})


@app.route("/api/buy", methods=["POST"])
def api_buy():
    d = request.json or {}
    try:
        result = buy(d["code"], d.get("name", d["code"]),
                     int(d["shares"]), float(d["price"]))
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/sell", methods=["POST"])
def api_sell():
    d = request.json or {}
    try:
        result = sell(d["code"], int(d["shares"]), float(d["price"]))
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
    data = _load()
    return jsonify(list(reversed(data.get("transactions", []))))


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
    t = threading.Thread(target=_monitor_thread, daemon=True)
    t.start()
    log.info(f"台股監控雲端版啟動中，PORT={PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
