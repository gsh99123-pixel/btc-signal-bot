#!/usr/bin/env python3
"""
BTC 선물 시그널 봇 v3
FVG + OB + 거래량 + 상위TF 필터 + 펀딩비 필터
+ 레버리지 추천 + 포지션 크기 자동 계산 (시드 1500 USDT 기준)
데이터 출처: Binance Futures API (fapi.binance.com) - 실시간
"""

import time
import datetime
import requests
from collections import defaultdict

# ═══════════════════════════════════════════════
# 설정값
# ═══════════════════════════════════════════════
TELEGRAM_TOKEN   = "8617006078:AAEarZfA75pQBZpKgJegbztO9XuHhUJCeR0"
TELEGRAM_CHAT_ID = "8285816381"

SYMBOL         = "BTCUSDT"
TIMEFRAMES     = ["15m", "1h", "2h", "4h"]
HTF            = "4h"
CHECK_INTERVAL = 60

# ── 시그널 조건
MIN_SCORE        = 7
MIN_RR           = 1.8
ATR_TP_MULT      = 2.5
ATR_SL_MULT      = 1.0
FVG_MIN_GAP_PCT  = 0.08
OB_MIN_MOVE_PCT  = 0.30
VOL_WINDOW       = 10

# ── 펀딩비 필터
FUNDING_LONG_BLOCK  =  0.0005
FUNDING_SHORT_BLOCK = -0.0005

# ── 리스크 관리 (시드 1500 USDT 기준)
TOTAL_SEED   = 1500.0   # 총 시드 USDT
MIN_LOSS_PCT = 0.01     # 최소 손실 허용 (1% = 15 USDT)
MAX_LOSS_PCT = 0.02     # 최대 손실 허용 (2% = 30 USDT)
MIN_LEV      = 5        # 최소 레버리지
MAX_LEV      = 15       # 최대 레버리지

# ── 중복 알림 방지
SIGNAL_COOLDOWN = 3600

BASE = "https://fapi.binance.com"


# ═══════════════════════════════════════════════
# 텔레그램
# ═══════════════════════════════════════════════
def send_telegram(msg):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=data, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[텔레그램 오류] {e}")
        return False


# ═══════════════════════════════════════════════
# Binance Futures API
# ═══════════════════════════════════════════════
def get_klines(interval, limit=100):
    r = requests.get(f"{BASE}/fapi/v1/klines",
                     params={"symbol": SYMBOL, "interval": interval, "limit": limit},
                     timeout=10)
    r.raise_for_status()
    return [{
        "open":     float(k[1]), "high":     float(k[2]),
        "low":      float(k[3]), "close":    float(k[4]),
        "volume":   float(k[5]), "buy_vol":  float(k[9]),
        "sell_vol": float(k[5]) - float(k[9]),
        "bull":     float(k[4]) >= float(k[1])
    } for k in r.json()]

def get_ticker():
    r = requests.get(f"{BASE}/fapi/v1/ticker/24hr",
                     params={"symbol": SYMBOL}, timeout=10)
    r.raise_for_status()
    return r.json()

def get_funding_rate():
    r = requests.get(f"{BASE}/fapi/v1/premiumIndex",
                     params={"symbol": SYMBOL}, timeout=10)
    r.raise_for_status()
    d = r.json()
    return {
        "funding_rate": float(d["lastFundingRate"]),
        "mark_price":   float(d["markPrice"]),
        "index_price":  float(d["indexPrice"])
    }

def get_oi_history():
    try:
        r = requests.get(f"{BASE}/futures/data/openInterestHist",
                         params={"symbol": SYMBOL, "period": "5m", "limit": 10},
                         timeout=10)
        r.raise_for_status()
        return [float(x["sumOpenInterest"]) for x in r.json()]
    except:
        return []


