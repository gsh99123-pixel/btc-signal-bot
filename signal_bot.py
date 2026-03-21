#!/usr/bin/env python3
"""
BTC 선물 시그널 봇 v6 (B-Final)
FVG + OB + 거래량 + HTF 필터 + 펀딩비 필터
+ 모멘텀 필터 + 켈리식 리스크 + 월간손실한도
데이터: OKX API (www.okx.com) - Railway 서버 접근 확인됨
"""

import time, datetime, requests
from collections import defaultdict

# ═══════════════════════════════════════
# 설정값
# ═══════════════════════════════════════
TELEGRAM_TOKEN   = "8617006078:AAEarZfA75pQBZpKgJegbztO9XuHhUJCeR0"
TELEGRAM_CHAT_ID = "8285816381"

SYMBOL         = "BTC-USDT-SWAP"      # OKX 선물 심볼
TIMEFRAMES     = ["60m","2H","4H"]    # OKX: 60m=1h, 2H=2h, 4H=4h
TF_LABEL       = {"60m":"1h","2H":"2h","4H":"4h"}
HTF            = "4H"
CHECK_INTERVAL = 60

# ── 시그널 조건
MIN_SCORE_DUAL   = 7
MIN_SCORE_SINGLE = 8
MIN_RR           = 1.8
ATR_TP_MULT      = 2.5
ATR_SL_MULT      = 1.5
FVG_MIN_GAP_PCT  = 0.10
OB_MIN_MOVE_PCT  = 0.40
VOL_WINDOW       = 10

# ── 필터
FUNDING_LONG_BLOCK  =  0.0005
FUNDING_SHORT_BLOCK = -0.0005

# ── 리스크 관리
TOTAL_SEED           = 1500.0
RISK_BASE            = 0.015
RISK_HIGH            = 0.020
RISK_LOW             = 0.010
MIN_LEV              = 5
MAX_LEV              = 15

# ── 손실 한도
DAILY_MAX_LOSS_PCT   = 0.05
MONTHLY_MAX_LOSS_PCT = 0.15

# ── 쿨다운
BASE_COOLDOWN     = 2 * 3600
MAX_COOLDOWN      = 8 * 3600
COOLDOWN_MULT     = 1.5
CONSEC_LOSE_LIMIT = 4

BASE = "https://www.okx.com"

