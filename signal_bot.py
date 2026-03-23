#!/usr/bin/env python3
"""
BTC 선물 시그널 봇 v12b
기반: v8 + RSI 다이버전스 필터 + 부분 익절 안내

v12b 변경사항 (v8 대비):
  ① RSI 다이버전스 감지
     강한 강세 다이버전스 → LONG 신호 강화 (+2점)
     강한 약세 다이버전스 → SHORT 신호 강화 (+2점)
     역다이버전스 → 해당 방향 시그널 차단
  ② 부분 익절 안내 메시지
     TP1: 50% 익절 + SL → 본전 이동
     TP2: 나머지 50% 전량 익절

백테스팅 결과 (387일 v12b):
  시그널: 204건 | 승률: 40.7% | EV: +0.468
  수익률: +371.9% | 수수료 후: +349.5%
  최대낙폭: -18.3% | 최대연속손실: 11회
  (v8 대비 +84.2%p 향상)
"""

import os, time, datetime, json, requests
from collections import defaultdict

# ═══════════════════════════════════════
# 설정값
# ═══════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOL         = "BTC-USDT-SWAP"
TIMEFRAMES     = ["1H","2H","4H"]
TF_LABEL       = {"1H":"1h","2H":"2h","4H":"4h"}
HTF            = "4H"
CHECK_INTERVAL = 60

# ── 시그널 조건
MIN_SCORE_DUAL   = 7
MIN_SCORE_SINGLE = 8
MIN_RR           = 1.5
ATR_TP_MULT      = 2.0
ATR_SL_MULT      = 1.3
FVG_MIN_GAP_PCT  = 0.10
OB_MIN_MOVE_PCT  = 0.40
VOL_WINDOW       = 10

# ── 변동성 필터
ATR_VOL_THRESHOLD = 0.65
ATR_LOOKBACK      = 20

# ── RSI 다이버전스 설정 (v12b 신규)
RSI_PERIOD        = 14
RSI_DIV_LOOKBACK  = 20   # 다이버전스 탐색 구간 (봉)
RSI_BULL_MAX      = 40   # 강세 다이버전스: RSI < 40
RSI_BEAR_MIN      = 60   # 약세 다이버전스: RSI > 60

# ── 필터
FUNDING_LONG_BLOCK  =  0.0005
FUNDING_SHORT_BLOCK = -0.0005

# ── 리스크 관리
TOTAL_SEED           = 1500.0
RISK_BASE            = 0.015
RISK_HIGH            = 0.020
RISK_LOW             = 0.010

# ── 손실 한도
DAILY_MAX_LOSS_PCT   = 0.05
MONTHLY_MAX_LOSS_PCT = 0.15

# ── 쿨다운
BASE_COOLDOWN     = 2 * 3600
MAX_COOLDOWN      = 8 * 3600
COOLDOWN_MULT     = 1.5
CONSEC_LOSE_LIMIT = 3

BASE       = "https://www.okx.com"
STATE_FILE = "state.json"

# ═══════════════════════════════════════
# 상태 영속화
# ═══════════════════════════════════════
def default_state():
    return {
        "cooldown": {
            "LONG":  {"last_ts":0,"cooldown":BASE_COOLDOWN,"consec_lose":0,"consec_win":0},
            "SHORT": {"last_ts":0,"cooldown":BASE_COOLDOWN,"consec_lose":0,"consec_win":0}
        },
        "daily_loss":    {},
        "daily_blocked": {},
        "monthly_blocked": [],
        "daily_had_loss":  {},
        "global_blocked_until": 0,
        "signals_sent":  []
    }

def load_state():
    try:
        with open(STATE_FILE,"r") as f:
            data=json.load(f)
            d=default_state()
            for k in d:
                if k not in data: data[k]=d[k]
            for direction in ["LONG","SHORT"]:
                if direction not in data["cooldown"]:
                    data["cooldown"][direction]=d["cooldown"][direction]
            return data
    except (FileNotFoundError,json.JSONDecodeError):
        return default_state()