# ═══════════════════════════════════════════════
# 레버리지 & 포지션 크기 계산
# ═══════════════════════════════════════════════
def calc_leverage_position(price, sl_price, signal_score):
    """
    시드 1500 USDT 기준, 1회 손실 1~2% 이내 설계
    - 점수 9~10 → 손실 2% (30 USDT), 레버리지 10x
    - 점수 8    → 손실 1.5% (22.5 USDT), 레버리지 7x
    - 점수 7    → 손실 1% (15 USDT), 레버리지 5x
    """
    if not sl_price or price == 0:
        return None

    sl_dist_pct = abs(price - sl_price) / price   # 소수 (예: 0.005 = 0.5%)

    if sl_dist_pct == 0:
        return None

    # 점수별 손실 허용 금액 & 추천 레버리지
    if signal_score >= 9:
        loss_usdt   = TOTAL_SEED * MAX_LOSS_PCT    # 30 USDT
        rec_lev     = 10
        risk_grade  = "공격적"
    elif signal_score >= 8:
        loss_usdt   = TOTAL_SEED * 0.015           # 22.5 USDT
        rec_lev     = 7
        risk_grade  = "중간"
    else:
        loss_usdt   = TOTAL_SEED * MIN_LOSS_PCT    # 15 USDT
        rec_lev     = 5
        risk_grade  = "보수적"

    # 포지션 크기 역산: 손실금액 = 포지션 × SL거리
    # → 포지션 = 손실금액 / SL거리
    position_size = round(loss_usdt / sl_dist_pct, 2)

    # 필요 증거금 = 포지션 / 레버리지
    margin = round(position_size / rec_lev, 2)

    # 증거금이 시드의 40% 초과 시 레버리지 자동 상향
    max_margin = TOTAL_SEED * 0.40
    if margin > max_margin:
        adj_lev = max(MIN_LEV, min(MAX_LEV, int(position_size / max_margin)))
        margin  = round(position_size / adj_lev, 2)
        rec_lev = adj_lev

    # BTC 수량
    btc_qty = round(position_size / price, 6)

    # 실제 손실 재계산
    actual_loss     = round(position_size * sl_dist_pct, 2)
    actual_loss_pct = round(actual_loss / TOTAL_SEED * 100, 2)

    # 레버리지별 예상 청산가
    liq_long  = round(price * (1 - 1/rec_lev + 0.0005), 1)
    liq_short = round(price * (1 + 1/rec_lev - 0.0005), 1)

    return {
        "rec_lev":       rec_lev,
        "position_size": position_size,
        "margin":        margin,
        "btc_qty":       btc_qty,
        "loss_usdt":     actual_loss,
        "loss_pct":      actual_loss_pct,
        "sl_dist_pct":   round(sl_dist_pct * 100, 3),
        "risk_grade":    risk_grade,
        "liq_long":      liq_long,
        "liq_short":     liq_short,
    }


# ═══════════════════════════════════════════════
# 분석 엔진
# ═══════════════════════════════════════════════
def calc_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i-1]
        trs.append(max(c["high"]-c["low"],
                       abs(c["high"]-p["close"]),
                       abs(c["low"]-p["close"])))
    sl = trs[-period:]
    return sum(sl)/len(sl) if sl else 0

def detect_fvg(candles):
    fvgs = []
    for i in range(1, len(candles)-1):
        a, b, c = candles[i-1], candles[i], candles[i+1]
        if c["low"] > a["high"]:
            gap = (c["low"]-a["high"])/a["high"]*100
            if gap >= FVG_MIN_GAP_PCT:
                fvgs.append({"type":"bull","top":c["low"],"bot":a["high"],"gap_pct":gap,"idx":i})
        if c["high"] < a["low"]:
            gap = (a["low"]-c["high"])/a["low"]*100
            if gap >= FVG_MIN_GAP_PCT:
                fvgs.append({"type":"bear","top":a["low"],"bot":c["high"],"gap_pct":gap,"idx":i})
    return fvgs

def detect_ob(candles):
    obs = []
    for i in range(2, len(candles)-2):
        a, n1, n2 = candles[i], candles[i+1], candles[i+2]
        body = abs(a["close"]-a["open"])/a["open"]*100
        if not a["bull"] and n1["bull"] and n2["bull"]:
            mv = (n2["close"]-a["close"])/a["close"]*100
            if mv >= OB_MIN_MOVE_PCT and body > 0.05:
                obs.append({"type":"bull","top":max(a["open"],a["close"]),"bot":min(a["open"],a["close"]),
                            "high":a["high"],"low":a["low"],"move_pct":mv,"idx":i})
        if a["bull"] and not n1["bull"] and not n2["bull"]:
            mv = (a["close"]-n2["close"])/a["close"]*100
            if mv >= OB_MIN_MOVE_PCT and body > 0.05:
                obs.append({"type":"bear","top":max(a["open"],a["close"]),"bot":min(a["open"],a["close"]),
                            "high":a["high"],"low":a["low"],"move_pct":mv,"idx":i})
    return obs

