import time
import datetime
import requests
from collections import defaultdict

TELEGRAM_TOKEN   = "8617006078:AAEarZfA75pQBZpKgJegbztO9XuHhUJCeR0"
TELEGRAM_CHAT_ID = "8285816381"
SYMBOL           = "BTCUSDT"
TIMEFRAMES       = ["15m", "1h", "2h", "4h"]
CHECK_INTERVAL   = 60
MIN_SCORE        = 6
MIN_RR           = 1.5
ATR_TP_MULT      = 2.5
ATR_SL_MULT      = 1.0
FVG_MIN_GAP_PCT  = 0.08
OB_MIN_MOVE_PCT  = 0.30
VOL_WINDOW       = 10
SIGNAL_COOLDOWN  = 3600
BASE             = "https://fapi.binance.com"

def send_telegram(msg):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=data, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[텔레그램 오류] {e}")
        return False

def get_klines(interval, limit=100):
    url = f"{BASE}/fapi/v1/klines"
    params = {"symbol": SYMBOL, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    candles = []
    for k in r.json():
        candles.append({
            "open":     float(k[1]),
            "high":     float(k[2]),
            "low":      float(k[3]),
            "close":    float(k[4]),
            "volume":   float(k[5]),
            "buy_vol":  float(k[9]),
            "sell_vol": float(k[5]) - float(k[9]),
            "bull":     float(k[4]) >= float(k[1])
        })
    return candles

def get_ticker():
    url = f"{BASE}/fapi/v1/ticker/24hr"
    r   = requests.get(url, params={"symbol": SYMBOL}, timeout=10)
    r.raise_for_status()
    return r.json()

def calc_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i-1]
        trs.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"]  - p["close"])
        ))
    sl = trs[-period:]
    return sum(sl) / len(sl) if sl else 0

def detect_fvg(candles):
    fvgs = []
    for i in range(1, len(candles) - 1):
        a, b, c = candles[i-1], candles[i], candles[i+1]
        if c["low"] > a["high"]:
            gap = (c["low"] - a["high"]) / a["high"] * 100
            if gap >= FVG_MIN_GAP_PCT:
                fvgs.append({"type":"bull","top":c["low"],"bot":a["high"],"gap_pct":gap,"idx":i})
        if c["high"] < a["low"]:
            gap = (a["low"] - c["high"]) / a["low"] * 100
            if gap >= FVG_MIN_GAP_PCT:
                fvgs.append({"type":"bear","top":a["low"],"bot":c["high"],"gap_pct":gap,"idx":i})
    return fvgs

def detect_ob(candles):
    obs = []
    for i in range(2, len(candles) - 2):
        a, n1, n2 = candles[i], candles[i+1], candles[i+2]
        body = abs(a["close"] - a["open"]) / a["open"] * 100
        if not a["bull"] and n1["bull"] and n2["bull"]:
            mv = (n2["close"] - a["close"]) / a["close"] * 100
            if mv >= OB_MIN_MOVE_PCT and body > 0.05:
                obs.append({"type":"bull","top":max(a["open"],a["close"]),"bot":min(a["open"],a["close"]),"move_pct":mv,"idx":i})
        if a["bull"] and not n1["bull"] and not n2["bull"]:
            mv = (a["close"] - n2["close"]) / a["close"] * 100
            if mv >= OB_MIN_MOVE_PCT and body > 0.05:
                obs.append({"type":"bear","top":max(a["open"],a["close"]),"bot":min(a["open"],a["close"]),"move_pct":mv,"idx":i})
    return obs

def analyze_volume(candles):
    recent     = candles[-VOL_WINDOW:]
    total_buy  = sum(c["buy_vol"]  for c in recent)
    total_sell = sum(c["sell_vol"] for c in recent)
    total_vol  = total_buy + total_sell
    buy_pct    = total_buy  / total_vol * 100 if total_vol > 0 else 50
    sell_pct   = total_sell / total_vol * 100 if total_vol > 0 else 50
    avg_vol    = sum(c["volume"] for c in candles[-20:-3]) / 17 if len(candles) >= 20 else 0
    last_vol   = sum(c["volume"] for c in candles[-3:]) / 3
    vol_surge  = last_vol > avg_vol * 1.5 if avg_vol > 0 else False
    if buy_pct >= 55:   bias = "bull"
    elif sell_pct >= 55: bias = "bear"
    else:                bias = "neutral"
    return {"bias":bias,"buy_pct":round(buy_pct,1),"sell_pct":round(sell_pct,1),"vol_surge":vol_surge}