# ═══════════════════════════════════════
# 텔레그램
# ═══════════════════════════════════════
def send_telegram(msg):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}
    try:
        r = requests.post(url, data=data, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[텔레그램 오류] {e}")
        return False

# ═══════════════════════════════════════
# OKX API
# ═══════════════════════════════════════
def get_klines(bar, limit=200):
    """OKX 캔들 데이터 — 최신순 반환 → reverse 필요"""
    r = requests.get(f"{BASE}/api/v5/market/candles", params={
        "instId": SYMBOL, "bar": bar, "limit": str(limit)
    }, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "0":
        raise Exception(f"OKX 오류: {d.get('msg')}")
    candles = []
    for k in reversed(d["data"]):  # 최신→과거 → 뒤집어서 과거→최신
        o=float(k[1]); h=float(k[2]); l=float(k[3]); c=float(k[4]); v=float(k[5])
        bull = c >= o
        candles.append({
            "open":o,"high":h,"low":l,"close":c,"volume":v,
            "buy_vol": v if bull else 0.0,
            "sell_vol": 0.0 if bull else v,
            "bull": bull
        })
    return candles

def get_ticker():
    """OKX 현재가"""
    r = requests.get(f"{BASE}/api/v5/market/ticker", params={
        "instId": SYMBOL
    }, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "0":
        raise Exception(f"OKX 오류: {d.get('msg')}")
    item = d["data"][0]
    last  = float(item["last"])
    open24= float(item["open24h"])
    chg   = (last - open24) / open24 * 100 if open24 > 0 else 0
    return {"lastPrice": str(last), "price24hPcnt": str(round(chg/100, 6))}

def get_funding_rate():
    """OKX 펀딩비"""
    try:
        r = requests.get(f"{BASE}/api/v5/public/funding-rate", params={
            "instId": SYMBOL
        }, timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("code") == "0" and d["data"]:
            rate = float(d["data"][0]["fundingRate"])
            return {"funding_rate": rate}
    except Exception as e:
        print(f"  [펀딩비 오류] {e}")
    return {"funding_rate": 0.0}

def get_oi_history():
    """OKX 미결제약정"""
    try:
        r = requests.get(f"{BASE}/api/v5/public/open-interest", params={
            "instType": "SWAP", "instId": SYMBOL
        }, timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("code") == "0" and d["data"]:
            oi = float(d["data"][0]["oi"])
            return [oi] * 6  # 현재값만 있으므로 동일값 반복
    except Exception as e:
        print(f"  [OI 오류] {e}")
    return []

# ═══════════════════════════════════════
# 분석 엔진
# ═══════════════════════════════════════
def calc_atr(cs, p=14):
    trs=[]
    for i in range(1,len(cs)):
        c,pv=cs[i],cs[i-1]
        trs.append(max(c["high"]-c["low"],abs(c["high"]-pv["close"]),abs(c["low"]-pv["close"])))
    sl=trs[-p:]; return sum(sl)/len(sl) if sl else 0

def detect_fvg(cs):
    out=[]
    for i in range(1,len(cs)-1):
        a,b,c=cs[i-1],cs[i],cs[i+1]
        if c["low"]>a["high"]:
            g=(c["low"]-a["high"])/a["high"]*100
            if g>=FVG_MIN_GAP_PCT: out.append({"type":"bull","top":c["low"],"bot":a["high"],"gap_pct":g})
        if c["high"]<a["low"]:
            g=(a["low"]-c["high"])/a["low"]*100
            if g>=FVG_MIN_GAP_PCT: out.append({"type":"bear","top":a["low"],"bot":c["high"],"gap_pct":g})
    return out

def detect_ob(cs):
    out=[]
    for i in range(2,len(cs)-2):
        a,n1,n2=cs[i],cs[i+1],cs[i+2]
        body=abs(a["close"]-a["open"])/a["open"]*100
        if not a["bull"] and n1["bull"] and n2["bull"]:
            mv=(n2["close"]-a["close"])/a["close"]*100
            if mv>=OB_MIN_MOVE_PCT and body>0.05:
                out.append({"type":"bull","top":max(a["open"],a["close"]),
                            "bot":min(a["open"],a["close"]),
                            "high":a["high"],"low":a["low"],"move_pct":mv})
        if a["bull"] and not n1["bull"] and not n2["bull"]:
            mv=(a["close"]-n2["close"])/a["close"]*100
            if mv>=OB_MIN_MOVE_PCT and body>0.05:
                out.append({"type":"bear","top":max(a["open"],a["close"]),
                            "bot":min(a["open"],a["close"]),
                            "high":a["high"],"low":a["low"],"move_pct":mv})
    return out

def analyze_volume(cs):
    r=cs[-VOL_WINDOW:]
    tb=sum(c["buy_vol"] for c in r); ts_=sum(c["sell_vol"] for c in r); tv=tb+ts_
    bp=tb/tv*100 if tv>0 else 50; sp=100-bp
    avg=sum(c["volume"] for c in cs[-20:-3])/17 if len(cs)>=20 else 0
    lv=sum(c["volume"] for c in cs[-3:])/3
    surge=lv>avg*1.5 if avg>0 else False
    bias="bull" if bp>=55 else "bear" if sp>=55 else "neutral"
    return {"bias":bias,"buy_pct":round(bp,1),"sell_pct":round(sp,1),"vol_surge":surge}

def detect_trend(cs):
    sl=cs[-20:]
    h1=max(c["high"] for c in sl[-10:]); h2=max(c["high"] for c in sl[:10])
    l1=min(c["low"]  for c in sl[-10:]); l2=min(c["low"]  for c in sl[:10])
    ema=sum(c["close"] for c in sl)/len(sl); last=cs[-1]["close"]
    hhhl=h1>h2 and l1>l2; lllh=h1<h2 and l1<l2
    if hhhl and last>ema:       d,s="bull",2
    elif not lllh and last>ema: d,s="bull",1
    elif lllh and last<ema:     d,s="bear",2
    else:                       d,s="bear",1
    return {"direction":d,"strength":s,"ema20":round(ema,1),"hhhl":hhhl,"lllh":lllh}

def check_htf_trend(htf_cs):
    tr=detect_trend(htf_cs)
    e50=sum(c["close"] for c in htf_cs[-50:])/min(50,len(htf_cs))
    e200=sum(c["close"] for c in htf_cs[-100:])/min(100,len(htf_cs))
    last=htf_cs[-1]["close"]
    return {"direction":tr["direction"],"strength":tr["strength"],
            "ema50":round(e50,1),"ema200":round(e200,1),
            "strong_bull":last>e50>e200,"strong_bear":last<e50<e200,
            "hhhl":tr["hhhl"],"lllh":tr["lllh"]}

def check_funding(fund_raw):
    rate=fund_raw["funding_rate"]; rate_pct=rate*100
    if rate>0.0003:    status="롱 과열 ⚠️"
    elif rate<-0.0003: status="숏 과열 ⚠️"
    else:              status="정상 ✅"
    return {"rate":rate,"rate_pct":round(rate_pct,4),
            "long_blocked":rate>FUNDING_LONG_BLOCK,
            "short_blocked":rate<FUNDING_SHORT_BLOCK,"status":status}

def check_oi_trend(oi_hist):
    if len(oi_hist)<3: return {"trend":"unknown","change_pct":0}
    avg_new=sum(oi_hist[-3:])/3; avg_old=sum(oi_hist[:3])/3
    change=(avg_new-avg_old)/avg_old*100 if avg_old>0 else 0
    if change>1: trend="increasing"
    elif change<-1: trend="decreasing"
    else: trend="neutral"
    return {"trend":trend,"change_pct":round(change,2)}

def calc_rr(entry,tp,sl,long):
    risk=(entry-sl) if long else (sl-entry)
    rew=(tp-entry)  if long else (entry-tp)
    return round(rew/risk,2) if risk>0 else 0

def check_momentum(cs, direction):
    if len(cs)<2: return True
    curr=cs[-1]
    return curr["bull"] if direction=="bull" else not curr["bull"]

def analyze(candles, price, htf_trend, funding, oi_info):
    atr=calc_atr(candles); fvgs=detect_fvg(candles)
    obs=detect_ob(candles); vol=analyze_volume(candles); tr=detect_trend(candles)

    near_fvg,fsc=None,0
    for f in reversed(fvgs[-8:]):
        inz=f["bot"]*0.998<=price<=f["top"]*1.002
        dist=min(abs(price-f["bot"]),abs(price-f["top"]))/price*100
        if inz:   near_fvg=f;fsc=3 if f["type"]==tr["direction"] else 1;break
        elif dist<0.3 and not near_fvg: near_fvg=f;fsc=2 if f["type"]==tr["direction"] else 1

    near_ob,osc=None,0
    for o in reversed(obs[-6:]):
        inz=o["bot"]*0.998<=price<=o["top"]*1.002
        dist=min(abs(price-o["bot"]),abs(price-o["top"]))/price*100
        if inz:   near_ob=o;osc=3 if o["type"]==tr["direction"] else 1;break
        elif dist<0.4 and not near_ob: near_ob=o;osc=2 if o["type"]==tr["direction"] else 1

    vsc=(3 if vol["bias"]==tr["direction"] and vol["vol_surge"] else
         2 if vol["bias"]==tr["direction"] else
         1 if vol["bias"]=="neutral" else 0)
    tsc=tr["strength"]
    hsc=(2 if htf_trend["direction"]==tr["direction"] and
              (htf_trend["strong_bull"] or htf_trend["strong_bear"]) else
         1 if htf_trend["direction"]==tr["direction"] else 0)
    total=min(10,round((fsc+osc+vsc+tsc+hsc)*10/13))

    has_fvg=near_fvg is not None and near_fvg["type"]==tr["direction"]
    has_ob =near_ob  is not None and near_ob["type"]==tr["direction"]
    min_score=MIN_SCORE_DUAL if (has_fvg and has_ob) else MIN_SCORE_SINGLE

    bull_ok=(near_fvg and near_fvg["type"]=="bull" and near_ob and near_ob["type"]=="bull"
             and vol["bias"]=="bull" and tr["direction"]=="bull")
    bear_ok=(near_fvg and near_fvg["type"]=="bear" and near_ob and near_ob["type"]=="bear"
             and vol["bias"]=="bear" and tr["direction"]=="bear")

    sig="WAIT"
    if total>=min_score:
        if bull_ok:   sig="LONG_STRONG"
        elif bear_ok: sig="SHORT_STRONG"
        elif (tr["direction"]=="bull"
              and (near_fvg and near_fvg["type"]=="bull" or near_ob and near_ob["type"]=="bull")
              and vol["bias"] in ("bull","neutral")): sig="LONG"
        elif (tr["direction"]=="bear"
              and (near_fvg and near_fvg["type"]=="bear" or near_ob and near_ob["type"]=="bear")
              and vol["bias"] in ("bear","neutral")): sig="SHORT"

    if sig not in ("WAIT",):
        mdir="bull" if "LONG" in sig else "bear"
        if not check_momentum(candles,mdir): sig="FILTERED_MOMENTUM"

    if sig not in ("WAIT","FILTERED_MOMENTUM"):
        if "LONG"  in sig and htf_trend["direction"]=="bear": sig="FILTERED_HTF"
        if "SHORT" in sig and htf_trend["direction"]=="bull":  sig="FILTERED_HTF"

    if sig not in ("WAIT","FILTERED_MOMENTUM","FILTERED_HTF"):
        if "LONG"  in sig and funding["long_blocked"]:  sig="FILTERED_FUND"
        if "SHORT" in sig and funding["short_blocked"]: sig="FILTERED_FUND"

    if "FILTERED" in sig or sig=="WAIT":
        return {"sig":sig,"total":total,"price":price,
                "tp1":None,"tp2":None,"tp3":None,"sl":None,
                "rr1":None,"rr2":None,"rr3":None,"sl_basis":None,
                "lev_info":None,"tp_profits":{}}

    is_long="LONG" in sig; strong="STRONG" in sig
    tp_m=ATR_TP_MULT*(1.2 if strong else 1.0)
    tp1=price+atr*tp_m if is_long else price-atr*tp_m
    tp2=price+atr*tp_m*1.8 if is_long else price-atr*tp_m*1.8
    tp3=price+atr*tp_m*2.8 if is_long else price-atr*tp_m*2.8

    sl_atr=price-atr*ATR_SL_MULT if is_long else price+atr*ATR_SL_MULT
    sl_ob=(near_ob["low"]*0.999 if is_long else near_ob["high"]*1.001) if near_ob else None
    sl_fvg=(near_fvg["bot"]*0.999 if is_long else near_fvg["top"]*1.001) if near_fvg else None

    sl_price=sl_atr; sl_basis="ATR"
    for cand,basis in [(sl_ob,"OB"),(sl_fvg,"FVG"),(sl_atr,"ATR")]:
        if cand is None: continue
        valid=(is_long and cand<price) or (not is_long and cand>price)
        if valid and abs(price-cand)>=atr*0.5 and calc_rr(price,tp1,cand,is_long)>=MIN_RR:
            sl_price=cand; sl_basis=basis; break

    if calc_rr(price,tp1,sl_price,is_long)<MIN_RR:
        mr=max(abs(tp1-price)/MIN_RR,atr*0.5)
        sl_price=(price-mr) if is_long else (price+mr)
        sl_basis="RR역산"

    sl_dist=abs(price-sl_price)/price*100
    rr1=calc_rr(price,tp1,sl_price,is_long)
    rr2=calc_rr(price,tp2,sl_price,is_long)
    rr3=calc_rr(price,tp3,sl_price,is_long)

    lev_info=calc_leverage(price,sl_price,total)
    tp_profits={}
    if lev_info:
        pos=lev_info["position_size"]; mrg=lev_info["margin"]
        for lbl,tpv in [("tp1",tp1),("tp2",tp2),("tp3",tp3)]:
            gp=abs(tpv-price)/price*100
            prof=round(pos*(gp/100),2)
            mp=round(prof/mrg*100,1) if mrg>0 else 0
            tp_profits[lbl]={"usdt":prof,"margin_pct":mp}

    return {"sig":sig,"total":total,"price":price,"is_long":is_long,
            "near_fvg":near_fvg,"near_ob":near_ob,"vol":vol,"trend":tr,
            "htf_trend":htf_trend,"funding":funding,"oi_info":oi_info,
            "atr":round(atr,1),"tp1":tp1,"tp2":tp2,"tp3":tp3,
            "sl":sl_price,"sl_basis":sl_basis,"sl_dist":round(sl_dist,3),
            "sl_ob":sl_ob,"sl_fvg":sl_fvg,"sl_atr":sl_atr,
            "rr1":rr1,"rr2":rr2,"rr3":rr3,"rr":rr1,
            "lev_info":lev_info,"tp_profits":tp_profits}

def calc_leverage(price, sl_price, score):
    if not sl_price or price==0: return None
    sl_dist=abs(price-sl_price)/price
    if sl_dist==0: return None
    if score>=9:   loss_usdt=TOTAL_SEED*0.02; rec_lev=10; grade="공격적"
    elif score>=8: loss_usdt=TOTAL_SEED*0.015; rec_lev=7; grade="중간"
    else:          loss_usdt=TOTAL_SEED*0.01;  rec_lev=5; grade="보수적"
    pos=round(loss_usdt/sl_dist,2)
    margin=round(pos/rec_lev,2)
    if margin>TOTAL_SEED*0.4:
        rec_lev=max(MIN_LEV,min(MAX_LEV,int(pos/(TOTAL_SEED*0.4))))
        margin=round(pos/rec_lev,2)
    return {"rec_lev":rec_lev,"position_size":pos,"margin":margin,
            "btc_qty":round(pos/price,6),"loss_usdt":round(pos*sl_dist,2),
            "loss_pct":round(pos*sl_dist/TOTAL_SEED*100,2),
            "sl_dist_pct":round(sl_dist*100,3),"risk_grade":grade,
            "liq_long":round(price*(1-1/rec_lev+0.0005),1),
            "liq_short":round(price*(1+1/rec_lev-0.0005),1)}

# ═══════════════════════════════════════
# 메시지 포맷
# ═══════════════════════════════════════
def rr_grade(rr):
    if rr is None: return ""
    if rr>=4.0: return "🏆"
    if rr>=3.0: return "💎"
    if rr>=2.0: return "✅"
    if rr>=1.5: return "🆗"
    return "⚠️"

def format_msg(result, tf_label, risk_pct, consec_win, consec_lose):
    sig=result["sig"]; price=result["price"]
    tp1=result["tp1"]; tp2=result["tp2"]; tp3=result["tp3"]
    sl=result["sl"]; sl_basis=result["sl_basis"]; sl_dist=result["sl_dist"]
    rr1=result["rr1"]; rr2=result["rr2"]; rr3=result["rr3"]
    sl_ob=result["sl_ob"]; sl_fvg=result["sl_fvg"]; sl_atr=result["sl_atr"]
    lev=result["lev_info"]; profits=result["tp_profits"]
    near_fvg=result["near_fvg"]; near_ob=result["near_ob"]
    vol=result["vol"]; trend=result["trend"]; htf=result["htf_trend"]
    funding=result["funding"]; oi=result["oi_info"]; atr=result["atr"]
    total=result["total"]

    is_long="LONG" in sig; strong="STRONG" in sig
    now=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if strong and is_long:   header="🚀 <b>LONG 강한 시그널!</b>"
    elif is_long:            header="🟢 <b>LONG 시그널</b>"
    elif strong:             header="💥 <b>SHORT 강한 시그널!</b>"
    else:                    header="🔴 <b>SHORT 시그널</b>"

    tp1_pct=abs(tp1-price)/price*100
    tp2_pct=abs(tp2-price)/price*100
    tp3_pct=abs(tp3-price)/price*100
    sl_pct=abs(sl-price)/price*100

    def profit_str(lbl):
        if not profits.get(lbl): return ""
        p=profits[lbl]
        return f"  → +{p['usdt']:,.1f} USDT (+{p['margin_pct']}%)"

    sl_lines=[f"🛑 <b>SL: ${sl:,.1f}</b>  (-{sl_pct:.3f}% / ${abs(price-sl):,.1f})  [{sl_basis}]"]
    shown={round(sl,1)}
    for cv,cn in [(sl_ob,"OB기반"),(sl_fvg,"FVG기반"),(sl_atr,"ATR기반")]:
        if cv and round(cv,1) not in shown:
            pct=abs(price-cv)/price*100
            sl_lines.append(f"   ├ {cn}: ${cv:,.1f}  (-{pct:.3f}%)")
            shown.add(round(cv,1))
    sl_block="\n".join(sl_lines)

    if lev:
        lev_icon="🔥" if lev["rec_lev"]>=10 else "⚡" if lev["rec_lev"]>=7 else "🛡️"
        liq=lev["liq_long"] if is_long else lev["liq_short"]
        lev_block=(
            f"┌ {lev_icon} <b>추천 레버리지: {lev['rec_lev']}x</b>  ({lev['risk_grade']})\n"
            f"├ 포지션: ${lev['position_size']:,.2f} USDT  ({lev['btc_qty']} BTC)\n"
            f"├ 증거금: ${lev['margin']:,.2f} USDT\n"
            f"├ 청산가: ${liq:,.1f}\n"
            f"├ 최대손실: ${lev['loss_usdt']:,.2f} USDT ({lev['loss_pct']}% / {TOTAL_SEED:,.0f} USDT)\n"
            f"└ SL거리: {lev['sl_dist_pct']:.3f}%"
        )
    else:
        lev_block="레버리지 계산 불가"

    risk_icon="🔥" if risk_pct==RISK_HIGH else "🛡️" if risk_pct==RISK_LOW else "⚡"
    risk_state=(f"{risk_icon} 리스크: {risk_pct*100:.1f}%  "
                f"(연속WIN {consec_win}회 / 연속LOSE {consec_lose}회)")

    if near_fvg:
        inz=near_fvg["bot"]*0.998<=price<=near_fvg["top"]*1.002
        fvg_line=(f"├ {'✅' if near_fvg['type']==('bull' if is_long else 'bear') else '⚠️'} "
                  f"<b>FVG {near_fvg['type'].upper()}</b>: "
                  f"${near_fvg['bot']:,.1f}~${near_fvg['top']:,.1f} "
                  f"(갭 {near_fvg['gap_pct']:.2f}%) {'내부✅' if inz else '근접'}")
    else:
        fvg_line="├ ⚪ FVG: 없음"

    if near_ob:
        inz=near_ob["bot"]*0.998<=price<=near_ob["top"]*1.002
        ob_line=(f"├ {'✅' if near_ob['type']==('bull' if is_long else 'bear') else '⚠️'} "
                 f"<b>OB {near_ob['type'].upper()}</b>: "
                 f"${near_ob['bot']:,.1f}~${near_ob['top']:,.1f} "
                 f"(강도 {near_ob['move_pct']:.2f}%) {'내부✅' if inz else '근접'}")
    else:
        ob_line="├ ⚪ OB: 없음"

    surge=" 🔥급증" if vol["vol_surge"] else ""
    vol_icon="✅" if vol["bias"]==("bull" if is_long else "bear") else "⚠️"
    vol_line=f"└ {vol_icon} <b>거래량</b>: 매수{vol['buy_pct']}% 매도{vol['sell_pct']}%{surge}"

    trend_txt="상승 HH+HL" if trend["hhhl"] else "하락 LH+LL" if trend["lllh"] else "중립"
    htf_txt="상승✅" if htf["direction"]=="bull" else "하락✅"
    htf_str="강한 " if (htf["strong_bull"] or htf["strong_bear"]) else ""
    fund_icon="✅" if not(funding["long_blocked"] or funding["short_blocked"]) else "⚠️"
    oi_txt={"increasing":"증가📈","decreasing":"감소📉","neutral":"중립➡️"}.get(oi["trend"],"—")
    filled="█"*total+"░"*(10-total)

    return f"""{header}
━━━━━━━━━━━━━━━━━━━
📌 BTCUSDT Perp | ⏱ {tf_label}
💰 <b>현재가: ${price:,.1f}</b>

📈 <b>진입 & 목표가</b>
🟡 진입가: ${price:,.1f}
🎯 TP1: ${tp1:,.1f}  (+{tp1_pct:.2f}%)  R:R {rr_grade(rr1)} 1:{rr1}{profit_str('tp1')}
🎯 TP2: ${tp2:,.1f}  (+{tp2_pct:.2f}%)  R:R {rr_grade(rr2)} 1:{rr2}{profit_str('tp2')}
🎯 TP3: ${tp3:,.1f}  (+{tp3_pct:.2f}%)  R:R {rr_grade(rr3)} 1:{rr3}{profit_str('tp3')}

📉 <b>손절 라인 (SL)</b>
{sl_block}
📏 ATR: ${atr:,}

💼 <b>포지션 설정 (시드 {TOTAL_SEED:,.0f} USDT)</b>
{lev_block}
{risk_state}

📋 <b>시그널 근거</b>
{fvg_line}
{ob_line}
{vol_line}

📊 <b>필터 현황</b>
├ 현재추세({tf_label}): {trend_txt} | EMA20: ${trend['ema20']:,}
├ 상위추세(4h): {htf_str}{htf_txt} | EMA50: ${htf['ema50']:,}
├ {fund_icon} 펀딩비: {funding['rate_pct']:+.4f}% ({funding['status']})
└ 미결제약정: {oi_txt} ({oi['change_pct']:+.2f}%)

⚡ 강도: {filled} {total}/10
🕐 {now} KST
━━━━━━━━━━━━━━━━━━━
⚠️ SL 필수 | 추천 레버리지 참고용""".strip()

# ═══════════════════════════════════════
# 상태 관리
# ═══════════════════════════════════════
cooldown_state = {
    "LONG":  {"last_ts":0,"cooldown":BASE_COOLDOWN,"consec_lose":0,"consec_win":0},
    "SHORT": {"last_ts":0,"cooldown":BASE_COOLDOWN,"consec_lose":0,"consec_win":0}
}
daily_loss      = defaultdict(float)
daily_blocked   = defaultdict(set)
monthly_blocked = set()

def get_risk_pct(direction):
    cd=cooldown_state[direction]
    if cd["consec_win"]>=2:    return RISK_HIGH
    elif cd["consec_lose"]>=2: return RISK_LOW
    else:                      return RISK_BASE

def can_trade(direction, now_ts):
    now=datetime.datetime.now()
    date_key=now.strftime("%Y-%m-%d")
    month_key=now.strftime("%Y-%m")
    cd=cooldown_state[direction]
    if month_key in monthly_blocked:
        return False, f"월간손실 {MONTHLY_MAX_LOSS_PCT*100:.0f}% 한도 초과"
    if direction in daily_blocked[date_key]:
        return False, f"{CONSEC_LOSE_LIMIT}연속 LOSE 당일 차단"
    if now_ts-cd["last_ts"] < cd["cooldown"]:
        remain=(cd["cooldown"]-(now_ts-cd["last_ts"]))//60
        return False, f"쿨다운 {remain}분 남음"
    if daily_loss[date_key] >= TOTAL_SEED*DAILY_MAX_LOSS_PCT:
        return False, f"일일손실 {DAILY_MAX_LOSS_PCT*100:.0f}% 한도 초과"
    return True, ""

def mark_signal_sent(direction, now_ts):
    cooldown_state[direction]["last_ts"]=now_ts

# ═══════════════════════════════════════
# 메인 루프
# ═══════════════════════════════════════
def run():
    print("="*60)
    print("  BTC 시그널 봇 v6 (B-Final) - OKX API")
    print(f"  시드: {TOTAL_SEED:,.0f} USDT")
    print(f"  리스크: {RISK_LOW*100:.0f}%/{RISK_BASE*100:.0f}%/{RISK_HIGH*100:.0f}% (켈리)")
    print(f"  쿨다운: 2h | 연속LOSE {CONSEC_LOSE_LIMIT}회 차단")
    print(f"  일손실: {DAILY_MAX_LOSS_PCT*100:.0f}% | 월손실: {MONTHLY_MAX_LOSS_PCT*100:.0f}%")
    print(f"  TF: 1h/2h/4h | HTF: 4h | API: OKX")
    print("="*60)

    send_telegram(
        "🤖 <b>BTC 시그널 봇 v6 (B-Final) 시작</b>\n\n"
        "📌 BTCUSDT Perp | 1h / 2h / 4h\n"
        "🔌 API: OKX (Railway 서버 접근 확인됨)\n\n"
        f"💼 시드: {TOTAL_SEED:,.0f} USDT\n"
        f"⚡ 켈리 리스크: {RISK_LOW*100:.0f}%/{RISK_BASE*100:.0f}%/{RISK_HIGH*100:.0f}%\n"
        f"⏱ 쿨다운: 2h | 연속LOSE {CONSEC_LOSE_LIMIT}회 차단\n"
        f"🛡️ 일손실 {DAILY_MAX_LOSS_PCT*100:.0f}% | 월손실 {MONTHLY_MAX_LOSS_PCT*100:.0f}%\n\n"
        f"🔍 OB+FVG={MIN_SCORE_DUAL}점 | 하나만={MIN_SCORE_SINGLE}점\n"
        f"📐 ATR_SL={ATR_SL_MULT} | FVG>={FVG_MIN_GAP_PCT}% | OB>={OB_MIN_MOVE_PCT}%\n\n"
        "FVG + OB + 거래량 + 모멘텀 분석 중..."
    )

    htf_candles=[]; htf_last_upd=0; cycle=0

    while True:
        cycle+=1
        now_str=datetime.datetime.now().strftime("%H:%M:%S")
        now_ts=int(time.time())
        print(f"\n[{now_str}] 사이클 #{cycle}")

        try:
            ticker=get_ticker()
            price=float(ticker["lastPrice"])
            chg=float(ticker["price24hPcnt"])*100
            print(f"  현재가: ${price:,.1f} ({chg:+.2f}%)")

            fund_raw=get_funding_rate()
            funding=check_funding(fund_raw)
            print(f"  펀딩비: {funding['rate_pct']:+.4f}% ({funding['status']})")

            oi_hist=get_oi_history()
            oi_info=check_oi_trend(oi_hist)

            if now_ts-htf_last_upd>300:
                htf_candles=get_klines(HTF, limit=200)
                htf_last_upd=now_ts
            htf_trend=check_htf_trend(htf_candles) if htf_candles else {
                "direction":"bull","strength":1,"ema50":0,"ema200":0,
                "strong_bull":False,"strong_bear":False,"hhhl":False,"lllh":False}

            for tf in TIMEFRAMES:
                tf_label=TF_LABEL[tf]
                try:
                    candles=get_klines(tf, limit=200)
                    result=analyze(candles,price,htf_trend,funding,oi_info)
                    sig=result["sig"]

                    if sig in ("WAIT","FILTERED_MOMENTUM","FILTERED_HTF","FILTERED_FUND"):
                        print(f"  [{tf_label}] {sig}")
                        time.sleep(0.5); continue

                    direction="LONG" if "LONG" in sig else "SHORT"
                    ok,reason=can_trade(direction,now_ts)

                    lev_txt=""
                    if result["lev_info"]:
                        lev_txt=f" LEV:{result['lev_info']['rec_lev']}x"
                    print(f"  [{tf_label}] {sig:15s} 점수:{result['total']}/10{lev_txt}"
                          +(f" → 차단: {reason}" if not ok else ""))

                    if ok and result["rr1"] and result["rr1"]>=MIN_RR:
                        risk_pct=get_risk_pct(direction)
                        cd=cooldown_state[direction]
                        msg=format_msg(result,tf_label,risk_pct,
                                       cd["consec_win"],cd["consec_lose"])
                        if send_telegram(msg):
                            mark_signal_sent(direction,now_ts)
                            print(f"  ✅ [{tf_label}] 전송 완료!")

                    time.sleep(0.5)
                except Exception as e:
                    print(f"  ⚠️ [{tf_label}] 오류: {e}")

        except Exception as e:
            print(f"  ❌ 오류: {e}")

        print(f"  → {CHECK_INTERVAL}초 후 재실행...")
        time.sleep(CHECK_INTERVAL)

if __name__=="__main__":
    run()