def analyze_volume(candles):
    recent     = candles[-VOL_WINDOW:]
    total_buy  = sum(c["buy_vol"]  for c in recent)
    total_sell = sum(c["sell_vol"] for c in recent)
    total_vol  = total_buy + total_sell
    buy_pct    = total_buy/total_vol*100 if total_vol > 0 else 50
    sell_pct   = 100 - buy_pct
    avg_vol    = sum(c["volume"] for c in candles[-20:-3])/17 if len(candles) >= 20 else 0
    last_vol   = sum(c["volume"] for c in candles[-3:])/3
    vol_surge  = last_vol > avg_vol*1.5 if avg_vol > 0 else False
    if buy_pct >= 55:    bias = "bull"
    elif sell_pct >= 55: bias = "bear"
    else:                bias = "neutral"
    return {"bias":bias, "buy_pct":round(buy_pct,1), "sell_pct":round(sell_pct,1), "vol_surge":vol_surge}

def detect_trend(candles):
    sl   = candles[-20:]
    h1   = max(c["high"]  for c in sl[-10:])
    h2   = max(c["high"]  for c in sl[:10])
    l1   = min(c["low"]   for c in sl[-10:])
    l2   = min(c["low"]   for c in sl[:10])
    ema  = sum(c["close"] for c in sl)/len(sl)
    last = candles[-1]["close"]
    hhhl = h1>h2 and l1>l2
    lllh = h1<h2 and l1<l2
    if hhhl and last>ema:       direction, strength = "bull", 2
    elif not lllh and last>ema: direction, strength = "bull", 1
    elif lllh and last<ema:     direction, strength = "bear", 2
    else:                       direction, strength = "bear", 1
    return {"direction":direction,"strength":strength,"ema20":round(ema,1),"hhhl":hhhl,"lllh":lllh}

def check_htf_trend(htf_candles):
    trend = detect_trend(htf_candles)
    sl50  = htf_candles[-50:] if len(htf_candles)>=50 else htf_candles
    ema50 = sum(c["close"] for c in sl50)/len(sl50)
    sl200 = htf_candles[-100:] if len(htf_candles)>=100 else htf_candles
    ema200= sum(c["close"] for c in sl200)/len(sl200)
    last  = htf_candles[-1]["close"]
    return {
        "direction":   trend["direction"],
        "strength":    trend["strength"],
        "ema50":       round(ema50,1),
        "ema200":      round(ema200,1),
        "strong_bull": last>ema50>ema200,
        "strong_bear": last<ema50<ema200,
        "hhhl":        trend["hhhl"],
        "lllh":        trend["lllh"]
    }

def check_funding_filter(funding):
    rate = funding["funding_rate"]
    rate_pct = rate*100
    if rate > 0.0003:    status = "롱 과열 ⚠️"
    elif rate < -0.0003: status = "숏 과열 ⚠️"
    else:                status = "정상 ✅"
    return {
        "rate":          rate,
        "rate_pct":      round(rate_pct,4),
        "long_blocked":  rate > FUNDING_LONG_BLOCK,
        "short_blocked": rate < FUNDING_SHORT_BLOCK,
        "status":        status
    }

def check_oi_trend(oi_history):
    if len(oi_history) < 3:
        return {"trend":"unknown","change_pct":0}
    avg_new = sum(oi_history[-3:])/3
    avg_old = sum(oi_history[:3])/3
    change  = (avg_new-avg_old)/avg_old*100 if avg_old>0 else 0
    if change > 1:    trend = "increasing"
    elif change < -1: trend = "decreasing"
    else:             trend = "neutral"
    return {"trend":trend,"change_pct":round(change,2)}