def detect_trend(candles):
    sl   = candles[-20:]
    h1   = max(c["high"]  for c in sl[-10:])
    h2   = max(c["high"]  for c in sl[:10])
    l1   = min(c["low"]   for c in sl[-10:])
    l2   = min(c["low"]   for c in sl[:10])
    ema  = sum(c["close"] for c in sl) / len(sl)
    last = candles[-1]["close"]
    hhhl = h1 > h2 and l1 > l2
    lllh = h1 < h2 and l1 < l2
    if hhhl and last > ema:       direction, strength = "bull", 2
    elif not lllh and last > ema: direction, strength = "bull", 1
    elif lllh and last < ema:     direction, strength = "bear", 2
    else:                         direction, strength = "bear", 1
    return {"direction":direction,"strength":strength,"ema20":round(ema,1),"hhhl":hhhl,"lllh":lllh}

def analyze(candles, price):
    atr   = calc_atr(candles)
    fvgs  = detect_fvg(candles)
    obs   = detect_ob(candles)
    vol   = analyze_volume(candles)
    trend = detect_trend(candles)
    near_fvg, fvg_score = None, 0
    for f in reversed(fvgs[-8:]):
        in_zone  = f["bot"]*0.998 <= price <= f["top"]*1.002
        dist_pct = min(abs(price-f["bot"]),abs(price-f["top"]))/price*100
        if in_zone:
            near_fvg=f; fvg_score=3 if f["type"]==trend["direction"] else 1; break
        elif dist_pct < 0.3 and near_fvg is None:
            near_fvg=f; fvg_score=2 if f["type"]==trend["direction"] else 1
    near_ob, ob_score = None, 0
    for o in reversed(obs[-6:]):
        in_zone  = o["bot"]*0.998 <= price <= o["top"]*1.002
        dist_pct = min(abs(price-o["bot"]),abs(price-o["top"]))/price*100
        if in_zone:
            near_ob=o; ob_score=3 if o["type"]==trend["direction"] else 1; break
        elif dist_pct < 0.4 and near_ob is None:
            near_ob=o; ob_score=2 if o["type"]==trend["direction"] else 1
    vol_score   = 3 if vol["bias"]==trend["direction"] and vol["vol_surge"] else 2 if vol["bias"]==trend["direction"] else 1 if vol["bias"]=="neutral" else 0
    trend_score = trend["strength"]
    raw_score   = fvg_score + ob_score + vol_score + trend_score
    total       = min(10, round(raw_score * 10 / 11))
    bull_align  = near_fvg and near_fvg["type"]=="bull" and near_ob and near_ob["type"]=="bull" and vol["bias"]=="bull" and trend["direction"]=="bull"
    bear_align  = near_fvg and near_fvg["type"]=="bear" and near_ob and near_ob["type"]=="bear" and vol["bias"]=="bear" and trend["direction"]=="bear"
    sig = "WAIT"
    if total >= MIN_SCORE:
        if bull_align:   sig = "LONG_STRONG"
        elif bear_align: sig = "SHORT_STRONG"
        elif trend["direction"]=="bull" and (near_fvg and near_fvg["type"]=="bull" or near_ob and near_ob["type"]=="bull") and vol["bias"] in ("bull","neutral"): sig="LONG"
        elif trend["direction"]=="bear" and (near_fvg and near_fvg["type"]=="bear" or near_ob and near_ob["type"]=="bear") and vol["bias"] in ("bear","neutral"): sig="SHORT"
    is_long  = "LONG"   in sig
    is_short = "SHORT"  in sig
    strong   = "STRONG" in sig
    tp_mult  = ATR_TP_MULT * (1.2 if strong else 1.0)
    tp1=tp2=sl_price=rr=None
    if is_long:
        tp1=price+atr*tp_mult; tp2=price+atr*tp_mult*1.6; sl_price=price-atr*ATR_SL_MULT
        rr=round((tp1-price)/(price-sl_price),2) if price!=sl_price else 0
    elif is_short:
        tp1=price-atr*tp_mult; tp2=price-atr*tp_mult*1.6; sl_price=price+atr*ATR_SL_MULT
        rr=round((price-tp1)/(sl_price-price),2) if price!=sl_price else 0
    return {"sig":sig,"total":total,"fvg_score":fvg_score,"ob_score":ob_score,"vol_score":vol_score,
            "trend_score":trend_score,"near_fvg":near_fvg,"near_ob":near_ob,"vol":vol,"trend":trend,
            "atr":round(atr,1),"tp1":tp1,"tp2":tp2,"sl":sl_price,"rr":rr,"price":price}