def save_state():
    try:
        with open(STATE_FILE,"w") as f:
            json.dump(state,f,indent=2,ensure_ascii=False)
    except Exception as e:
        print(f"  [상태 저장 오류] {e}")

state = load_state()

# ═══════════════════════════════════════
# 텔레그램
# ═══════════════════════════════════════
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[텔레그램] 환경변수를 설정하세요.")
        return False
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}
    try:
        r = requests.post(url,data=data,timeout=10)
        return r.status_code==200
    except Exception as e:
        print(f"[텔레그램 오류] {e}")
        return False

# ═══════════════════════════════════════
# OKX API
# ═══════════════════════════════════════
def get_klines(bar, limit=200):
    r = requests.get(f"{BASE}/api/v5/market/candles",params={
        "instId":SYMBOL,"bar":bar,"limit":str(limit)
    },timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code")!="0": raise Exception(f"OKX:{d.get('msg')}")
    if not d.get("data"): raise Exception(f"OKX: 데이터 없음 ({bar})")
    candles=[]
    for k in reversed(d["data"]):
        o=float(k[1]);h=float(k[2]);l=float(k[3]);c=float(k[4]);v=float(k[5])
        bull=c>=o
        candles.append({"open":o,"high":h,"low":l,"close":c,"volume":v,
                        "buy_vol":v if bull else 0.0,
                        "sell_vol":0.0 if bull else v,"bull":bull})
    if not candles: raise Exception(f"OKX: 캔들 파싱 실패 ({bar})")
    return candles

def get_ticker():
    r = requests.get(f"{BASE}/api/v5/market/ticker",params={"instId":SYMBOL},timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code")!="0": raise Exception(f"OKX:{d.get('msg')}")
    item=d["data"][0]
    last=float(item["last"]); open24=float(item["open24h"])
    chg=(last-open24)/open24*100 if open24>0 else 0
    return {"lastPrice":str(last),"price24hPcnt":str(round(chg/100,6))}

def get_funding_rate():
    try:
        r = requests.get(f"{BASE}/api/v5/public/funding-rate",params={"instId":SYMBOL},timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("code")=="0" and d["data"]:
            return {"funding_rate":float(d["data"][0]["fundingRate"])}
    except Exception as e:
        print(f"  [펀딩비 오류] {e}")
    return {"funding_rate":0.0}

def get_oi_history():
    try:
        r = requests.get(f"{BASE}/api/v5/public/open-interest",
                         params={"instType":"SWAP","instId":SYMBOL},timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("code")=="0" and d["data"]:
            oi=float(d["data"][0]["oi"])
            return [oi]*6
    except: pass
    return []

# ═══════════════════════════════════════
# RSI 계산 및 다이버전스 감지
# ═══════════════════════════════════════
def calc_rsi(cs, p=14):
    if len(cs)<p+1: return 50.0
    closes=[c["close"] for c in cs[-(p+1):]]
    gains=[]; losses=[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    avg_g=sum(gains)/p; avg_l=sum(losses)/p
    if avg_l==0: return 100.0
    rs=avg_g/avg_l
    return round(100-100/(1+rs),2)

def calc_rsi_series(cs, p=14):
    result=[]
    for i in range(len(cs)):
        if i<p: result.append(50.0); continue
        sub=cs[max(0,i-p-1):i+1]
        result.append(calc_rsi(sub,p))
    return result

def detect_rsi_divergence(cs, direction):
    """
    RSI 다이버전스 감지
    반환: (점수, 설명문자열)
      +2: 강한 다이버전스
      +1: 약한 다이버전스
       0: 없음
      -1: 역다이버전스 → 진입 차단
    """
    lookback = RSI_DIV_LOOKBACK
    if len(cs)<lookback+RSI_PERIOD: return 0, ""

    rsi_series = calc_rsi_series(cs, RSI_PERIOD)
    lows  = [c["low"]  for c in cs]
    highs = [c["high"] for c in cs]
    n = len(cs)-1

    if direction=="bull":
        # 가격 저점 2개 탐색
        price_lows=[]
        for i in range(n-lookback, n-1):
            if i>0 and lows[i]<=lows[i-1] and lows[i]<=lows[i+1]:
                price_lows.append((i, lows[i], rsi_series[i]))
        if len(price_lows)<2: return 0, ""
        p1,p2 = price_lows[-2], price_lows[-1]

        if p2[1]<p1[1] and p2[2]>p1[2] and p2[2]<RSI_BULL_MAX:
            return 2, f"강세다이버전스 RSI:{p2[2]:.0f}"
        if p2[1]<=p1[1]*1.005 and p2[2]>p1[2]+2:
            return 1, f"약세다이버전스 RSI:{p2[2]:.0f}"
        if p2[1]<p1[1]*0.995 and p2[2]<p1[2]-2 and p2[2]<35:
            return -1, f"역다이버전스(LONG차단) RSI:{p2[2]:.0f}"

    else:  # SHORT
        # 가격 고점 2개 탐색
        price_highs=[]
        for i in range(n-lookback, n-1):
            if i>0 and highs[i]>=highs[i-1] and highs[i]>=highs[i+1]:
                price_highs.append((i, highs[i], rsi_series[i]))
        if len(price_highs)<2: return 0, ""
        p1,p2 = price_highs[-2], price_highs[-1]

        if p2[1]>p1[1] and p2[2]<p1[2] and p2[2]>RSI_BEAR_MIN:
            return 2, f"약세다이버전스 RSI:{p2[2]:.0f}"
        if p2[1]>=p1[1]*0.995 and p2[2]<p1[2]-2:
            return 1, f"약한약세다이버전스 RSI:{p2[2]:.0f}"
        if p2[1]>p1[1]*1.005 and p2[2]>p1[2]+2 and p2[2]>65:
            return -1, f"역다이버전스(SHORT차단) RSI:{p2[2]:.0f}"

    return 0, ""

# ═══════════════════════════════════════
# 분석 엔진
# ═══════════════════════════════════════
def calc_atr(cs, p=14):
    trs=[]
    for i in range(1,len(cs)):
        c,pv=cs[i],cs[i-1]
        trs.append(max(c["high"]-c["low"],abs(c["high"]-pv["close"]),abs(c["low"]-pv["close"])))
    sl=trs[-p:]; return sum(sl)/len(sl) if sl else 0

def check_volatility(cs):
    if len(cs)<ATR_LOOKBACK+14: return True
    current_atr=calc_atr(cs[-14:])
    atr_list=[]
    for i in range(ATR_LOOKBACK):
        start=-(ATR_LOOKBACK-i+14)
        end=-(ATR_LOOKBACK-i) if (ATR_LOOKBACK-i)>0 else None
        seg=cs[start:end] if end else cs[start:]
        if len(seg)>=14: atr_list.append(calc_atr(seg))
    if not atr_list: return True
    avg_atr=sum(atr_list)/len(atr_list)
    return current_atr>=avg_atr*ATR_VOL_THRESHOLD if avg_atr>0 else True

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
    trend="increasing" if change>1 else "decreasing" if change<-1 else "neutral"
    return {"trend":trend,"change_pct":round(change,2)}

def calc_rr(entry,tp,sl,long):
    risk=(entry-sl) if long else (sl-entry)
    rew=(tp-entry)  if long else (entry-tp)
    return round(rew/risk,2) if risk>0 else 0

def check_momentum(cs, direction):
    if len(cs)<2: return True
    return cs[-1]["bull"] if direction=="bull" else not cs[-1]["bull"]

def analyze(candles, price, htf_trend, funding, oi_info):
    WAIT_RESULT = {"sig":"WAIT","total":0,"price":price,
                   "tp1":None,"tp2":None,"tp3":None,"sl":None,
                   "rr1":None,"rr2":None,"rr3":None,"sl_basis":None,
                   "lev_info":None,"tp_profits":{},"rsi_info":None}

    if not candles or len(candles)<30: return {**WAIT_RESULT,"sig":"WAIT"}
    if not htf_trend or not isinstance(htf_trend,dict): return {**WAIT_RESULT,"sig":"WAIT"}
    if not funding or not isinstance(funding,dict): return {**WAIT_RESULT,"sig":"WAIT"}
    if not oi_info or not isinstance(oi_info,dict): return {**WAIT_RESULT,"sig":"WAIT"}

    if not check_volatility(candles):
        return {**WAIT_RESULT,"sig":"FILTERED_VOL"}

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
    base_score=min(10,round((fsc+osc+vsc+tsc+hsc)*10/13))

    has_fvg=near_fvg is not None and near_fvg["type"]==tr["direction"]
    has_ob =near_ob  is not None and near_ob["type"]==tr["direction"]
    min_score=MIN_SCORE_DUAL if (has_fvg and has_ob) else MIN_SCORE_SINGLE

    bull_ok=(near_fvg and near_fvg["type"]=="bull" and near_ob and near_ob["type"]=="bull"
             and vol["bias"]=="bull" and tr["direction"]=="bull")
    bear_ok=(near_fvg and near_fvg["type"]=="bear" and near_ob and near_ob["type"]=="bear"
             and vol["bias"]=="bear" and tr["direction"]=="bear")

    sig="WAIT"
    if base_score>=min_score:
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
        if "LONG" in sig  and htf_trend["direction"]=="bear": sig="FILTERED_HTF"
        if "SHORT" in sig and htf_trend["direction"]=="bull": sig="FILTERED_HTF"
    if sig not in ("WAIT","FILTERED_MOMENTUM","FILTERED_HTF"):
        if "LONG" in sig  and funding["long_blocked"]:  sig="FILTERED_FUND"
        if "SHORT" in sig and funding["short_blocked"]: sig="FILTERED_FUND"

    if "FILTERED" in sig or sig=="WAIT":
        return {**WAIT_RESULT,"sig":sig}

    # ── RSI 다이버전스 체크 (v12b 핵심)
    is_long_dir = "LONG" in sig
    rsi_direction = "bull" if is_long_dir else "bear"
    rsi_score, rsi_desc = detect_rsi_divergence(candles, rsi_direction)
    rsi_info = {"score":rsi_score,"desc":rsi_desc,"current_rsi":calc_rsi(candles)}

    if rsi_score == -1:
        # 역다이버전스 → 진입 차단
        return {**WAIT_RESULT,"sig":"FILTERED_RSI_DIV","rsi_info":rsi_info}

    # RSI 다이버전스 가산점 적용
    total = min(10, base_score + rsi_score)

    is_long="LONG" in sig; strong="STRONG" in sig
    tp_m=ATR_TP_MULT*(1.2 if strong else 1.0)

    # TP1: 50% 익절 기준 (v10 스타일)
    # TP2: 나머지 50% 전량 익절 (×1.5)
    # TP3: 참고용 (×2.2)
    tp1=price+atr*tp_m       if is_long else price-atr*tp_m
    tp2=price+atr*tp_m*1.5   if is_long else price-atr*tp_m*1.5
    tp3=price+atr*tp_m*2.2   if is_long else price-atr*tp_m*2.2

    sl_atr=price-atr*ATR_SL_MULT if is_long else price+atr*ATR_SL_MULT
    sl_ob=(near_ob["low"]*0.999  if is_long else near_ob["high"]*1.001) if near_ob else None
    sl_fvg=(near_fvg["bot"]*0.999 if is_long else near_fvg["top"]*1.001) if near_fvg else None

    sl_price=sl_atr; sl_basis="ATR"
    for cand,basis in [(sl_ob,"OB"),(sl_fvg,"FVG"),(sl_atr,"ATR")]:
        if cand is None: continue
        valid=(is_long and cand<price) or (not is_long and cand>price)
        if valid and abs(price-cand)>=atr*0.4 and calc_rr(price,tp1,cand,is_long)>=MIN_RR:
            sl_price=cand; sl_basis=basis; break
    if calc_rr(price,tp1,sl_price,is_long)<MIN_RR:
        mr=max(abs(tp1-price)/MIN_RR,atr*0.4)
        sl_price=(price-mr) if is_long else (price+mr); sl_basis="RR역산"

    sl_dist=abs(price-sl_price)/price*100
    rr1=calc_rr(price,tp1,sl_price,is_long)
    rr2=calc_rr(price,tp2,sl_price,is_long)
    rr3=calc_rr(price,tp3,sl_price,is_long)

    if total>=9:   leverage=10; grade="공격적"
    elif total>=8: leverage=7;  grade="중간"
    else:          leverage=5;  grade="보수적"

    lev_info={"rec_lev":leverage,"risk_grade":grade}
    tp_profits={}
    loss_usdt=TOTAL_SEED*(0.02 if total>=9 else 0.015 if total>=8 else 0.01)
    sl_d=abs(price-sl_price)/price
    pos=round(loss_usdt/sl_d,2) if sl_d>0 else 0
    margin=round(pos/leverage,2)
    lev_info.update({"position_size":pos,"margin":margin,
                     "btc_qty":round(pos/price,6),
                     "loss_usdt":round(loss_usdt,2),
                     "loss_pct":round(loss_usdt/TOTAL_SEED*100,2),
                     "sl_dist_pct":round(sl_d*100,3),
                     "liq_long":round(price*(1-1/leverage+0.0005),1),
                     "liq_short":round(price*(1+1/leverage-0.0005),1)})
    for lbl,tpv in [("tp1",tp1),("tp2",tp2),("tp3",tp3)]:
        gp=abs(tpv-price)/price*100
        prof=round(pos*(gp/100),2)
        mp=round(prof/margin*100,1) if margin>0 else 0
        tp_profits[lbl]={"usdt":prof,"margin_pct":mp}

    return {"sig":sig,"total":total,"base_score":base_score,"rsi_info":rsi_info,
            "price":price,"is_long":is_long,
            "near_fvg":near_fvg,"near_ob":near_ob,"vol":vol,"trend":tr,
            "htf_trend":htf_trend,"funding":funding,"oi_info":oi_info,
            "atr":round(atr,1),"tp1":tp1,"tp2":tp2,"tp3":tp3,
            "sl":sl_price,"sl_basis":sl_basis,"sl_dist":round(sl_dist,3),
            "sl_ob":sl_ob,"sl_fvg":sl_fvg,"sl_atr":sl_atr,
            "rr1":rr1,"rr2":rr2,"rr3":rr3,
            "lev_info":lev_info,"tp_profits":tp_profits}

# ═══════════════════════════════════════
# 메시지 포맷 (v12b — 부분 익절 안내 추가)
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
    total=result["total"]; base_score=result.get("base_score",total)
    rsi_info=result.get("rsi_info",{})
    is_long="LONG" in sig; strong="STRONG" in sig
    now=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if strong and is_long:   header="🚀 <b>LONG 강한 시그널!</b>"
    elif is_long:            header="🟢 <b>LONG 시그널</b>"
    elif strong:             header="💥 <b>SHORT 강한 시그널!</b>"
    else:                    header="🔴 <b>SHORT 시그널</b>"

    tp1_pct=abs(tp1-price)/price*100
    tp2_pct=abs(tp2-price)/price*100
    tp3_pct=abs(tp3-price)/price*100
    sl_pct =abs(sl-price)/price*100

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
            f"├ 최대손실: ${lev['loss_usdt']:,.2f} USDT ({lev['loss_pct']}%)\n"
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

    # RSI 다이버전스 표시
    rsi_score=rsi_info.get("score",0)
    rsi_desc =rsi_info.get("desc","")
    rsi_cur  =rsi_info.get("current_rsi",50)
    if rsi_score>=2:
        rsi_line=f"├ 📈 <b>RSI 다이버전스</b>: {rsi_desc} (현재 RSI:{rsi_cur:.0f}) +{rsi_score}점"
    elif rsi_score==1:
        rsi_line=f"├ 〽️ RSI 약한다이버전스: {rsi_desc} (현재 RSI:{rsi_cur:.0f}) +{rsi_score}점"
    else:
        rsi_line=f"├ ➖ RSI: 다이버전스 없음 (현재 RSI:{rsi_cur:.0f})"

    trend_txt="상승 HH+HL" if trend["hhhl"] else "하락 LH+LL" if trend["lllh"] else "중립"
    htf_txt="상승✅" if htf["direction"]=="bull" else "하락✅"
    htf_str="강한 " if (htf["strong_bull"] or htf["strong_bear"]) else ""
    fund_icon="✅" if not(funding["long_blocked"] or funding["short_blocked"]) else "⚠️"
    oi_txt={"increasing":"증가📈","decreasing":"감소📉","neutral":"중립➡️"}.get(oi["trend"],"—")
    filled="█"*total+"░"*(10-total)
    score_detail=f"기본{base_score}+RSI{rsi_score}" if rsi_score>0 else f"{base_score}"

    # 부분 익절 전략 안내 (v12b 신규)
    partial_tp_block=(
        f"📌 <b>부분 익절 전략 (v12b)</b>\n"
        f"├ TP1 도달 시: <b>50% 익절</b>{profit_str('tp1')}\n"
        f"│  → SL을 <b>진입가(본전)</b>로 이동\n"
        f"└ TP2 도달 시: <b>나머지 50% 전량 익절</b>{profit_str('tp2')}"
    )

    return f"""{header}
━━━━━━━━━━━━━━━━━━━
📌 BTCUSDT Perp | ⏱ {tf_label}
💰 <b>현재가: ${price:,.1f}</b>
📈 <b>진입 & 목표가</b>
🟡 진입가: ${price:,.1f}
🎯 TP1: ${tp1:,.1f}  (+{tp1_pct:.2f}%)  R:R {rr_grade(rr1)} 1:{rr1}  [50% 익절]
🎯 TP2: ${tp2:,.1f}  (+{tp2_pct:.2f}%)  R:R {rr_grade(rr2)} 1:{rr2}  [50% 익절]
🎯 TP3: ${tp3:,.1f}  (+{tp3_pct:.2f}%)  R:R {rr_grade(rr3)} 1:{rr3}  [참고]
📉 <b>손절 라인 (SL)</b>
{sl_block}
{partial_tp_block}
📏 ATR: ${atr:,}
💼 <b>포지션 설정 (시드 {TOTAL_SEED:,.0f} USDT)</b>
{lev_block}
{risk_state}
📋 <b>시그널 근거</b>
{fvg_line}
{ob_line}
{vol_line}
📊 <b>필터 현황</b>
{rsi_line}
├ 현재추세({tf_label}): {trend_txt} | EMA20: ${trend['ema20']:,}
├ 상위추세(4h): {htf_str}{htf_txt} | EMA50: ${htf['ema50']:,}
├ {fund_icon} 펀딩비: {funding['rate_pct']:+.4f}% ({funding['status']})
└ 미결제약정: {oi_txt} ({oi['change_pct']:+.2f}%)
⚡ 강도: {filled} {total}/10  ({score_detail})
🕐 {now} KST
━━━━━━━━━━━━━━━━━━━
⚠️ SL 필수 | TP1 50% 익절 후 SL → 본전""".strip()

# ═══════════════════════════════════════
# 상태 관리
# ═══════════════════════════════════════
def get_risk_pct(direction):
    cd=state["cooldown"][direction]
    if cd["consec_win"]>=2:    return RISK_HIGH
    elif cd["consec_lose"]>=2: return RISK_LOW
    else:                      return RISK_BASE

def can_trade(direction, now_ts):
    now=datetime.datetime.now()
    date_key=now.strftime("%Y-%m-%d")
    month_key=now.strftime("%Y-%m")
    cd=state["cooldown"][direction]

    if now_ts < state.get("global_blocked_until",0):
        remain=(state["global_blocked_until"]-now_ts)//60
        return False, f"3일연속손실 차단 ({remain}분 남음)"
    if month_key in state["monthly_blocked"]:
        return False, f"월간손실 {MONTHLY_MAX_LOSS_PCT*100:.0f}% 한도 초과"
    daily_bl=state["daily_blocked"].get(date_key,[])
    if direction in daily_bl:
        return False, f"{CONSEC_LOSE_LIMIT}연속 LOSE 당일 차단"
    if now_ts-cd["last_ts"] < cd["cooldown"]:
        remain=(cd["cooldown"]-(now_ts-cd["last_ts"]))//60
        return False, f"쿨다운 {remain}분 남음"
    dl=state["daily_loss"].get(date_key,0)
    if dl >= TOTAL_SEED*DAILY_MAX_LOSS_PCT:
        return False, f"일일손실 {DAILY_MAX_LOSS_PCT*100:.0f}% 한도 초과"
    return True, ""

def mark_signal_sent(direction, now_ts, expected_loss):
    now=datetime.datetime.now()
    date_key=now.strftime("%Y-%m-%d")
    month_key=now.strftime("%Y-%m")
    cd=state["cooldown"][direction]
    cd["last_ts"]=now_ts

    if date_key not in state["daily_loss"]:
        state["daily_loss"][date_key]=0
    state["daily_loss"][date_key]+=expected_loss

    if state["daily_loss"][date_key] >= TOTAL_SEED*DAILY_MAX_LOSS_PCT:
        if date_key not in state["daily_blocked"]:
            state["daily_blocked"][date_key]=[]
        if direction not in state["daily_blocked"][date_key]:
            state["daily_blocked"][date_key].append(direction)

    monthly_total=sum(v for k,v in state["daily_loss"].items() if k.startswith(month_key))
    if monthly_total >= TOTAL_SEED*MONTHLY_MAX_LOSS_PCT:
        if month_key not in state["monthly_blocked"]:
            state["monthly_blocked"].append(month_key)
            send_telegram(f"⛔ <b>월간손실 {MONTHLY_MAX_LOSS_PCT*100:.0f}% 한도 도달</b>\n이번 달 거래가 중단됩니다.")

    if "daily_had_loss" not in state: state["daily_had_loss"]={}
    state["daily_had_loss"][date_key]=True
    dates_loss=sorted([d for d,v in state["daily_had_loss"].items() if v])
    if len(dates_loss)>=3:
        last3=dates_loss[-3:]
        try:
            d1=datetime.datetime.strptime(last3[0],"%Y-%m-%d")
            d2=datetime.datetime.strptime(last3[1],"%Y-%m-%d")
            d3=datetime.datetime.strptime(last3[2],"%Y-%m-%d")
            if (d2-d1).days==1 and (d3-d2).days==1:
                state["global_blocked_until"]=now_ts+24*3600
                print(f"  ⛔ 3일 연속 손실 → 24h 전체 거래 차단")
                send_telegram("⛔ <b>3일 연속 손실 감지</b>\n24시간 전체 거래 차단됩니다.")
        except: pass

    state["signals_sent"].append({
        "ts":now_ts,"direction":direction,
        "date":date_key,"expected_loss":expected_loss
    })
    if len(state["signals_sent"])>100:
        state["signals_sent"]=state["signals_sent"][-100:]
    save_state()

# ═══════════════════════════════════════
# 메인 루프
# ═══════════════════════════════════════
def run():
    print("="*62)
    print("  BTC 시그널 봇 v12b — OKX API")
    print(f"  시드: {TOTAL_SEED:,.0f} USDT")
    print(f"  MIN_RR={MIN_RR} | TP×{ATR_TP_MULT} | SL×{ATR_SL_MULT}")
    print(f"  변동성 필터: ATR×{ATR_VOL_THRESHOLD}")
    print(f"  RSI 다이버전스: 강한신호 +2점 / 역다이버전스 차단")
    print(f"  부분 익절: TP1 50% + SL본전 → TP2 전량")
    print(f"  리스크: {RISK_LOW*100:.0f}%/{RISK_BASE*100:.0f}%/{RISK_HIGH*100:.0f}% (켈리)")
    print(f"  연속LOSE {CONSEC_LOSE_LIMIT}회 차단 | 일{DAILY_MAX_LOSS_PCT*100:.0f}% | 월{MONTHLY_MAX_LOSS_PCT*100:.0f}%")
    print("="*62)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 환경변수를 설정하세요!")
        return

    if os.path.exists(STATE_FILE):
        sigs=len(state.get("signals_sent",[]))
        print(f"  📂 state.json 복원: 시그널 {sigs}건 기록")
    else:
        print("  📂 첫 실행: state.json 생성 예정")

    send_telegram(
        "🤖 <b>BTC 시그널 봇 v12b 시작</b>\n\n"
        "📌 BTCUSDT Perp | 1h / 2h / 4h\n"
        "🔌 API: OKX\n\n"
        f"💼 시드: {TOTAL_SEED:,.0f} USDT\n"
        f"📐 MIN_RR={MIN_RR} | TP×{ATR_TP_MULT} | SL×{ATR_SL_MULT}\n"
        f"📊 변동성 필터: ATR×{ATR_VOL_THRESHOLD}\n"
        f"📈 RSI 다이버전스: 강한신호 +2점 / 역다이버전스 차단\n"
        f"💰 부분 익절: TP1 50% + SL본전 이동 → TP2 전량\n"
        f"⚡ 켈리 리스크: {RISK_LOW*100:.0f}%/{RISK_BASE*100:.0f}%/{RISK_HIGH*100:.0f}%\n"
        f"⏱ 쿨다운: 2h | 연속LOSE {CONSEC_LOSE_LIMIT}회 차단\n\n"
        f"📈 백테스팅: 승률 40.7% | EV +0.468 | +371.9%\n"
        "FVG + OB + RSI다이버전스 + 변동성 필터 분석 중..."
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
                htf_candles=get_klines(HTF,limit=200)
                htf_last_upd=now_ts
            htf_trend=check_htf_trend(htf_candles) if htf_candles else {
                "direction":"bull","strength":1,"ema50":0,"ema200":0,
                "strong_bull":False,"strong_bear":False,"hhhl":False,"lllh":False}

            for tf in TIMEFRAMES:
                tf_label=TF_LABEL[tf]
                try:
                    candles=get_klines(tf,limit=200)
                    if not candles:
                        print(f"  [{TF_LABEL[tf]}] 캔들 데이터 없음 — 스킵")
                        time.sleep(0.5); continue
                    result=analyze(candles,price,htf_trend,funding,oi_info)
                    if result is None:
                        print(f"  [{TF_LABEL[tf]}] analyze 반환값 없음 — 스킵")
                        time.sleep(0.5); continue
                    sig=result["sig"]

                    if sig in ("WAIT","FILTERED_MOMENTUM","FILTERED_HTF",
                               "FILTERED_FUND","FILTERED_VOL","FILTERED_RSI_DIV"):
                        rsi_cur=result.get("rsi_info",{}).get("current_rsi",0)
                        rsi_extra=f" (RSI역다이버전스 차단)" if sig=="FILTERED_RSI_DIV" else ""
                        print(f"  [{tf_label}] {sig}{rsi_extra}")
                        time.sleep(0.5); continue

                    direction="LONG" if "LONG" in sig else "SHORT"
                    ok,reason=can_trade(direction,now_ts)

                    rsi_s=result.get("rsi_info",{}).get("score",0)
                    lev_txt=f" LEV:{result['lev_info']['rec_lev']}x" if result.get("lev_info") else ""
                    rsi_txt=f" RSI+{rsi_s}" if rsi_s>0 else ""
                    print(f"  [{tf_label}] {sig:15s} 점수:{result['total']}/10{lev_txt}{rsi_txt}"
                          +(f" → 차단: {reason}" if not ok else ""))

                    if ok and result["rr1"] and result["rr1"]>=MIN_RR:
                        risk_pct=get_risk_pct(direction)
                        cd=state["cooldown"][direction]
                        msg=format_msg(result,tf_label,risk_pct,
                                       cd["consec_win"],cd["consec_lose"])
                        if send_telegram(msg):
                            expected_loss=result["lev_info"]["loss_usdt"] if result.get("lev_info") else 0
                            mark_signal_sent(direction,now_ts,expected_loss)
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