# ═══════════════════════════════════════════════
# 종합 분석
# ═══════════════════════════════════════════════
def analyze(candles, price, htf_trend, funding, oi_info):
    atr   = calc_atr(candles)
    fvgs  = detect_fvg(candles)
    obs   = detect_ob(candles)
    vol   = analyze_volume(candles)
    trend = detect_trend(candles)

    # 가장 가까운 FVG
    near_fvg, fvg_score = None, 0
    for f in reversed(fvgs[-8:]):
        in_zone  = f["bot"]*0.998 <= price <= f["top"]*1.002
        dist_pct = min(abs(price-f["bot"]),abs(price-f["top"]))/price*100
        if in_zone:
            near_fvg=f; fvg_score=3 if f["type"]==trend["direction"] else 1; break
        elif dist_pct<0.3 and near_fvg is None:
            near_fvg=f; fvg_score=2 if f["type"]==trend["direction"] else 1

    # 가장 가까운 OB
    near_ob, ob_score = None, 0
    for o in reversed(obs[-6:]):
        in_zone  = o["bot"]*0.998 <= price <= o["top"]*1.002
        dist_pct = min(abs(price-o["bot"]),abs(price-o["top"]))/price*100
        if in_zone:
            near_ob=o; ob_score=3 if o["type"]==trend["direction"] else 1; break
        elif dist_pct<0.4 and near_ob is None:
            near_ob=o; ob_score=2 if o["type"]==trend["direction"] else 1

    # 점수
    if vol["bias"]==trend["direction"] and vol["vol_surge"]: vol_score=3
    elif vol["bias"]==trend["direction"]:                    vol_score=2
    elif vol["bias"]=="neutral":                             vol_score=1
    else:                                                    vol_score=0
    trend_score = trend["strength"]
    htf_score = 0
    if htf_trend["direction"]==trend["direction"]:
        htf_score = 2 if (htf_trend["strong_bull"] or htf_trend["strong_bear"]) else 1
    raw   = fvg_score+ob_score+vol_score+trend_score+htf_score
    total = min(10, round(raw*10/13))

    # 시그널 결정
    bull_align = (near_fvg and near_fvg["type"]=="bull" and
                  near_ob  and near_ob["type"]=="bull"  and
                  vol["bias"]=="bull" and trend["direction"]=="bull")
    bear_align = (near_fvg and near_fvg["type"]=="bear" and
                  near_ob  and near_ob["type"]=="bear"  and
                  vol["bias"]=="bear" and trend["direction"]=="bear")
    sig = "WAIT"
    if total >= MIN_SCORE:
        if bull_align:   sig="LONG_STRONG"
        elif bear_align: sig="SHORT_STRONG"
        elif (trend["direction"]=="bull"
              and (near_fvg and near_fvg["type"]=="bull" or near_ob and near_ob["type"]=="bull")
              and vol["bias"] in ("bull","neutral")): sig="LONG"
        elif (trend["direction"]=="bear"
              and (near_fvg and near_fvg["type"]=="bear" or near_ob and near_ob["type"]=="bear")
              and vol["bias"] in ("bear","neutral")): sig="SHORT"

    # 상위 TF 필터
    htf_blocked=False; htf_block_reason=""
    if sig!="WAIT":
        if "LONG"  in sig and htf_trend["direction"]=="bear":
            htf_blocked=True; htf_block_reason=f"4시간봉 하락 추세"
        if "SHORT" in sig and htf_trend["direction"]=="bull":
            htf_blocked=True; htf_block_reason=f"4시간봉 상승 추세"

    # 펀딩비 필터
    fund_blocked=False; fund_block_reason=""
    if sig!="WAIT" and not htf_blocked:
        if "LONG"  in sig and funding["long_blocked"]:
            fund_blocked=True; fund_block_reason=f"펀딩비 과열 {funding['rate_pct']:+.4f}%"
        if "SHORT" in sig and funding["short_blocked"]:
            fund_blocked=True; fund_block_reason=f"펀딩비 과열 {funding['rate_pct']:+.4f}%"

    if htf_blocked or fund_blocked:
        final_sig    = "FILTERED"
        block_reason = htf_block_reason or fund_block_reason
    else:
        final_sig    = sig
        block_reason = ""

    # TP / SL 계산
    is_long  = "LONG"  in final_sig
    is_short = "SHORT" in final_sig
    strong   = "STRONG" in final_sig

    tp1=tp2=tp3=sl_price=sl_ob=sl_fvg=sl_atr=sl_basis=None
    rr1=rr2=rr3=None
    lev_info=None
    tp_profits={}

    if is_long or is_short:
        tp_mult = ATR_TP_MULT*(1.2 if strong else 1.0)
        if is_long:
            tp1=price+atr*tp_mult; tp2=price+atr*tp_mult*1.8; tp3=price+atr*tp_mult*2.8
        else:
            tp1=price-atr*tp_mult; tp2=price-atr*tp_mult*1.8; tp3=price-atr*tp_mult*2.8

        # SL 후보
        if near_ob:
            sl_ob  = near_ob["low"]*0.999  if is_long else near_ob["high"]*1.001
        if near_fvg:
            sl_fvg = near_fvg["bot"]*0.999 if is_long else near_fvg["top"]*1.001
        if is_long: sl_atr = price-atr*ATR_SL_MULT
        else:       sl_atr = price+atr*ATR_SL_MULT

        def calc_rr(entry, tp, sl, long):
            risk   = (entry-sl)  if long else (sl-entry)
            reward = (tp-entry)  if long else (entry-tp)
            return round(reward/risk,2) if risk>0 else 0

        # 최적 SL 선택 (OB → FVG → ATR, R:R MIN_RR 충족 기준)
        sl_price = sl_atr; sl_basis = "ATR 기반"
        for cand, basis in [(sl_ob,"OB 구조 기반"),(sl_fvg,"FVG 구조 기반"),(sl_atr,"ATR 기반")]:
            if cand is None: continue
            valid = (is_long and cand<price) or (is_short and cand>price)
            if valid and calc_rr(price,tp1,cand,is_long) >= MIN_RR:
                sl_price=cand; sl_basis=basis; break

        # R:R 역산 백업
        if calc_rr(price,tp1,sl_price,is_long) < MIN_RR:
            max_risk = abs(tp1-price)/MIN_RR
            sl_price = (price-max_risk) if is_long else (price+max_risk)
            sl_basis = f"R:R {MIN_RR} 역산"

        rr1 = calc_rr(price,tp1,sl_price,is_long)
        rr2 = calc_rr(price,tp2,sl_price,is_long)
        rr3 = calc_rr(price,tp3,sl_price,is_long)
        sl_dist_pct = abs(price-sl_price)/price*100

        # 레버리지 & 포지션 크기 계산
        lev_info = calc_leverage_position(price, sl_price, total)

        # TP별 실제 수익 계산
        if lev_info:
            pos = lev_info["position_size"]
            mrg = lev_info["margin"]
            for lbl, tpv in [("tp1",tp1),("tp2",tp2),("tp3",tp3)]:
                gp   = abs(tpv-price)/price*100
                prof = round(pos*(gp/100),2)
                mp   = round(prof/mrg*100,1) if mrg>0 else 0
                tp_profits[lbl] = {"usdt":prof,"margin_pct":mp}

    return {
        "sig":          final_sig, "raw_sig":      sig,
        "total":        total,     "fvg_score":    fvg_score,
        "ob_score":     ob_score,  "vol_score":    vol_score,
        "trend_score":  trend_score,"htf_score":   htf_score,
        "near_fvg":     near_fvg,  "near_ob":      near_ob,
        "vol":          vol,        "trend":        trend,
        "htf_trend":    htf_trend,  "funding":      funding,
        "oi_info":      oi_info,    "htf_blocked":  htf_blocked,
        "fund_blocked": fund_blocked,"block_reason":block_reason,
        "atr":          round(atr,1),
        "tp1":tp1,"tp2":tp2,"tp3":tp3,
        "sl":sl_price,"sl_ob":sl_ob,"sl_fvg":sl_fvg,"sl_atr":sl_atr,
        "sl_basis":sl_basis,
        "sl_dist_pct":  round(abs(price-sl_price)/price*100,3) if sl_price else None,
        "rr1":rr1,"rr2":rr2,"rr3":rr3,"rr":rr1,
        "lev_info":     lev_info,
        "tp_profits":   tp_profits,
        "price":        price
    }


