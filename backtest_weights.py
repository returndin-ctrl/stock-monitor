"""
回測 SIGNAL_WEIGHTS_BUY：對歷史 OHLCV 跑每條訊號，統計觸發後 t+1 收盤平均報酬。
Output: 建議的 SIGNAL_WEIGHTS_BUY dict（可手動覆寫 app.py 裡的預設）。
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np
import pandas as pd
import yfinance as yf
from collections import defaultdict

import app  # 重用 _rsi/_kd/_macd/_ma_data/_vol_ratio

STOCKS = ["2303", "2308", "2317", "2324", "2330", "2344", "2382", "2408", "2454", "3231"]
PERIOD = "2y"


def fetch(code):
    for suf in (".TW", ".TWO"):
        try:
            df = yf.Ticker(f"{code}{suf}").history(period=PERIOD, interval="1d")
            if len(df) >= 200:
                return df
        except Exception:
            pass
    return None


def signal_triggers(df, scfg):
    """對每一行（t），回傳 dict {signal_name: bool}。i=index in df."""
    n = len(df)
    out = []
    close = df["Close"]; high = df["High"]; low = df["Low"]
    open_ = df["Open"]; volume = df["Volume"]
    support = scfg.get("support_price")
    for i in range(60, n - 1):  # 跳過前 60 日（指標暖機），保留 t+1
        sub_close = close.iloc[:i+1]
        sub_high  = high.iloc[:i+1]
        sub_low   = low.iloc[:i+1]
        sub_vol   = volume.iloc[:i+1]
        trig = {}
        try:
            trig["RSI"] = app._rsi(sub_close) < 35
        except Exception: trig["RSI"] = False
        try:
            k, d, kp, dp = app._kd(sub_high, sub_low, sub_close)
            trig["KD"] = (k < 30) or ((kp < dp) and (k > d))
        except Exception: trig["KD"] = False
        try:
            _, _, hist, hist_prev = app._macd(sub_close)
            macd_v, signal_v, _, _ = app._macd(sub_close)
            trig["MACD"] = ((hist < 0) and (hist > hist_prev)) or (macd_v > signal_v)
        except Exception: trig["MACD"] = False
        try:
            ma = app._ma_data(sub_close)
            trig["均線趨勢"] = ma["ma5"] > ma["ma5_prev"]
        except Exception: trig["均線趨勢"] = False
        try:
            trig["成交量"] = app._vol_ratio(sub_vol) >= 1.0
        except Exception: trig["成交量"] = False
        # 價格位置
        try:
            p = float(sub_close.iloc[-1])
            ma = app._ma_data(sub_close)
            bullish = False
            if support and (p <= support or (p - support)/support*100 <= 3):
                bullish = True
            if ma["ma60"] and p < ma["ma60"]:
                bullish = True
            elif p < ma["ma20"] and not support:
                bullish = True
            trig["價格位置"] = bullish
        except Exception: trig["價格位置"] = False
        # 反轉 K 棒
        try:
            o_, c_, h_, l_ = float(open_.iloc[i]), float(close.iloc[i]), float(high.iloc[i]), float(low.iloc[i])
            prev_change = (close.iloc[i-1] - close.iloc[i-2]) / close.iloc[i-2] * 100
            rng = h_ - l_
            shadow = (min(c_, o_) - l_) / rng if rng > 0 else 0
            trig["反轉K棒"] = (prev_change <= -7 and c_ >= o_ and shadow >= 0.5)
        except Exception: trig["反轉K棒"] = False
        # 下影爆量
        try:
            o_, c_, h_, l_ = float(open_.iloc[i]), float(close.iloc[i]), float(high.iloc[i]), float(low.iloc[i])
            rng = h_ - l_
            shadow = (min(c_, o_) - l_) / rng if rng > 0 else 0
            vr = app._vol_ratio(sub_vol)
            trig["下影爆量"] = (shadow >= 0.5 and vr >= 1.5 and c_ >= o_)
        except Exception: trig["下影爆量"] = False
        # 突破
        try:
            ma60 = sub_close.rolling(60).mean()
            hi20 = float(sub_high.iloc[-21:-1].max()) if i >= 21 else float("inf")
            cur = float(sub_close.iloc[-1])
            trig["突破"] = (cur > hi20
                            and not pd.isna(ma60.iloc[-1])
                            and not pd.isna(ma60.iloc[-2])
                            and ma60.iloc[-1] > ma60.iloc[-2]
                            and cur > float(ma60.iloc[-1]))
        except Exception: trig["突破"] = False
        # 法人轉買、盤中走勢 跳過（需要 T86 + intraday，回測複雜）
        out.append((i, trig))
    return out


def main():
    cfg = app.load_config()
    stocks_cfg = cfg.get("stocks", {})
    sig_returns = defaultdict(list)  # signal -> list of t+1 returns when triggered

    for code in STOCKS:
        scfg = stocks_cfg.get(code, {})
        df = fetch(code)
        if df is None:
            print(f"  skip {code}: no data")
            continue
        triggers = signal_triggers(df, scfg)
        close = df["Close"]
        for i, trig in triggers:
            r = (close.iloc[i+1] - close.iloc[i]) / close.iloc[i] * 100
            for sig, on in trig.items():
                if on:
                    sig_returns[sig].append(r)
        print(f"  {code}: {len(triggers)} bars analyzed")

    print("\n=== 訊號歷史 t+1 報酬統計（{} 檔，{} 期間）===".format(len(STOCKS), PERIOD))
    print(f"{'訊號':<10} {'n':>6} {'平均報酬':>10} {'中位數':>8} {'勝率':>6}")
    weights = {}
    for sig in ["RSI", "KD", "MACD", "均線趨勢", "成交量", "價格位置",
                "反轉K棒", "下影爆量", "突破"]:
        rs = sig_returns.get(sig, [])
        if not rs:
            print(f"{sig:<10} {'0':>6} {'—':>10} {'—':>8} {'—':>6}")
            weights[sig] = 0.0
            continue
        mean = float(np.mean(rs))
        med  = float(np.median(rs))
        wr   = float(np.mean(np.array(rs) > 0) * 100)
        print(f"{sig:<10} {len(rs):>6} {mean:>+9.2f}% {med:>+7.2f}% {wr:>5.1f}%")
        weights[sig] = round(max(0, mean), 2)  # 取 mean，負的歸 0

    # 法人轉買用一個保守的固定值（無 T86 歷史可回測）
    weights["法人轉買"] = 0.8
    # 盤中走勢類似
    weights["盤中走勢"] = 0.4

    print("\n=== 建議 SIGNAL_WEIGHTS_BUY ===")
    print(json.dumps(weights, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