def format_msg(result, tf):
    sig=result["sig"]; price=result["price"]; tp1=result["tp1"]; tp2=result["tp2"]
    sl=result["sl"]; rr=result["rr"]; total=result["total"]
    near_fvg=result["near_fvg"]; near_ob=result["near_ob"]
    vol=result["vol"]; trend=result["trend"]; atr=result["atr"]
    is_long="LONG" in sig; strong="STRONG" in sig
    now=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if strong and is_long:   header="🚀 <b>LONG 강한 시그널!</b>"
    elif is_long:            header="🟢 <b>LONG 시그널</b>"
    elif strong:             header="💥 <b>SHORT 강한 시그널!</b>"
    else:                    header="🔴 <b>SHORT 시그널</b>"
    tp1_pct=abs(tp1-price)/price*100; tp2_pct=abs(tp2-price)/price*100; sl_pct=abs(sl-price)/price*100
    if near_fvg:
        inz=near_fvg["bot"]*0.998<=price<=near_fvg["top"]*1.002
        fvg_line=f"├ {'✅' if near_fvg['type']==('bull' if is_long else 'bear') else '⚠️'} <b>FVG {near_fvg['type'].upper()}</b>: ${near_fvg['bot']:,.1f}~${near_fvg['top']:,.1f} (갭 {near_fvg['gap_pct']:.2f}%) {'내부 ✅' if inz else '근접'}"
    else:
        fvg_line="├ ⚪ FVG: 활성 없음"
    if near_ob:
        inz=near_ob["bot"]*0.998<=price<=near_ob["top"]*1.002
        ob_line=f"├ {'✅' if near_ob['type']==('bull' if is_long else 'bear') else '⚠️'} <b>OB {near_ob['type'].upper()}</b>: ${near_ob['bot']:,.1f}~${near_ob['top']:,.1f} (강도 {near_ob['move_pct']:.2f}%) {'내부 ✅' if inz else '근접'}"
    else:
        ob_line="├ ⚪ OB: 활성 없음"
    surge="🔥거래량 급증" if vol["vol_surge"] else ""
    vol_icon="✅" if vol["bias"]==("bull" if is_long else "bear") else "⚠️"
    vol_line=f"└ {vol_icon} <b>거래량</b>: 매수 {vol['buy_pct']}% / 매도 {vol['sell_pct']}% {surge}"
    trend_txt="상승 (HH+HL)" if trend["hhhl"] else "하락 (LH+LL)" if trend["lllh"] else "중립"
    filled="█"*total+"░"*(10-total)
    return f"""{header}
━━━━━━━━━━━━━━━━━━━
📌 BTCUSDT Perp | ⏱ {tf}
💰 현재가: ${price:,.1f}
🎯 TP1: ${tp1:,.1f} (+{tp1_pct:.2f}%)
🎯 TP2: ${tp2:,.1f} (+{tp2_pct:.2f}%)
🛑 SL:  ${sl:,.1f}  (-{sl_pct:.2f}%)
⚖️ R:R: 1 : {rr}  |  ATR: ${atr:,}

📋 시그널 근거
{fvg_line}
{ob_line}
{vol_line}
📈 추세: {trend_txt} | EMA20: ${trend['ema20']:,}

⚡ 강도: {filled} {total}/10
🕐 {now} KST
━━━━━━━━━━━━━━━━━━━
⚠️ 반드시 SL 설정 후 진입하세요""".strip()

last_signal_time = defaultdict(dict)

def should_send(tf, sig):
    direction = "LONG" if "LONG" in sig else "SHORT"
    return (time.time() - last_signal_time[tf].get(direction, 0)) >= SIGNAL_COOLDOWN

def mark_sent(tf, sig):
    direction = "LONG" if "LONG" in sig else "SHORT"
    last_signal_time[tf][direction] = time.time()

def run():
    print("BTC 시그널 봇 시작")
    send_telegram("🤖 <b>BTC 시그널 봇 시작됨</b>\n\n📌 BTCUSDT Perp\n⏱ 15m / 1h / 2h / 4h\n⚡ 최소 점수: 6/10\n\nFVG + OB + 거래량 분석 중...")
    cycle = 0
    while True:
        cycle += 1
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{now_str}] 사이클 #{cycle}")
        try:
            ticker = get_ticker()
            price  = float(ticker["lastPrice"])
            chg    = float(ticker["priceChangePercent"])
            print(f"  현재가: ${price:,.1f} ({chg:+.2f}%)")
            for tf in TIMEFRAMES:
                try:
                    candles = get_klines(tf)
                    result  = analyze(candles, price)
                    sig     = result["sig"]
                    print(f"  [{tf}] {sig} 점수:{result['total']}/10")
                    if sig != "WAIT" and result["rr"] and result["rr"] >= MIN_RR and should_send(tf, sig):
                        msg = format_msg(result, tf)
                        if send_telegram(msg):
                            mark_sent(tf, sig)
                            print(f"  ✅ [{tf}] 전송 완료")
                    time.sleep(0.3)
                except Exception as e:
                    print(f"  ⚠️ [{tf}] 오류: {e}")
        except Exception as e:
            print(f"  ❌ 오류: {e}")
        print(f"  {CHECK_INTERVAL}초 후 재실행...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run()