# ═══════════════════════════════════════════════
# 메시지 포맷
# ═══════════════════════════════════════════════
def rr_grade(rr):
    if rr is None: return ""
    if rr >= 4.0: return "🏆"
    if rr >= 3.0: return "💎"
    if rr >= 2.0: return "✅"
    if rr >= 1.5: return "🆗"
    return "⚠️"

def format_msg(result, tf):
    sig      = result["sig"];    price    = result["price"]
    tp1      = result["tp1"];    tp2      = result["tp2"];    tp3   = result["tp3"]
    sl       = result["sl"];     sl_basis = result["sl_basis"]
    sl_dist  = result["sl_dist_pct"]
    rr1      = result["rr1"];    rr2      = result["rr2"];    rr3   = result["rr3"]
    sl_ob    = result["sl_ob"];  sl_fvg   = result["sl_fvg"]; sl_atr= result["sl_atr"]
    total    = result["total"];  near_fvg = result["near_fvg"]; near_ob=result["near_ob"]
    vol      = result["vol"];    trend    = result["trend"];   htf   = result["htf_trend"]
    funding  = result["funding"];oi       = result["oi_info"]; atr   = result["atr"]
    lev      = result["lev_info"]
    profits  = result["tp_profits"]

    is_long  = "LONG"  in sig
    strong   = "STRONG" in sig
    now      = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 헤더
    if strong and is_long:   header = "🚀 <b>LONG 강한 시그널!</b>"
    elif is_long:            header = "🟢 <b>LONG 시그널</b>"
    elif strong:             header = "💥 <b>SHORT 강한 시그널!</b>"
    else:                    header = "🔴 <b>SHORT 시그널</b>"

    # 수익률
    tp1_pct = abs(tp1-price)/price*100
    tp2_pct = abs(tp2-price)/price*100
    tp3_pct = abs(tp3-price)/price*100
    sl_pct  = abs(sl-price)/price*100

    # 수익 금액 문자열
    def profit_str(lbl):
        if not profits.get(lbl): return ""
        p = profits[lbl]
        return f"  → +{p['usdt']:,.1f} USDT (+{p['margin_pct']}%)"

    # SL 후보 비교 라인
    sl_lines = [f"🛑 <b>SL: ${sl:,.1f}</b>  (-{sl_pct:.3f}% / ${abs(price-sl):,.1f})  [{sl_basis}]"]
    shown = set()
    shown.add(round(sl,1))
    for cand_val, cand_name in [(sl_ob,"OB 기반"),(sl_fvg,"FVG 기반"),(sl_atr,"ATR 기반")]:
        if cand_val and round(cand_val,1) not in shown:
            pct = abs(price-cand_val)/price*100
            sl_lines.append(f"   ├ {cand_name}: ${cand_val:,.1f}  (-{pct:.3f}%)")
            shown.add(round(cand_val,1))
    sl_block = "\n".join(sl_lines)

    # 레버리지 블록
    if lev:
        lev_icon = "🔥" if lev["rec_lev"] >= 10 else "⚡" if lev["rec_lev"] >= 7 else "🛡️"
        liq_price = lev["liq_long"] if is_long else lev["liq_short"]
        lev_block = (
            f"┌ {lev_icon} <b>추천 레버리지: {lev['rec_lev']}x</b>  ({lev['risk_grade']})\n"
            f"├ 포지션 크기: ${lev['position_size']:,.2f} USDT  ({lev['btc_qty']} BTC)\n"
            f"├ 필요 증거금: ${lev['margin']:,.2f} USDT\n"
            f"├ 예상 청산가: ${liq_price:,.1f}  (SL보다 {'아래' if is_long else '위'})\n"
            f"├ 최대 손실: ${lev['loss_usdt']:,.2f} USDT  "
            f"(시드의 {lev['loss_pct']}% / {TOTAL_SEED:,.0f} USDT 기준)\n"
            f"└ SL 거리: {lev['sl_dist_pct']:.3f}%"
        )
    else:
        lev_block = "레버리지 계산 불가"

    # FVG
    if near_fvg:
        inz = near_fvg["bot"]*0.998<=price<=near_fvg["top"]*1.002
        fvg_line = (f"├ {'✅' if near_fvg['type']==('bull' if is_long else 'bear') else '⚠️'} "
                    f"<b>FVG {near_fvg['type'].upper()}</b>: "
                    f"${near_fvg['bot']:,.1f}~${near_fvg['top']:,.1f} "
                    f"(갭 {near_fvg['gap_pct']:.2f}%) {'내부 ✅' if inz else '근접'}")
    else:
        fvg_line = "├ ⚪ FVG: 활성 없음"

    # OB
    if near_ob:
        inz = near_ob["bot"]*0.998<=price<=near_ob["top"]*1.002
        ob_line = (f"├ {'✅' if near_ob['type']==('bull' if is_long else 'bear') else '⚠️'} "
                   f"<b>OB {near_ob['type'].upper()}</b>: "
                   f"${near_ob['bot']:,.1f}~${near_ob['top']:,.1f} "
                   f"(강도 {near_ob['move_pct']:.2f}%) {'내부 ✅' if inz else '근접'}")
    else:
        ob_line = "├ ⚪ OB: 활성 없음"

    # 거래량
    surge    = " 🔥급증" if vol["vol_surge"] else ""
    vol_icon = "✅" if vol["bias"]==("bull" if is_long else "bear") else "⚠️"
    vol_line = f"└ {vol_icon} <b>거래량</b>: 매수 {vol['buy_pct']}% / 매도 {vol['sell_pct']}%{surge}"

    # 추세
    trend_txt = "상승 HH+HL" if trend["hhhl"] else "하락 LH+LL" if trend["lllh"] else "중립"
    htf_txt   = "상승 ✅" if htf["direction"]=="bull" else "하락 ✅"
    htf_str   = "강한 " if (htf["strong_bull"] or htf["strong_bear"]) else ""
    fund_icon = "✅" if not (funding["long_blocked"] or funding["short_blocked"]) else "⚠️"
    oi_txt    = {"increasing":"증가 📈","decreasing":"감소 📉","neutral":"중립 ➡️"}.get(oi["trend"],"—")
    filled    = "█"*total + "░"*(10-total)

    return f"""{header}
━━━━━━━━━━━━━━━━━━━
📌 BTCUSDT Perp | ⏱ {tf}
💰 <b>현재가: ${price:,.1f}</b>

📈 <b>진입 & 목표가</b>
🟡 진입가: ${price:,.1f}
🎯 TP1: ${tp1:,.1f}  (+{tp1_pct:.2f}%)  R:R {rr_grade(rr1)} 1:{rr1}{profit_str('tp1')}
🎯 TP2: ${tp2:,.1f}  (+{tp2_pct:.2f}%)  R:R {rr_grade(rr2)} 1:{rr2}{profit_str('tp2')}
🎯 TP3: ${tp3:,.1f}  (+{tp3_pct:.2f}%)  R:R {rr_grade(rr3)} 1:{rr3}{profit_str('tp3')}

📉 <b>손절 라인 (SL)</b>
{sl_block}
📏 ATR: ${atr:,}

💼 <b>추천 포지션 설정 (시드 {TOTAL_SEED:,.0f} USDT)</b>
{lev_block}

📋 <b>시그널 근거</b>
{fvg_line}
{ob_line}
{vol_line}

📊 <b>필터 현황</b>
├ 현재추세({tf}): {trend_txt} | EMA20: ${trend['ema20']:,}
├ 상위추세(4h): {htf_str}{htf_txt} | EMA50: ${htf['ema50']:,}
├ {fund_icon} 펀딩비: {funding['rate_pct']:+.4f}% ({funding['status']})
└ 미결제약정: {oi_txt} ({oi['change_pct']:+.2f}%)

⚡ 강도: {filled} {total}/10
🕐 {now} KST
━━━━━━━━━━━━━━━━━━━
⚠️ SL 필수 설정 | 추천 레버리지는 참고용""".strip()


# ═══════════════════════════════════════════════
# 중복 방지
# ═══════════════════════════════════════════════
last_signal_time = defaultdict(dict)

def should_send(tf, sig):
    direction = "LONG" if "LONG" in sig else "SHORT"
    return (time.time()-last_signal_time[tf].get(direction,0)) >= SIGNAL_COOLDOWN

def mark_sent(tf, sig):
    direction = "LONG" if "LONG" in sig else "SHORT"
    last_signal_time[tf][direction] = time.time()


# ═══════════════════════════════════════════════
# 메인 루프
# ═══════════════════════════════════════════════
def run():
    print("="*55)
    print("  BTC 선물 시그널 봇 v3")
    print(f"  시드: {TOTAL_SEED:,.0f} USDT | 손실 한도: 1~2%/회")
    print(f"  레버리지: {MIN_LEV}x~{MAX_LEV}x (점수 기반 자동 추천)")
    print(f"  상위TF: {HTF} | 최소점수: {MIN_SCORE}/10 | R:R≥{MIN_RR}")
    print("="*55)

    send_telegram(
        "🤖 <b>BTC 시그널 봇 v3 시작됨</b>\n\n"
        "📌 BTCUSDT Perp\n"
        "⏱ 15m / 1h / 2h / 4h\n\n"
        f"💼 시드: {TOTAL_SEED:,.0f} USDT\n"
        f"⚠️ 1회 최대 손실: 1~2% ({TOTAL_SEED*MIN_LOSS_PCT:.0f}~{TOTAL_SEED*MAX_LOSS_PCT:.0f} USDT)\n"
        f"🔧 레버리지: {MIN_LEV}x~{MAX_LEV}x 자동 추천\n\n"
        f"🔍 상위TF 필터: {HTF} | 펀딩비 필터: ±0.05%\n"
        f"⚡ 최소 점수: {MIN_SCORE}/10 | R:R≥{MIN_RR}\n\n"
        "FVG + OB + 거래량 분석 중..."
    )

    htf_candles = []
    htf_last_upd = 0
    cycle = 0

    while True:
        cycle += 1
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"\n[{now_str}] 사이클 #{cycle}")

        try:
            ticker  = get_ticker()
            price   = float(ticker["lastPrice"])
            chg     = float(ticker["priceChangePercent"])
            print(f"  현재가: ${price:,.1f} ({chg:+.2f}%)")

            funding_raw = get_funding_rate()
            funding     = check_funding_filter(funding_raw)
            print(f"  펀딩비: {funding['rate_pct']:+.4f}% ({funding['status']})")

            oi_hist = get_oi_history()
            oi_info = check_oi_trend(oi_hist)

            if time.time()-htf_last_upd > 300:
                htf_candles  = get_klines(HTF, limit=100)
                htf_last_upd = time.time()
            htf_trend = check_htf_trend(htf_candles) if htf_candles else {
                "direction":"bull","strength":1,"ema50":0,"ema200":0,
                "strong_bull":False,"strong_bear":False,"hhhl":False,"lllh":False}

            for tf in TIMEFRAMES:
                try:
                    candles = get_klines(tf)
                    result  = analyze(candles, price, htf_trend, funding, oi_info)
                    sig     = result["sig"]

                    lev_txt = ""
                    if result["lev_info"]:
                        lev_txt = f" LEV:{result['lev_info']['rec_lev']}x ${result['lev_info']['margin']:.0f}증거금"

                    print(f"  [{tf}] {sig:15s} 점수:{result['total']}/10{lev_txt}")

                    if (sig not in ("WAIT","FILTERED")
                            and result["rr"] and result["rr"] >= MIN_RR
                            and should_send(tf, sig)):
                        msg = format_msg(result, tf)
                        if send_telegram(msg):
                            mark_sent(tf, sig)
                            print(f"  ✅ [{tf}] 전송 완료!")
                    time.sleep(0.3)
                except Exception as e:
                    print(f"  ⚠️ [{tf}] 오류: {e}")

        except Exception as e:
            print(f"  ❌ 오류: {e}")

        print(f"  → {CHECK_INTERVAL}초 후 재실행...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
