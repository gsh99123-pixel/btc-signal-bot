#!/usr/bin/env python3
"""
BTC 자동매매 봇 v1
시그널: OKX API (데이터 수집)
실행:   Bybit API (실제 주문)
마진:   격리마진 (Isolated)
시드:   $100 시범운용
"""

import time, datetime, requests, hmac, hashlib, json, os
from collections import defaultdict

# ═══════════════════════════════════════
# 환경변수에서 키 로드 (보안)
# ═══════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")

# ═══════════════════════════════════════
# 설정값
# ═══════════════════════════════════════
OKX_SYMBOL     = "BTC-USDT-SWAP"
BYBIT_SYMBOL   = "BTCUSDT"
TIMEFRAMES     = ["1H", "2H", "4H"]
TF_LABEL       = {"1H":"1h","2H":"2h","4H":"4h"}
HTF            = "4H"
CHECK_INTERVAL = 60

# ── 시그널 조건 (B-Final)
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

# ── 리스크 관리 ($100 시범운용)
TOTAL_SEED           = 100.0   # $100 시범운용
RISK_BASE            = 0.015   # 1.5%
RISK_HIGH            = 0.020   # 연속WIN 2회+ → 2.0%
RISK_LOW             = 0.010   # 연속LOSE 2회+ → 1.0%
MIN_LEV              = 5
MAX_LEV              = 10

# ── 손실 한도
DAILY_MAX_LOSS_PCT   = 0.05
MONTHLY_MAX_LOSS_PCT = 0.15

# ── 쿨다운
BASE_COOLDOWN     = 2 * 3600
MAX_COOLDOWN      = 8 * 3600
COOLDOWN_MULT     = 1.5
CONSEC_LOSE_LIMIT = 4

OKX_BASE   = "https://www.okx.com"
BYBIT_BASE = "https://api.bybit.com"

# ═══════════════════════════════════════
# 텔레그램
# ═══════════════════════════════════════
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[텔레그램 미설정] {msg[:50]}")
        return False
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}
    try:
        r = requests.post(url, data=data, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[텔레그램 오류] {e}")
        return False

# ═══════════════════════════════════════
# OKX API (시그널용)
# ═══════════════════════════════════════
def get_klines_okx(bar, limit=200):
    r = requests.get(f"{OKX_BASE}/api/v5/market/candles", params={
        "instId":OKX_SYMBOL,"bar":bar,"limit":str(limit)
    }, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "0":
        raise Exception(f"OKX:{d.get('msg')}")
    candles=[]
    for k in reversed(d["data"]):
        o=float(k[1]);h=float(k[2]);l=float(k[3]);c=float(k[4]);v=float(k[5])
        bull=c>=o
        candles.append({"open":o,"high":h,"low":l,"close":c,"volume":v,
                        "buy_vol":v if bull else 0.0,
                        "sell_vol":0.0 if bull else v,"bull":bull})
    return candles

def get_ticker_okx():
    r = requests.get(f"{OKX_BASE}/api/v5/market/ticker",
                     params={"instId":OKX_SYMBOL},timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "0": raise Exception(f"OKX:{d.get('msg')}")
    item=d["data"][0]
    last=float(item["last"]); open24=float(item["open24h"])
    chg=(last-open24)/open24*100 if open24>0 else 0
    return {"lastPrice":str(last),"price24hPcnt":str(round(chg/100,6))}

def get_funding_okx():
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/public/funding-rate",
                         params={"instId":OKX_SYMBOL},timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("code")=="0" and d["data"]:
            return {"funding_rate":float(d["data"][0]["fundingRate"])}
    except: pass
    return {"funding_rate":0.0}

def get_oi_okx():
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/public/open-interest",
                         params={"instType":"SWAP","instId":OKX_SYMBOL},timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("code")=="0" and d["data"]:
            oi=float(d["data"][0]["oi"])
            return [oi]*6
    except: pass
    return []

# ═══════════════════════════════════════
# Bybit API (주문용)
# ═══════════════════════════════════════
def bybit_sign(params_str):
    return hmac.new(BYBIT_API_SECRET.encode(),
                    params_str.encode(), hashlib.sha256).hexdigest()

def bybit_request(method, endpoint, params=None, body=None):
    ts = str(int(time.time() * 1000))
    recv_window = "5000"

    if method == "GET":
        query = "&".join(f"{k}={v}" for k,v in sorted((params or {}).items()))
        sign_str = ts + BYBIT_API_KEY + recv_window + query
        sig = bybit_sign(sign_str)
        headers = {
            "X-BAPI-API-KEY":    BYBIT_API_KEY,
            "X-BAPI-TIMESTAMP":  ts,
            "X-BAPI-SIGN":       sig,
            "X-BAPI-RECV-WINDOW":recv_window
        }
        r = requests.get(f"{BYBIT_BASE}{endpoint}",
                         params=params, headers=headers, timeout=10)
    else:
        body_str = json.dumps(body or {})
        sign_str = ts + BYBIT_API_KEY + recv_window + body_str
        sig = bybit_sign(sign_str)
        headers = {
            "X-BAPI-API-KEY":    BYBIT_API_KEY,
            "X-BAPI-TIMESTAMP":  ts,
            "X-BAPI-SIGN":       sig,
            "X-BAPI-RECV-WINDOW":recv_window,
            "Content-Type":      "application/json"
        }
        r = requests.post(f"{BYBIT_BASE}{endpoint}",
                          data=body_str, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()

def get_balance_bybit():
    """잔고 조회"""
    try:
        d = bybit_request("GET","/v5/account/wallet-balance",
                          params={"accountType":"UNIFIED","coin":"USDT"})
        if d.get("retCode")==0:
            coins=d["result"]["list"][0]["coin"]
            for c in coins:
                if c["coin"]=="USDT":
                    return float(c["availableToWithdraw"])
    except Exception as e:
        print(f"[잔고조회 오류] {e}")
    return 0.0

def get_position_bybit():
    """현재 포지션 조회"""
    try:
        d = bybit_request("GET","/v5/position/list",
                          params={"category":"linear","symbol":BYBIT_SYMBOL})
        if d.get("retCode")==0:
            for p in d["result"]["list"]:
                if float(p.get("size","0")) > 0:
                    return {
                        "side":      p["side"],
                        "size":      float(p["size"]),
                        "entryPrice":float(p["avgPrice"]),
                        "leverage":  float(p["leverage"]),
                        "unrealPnl": float(p["unrealisedPnl"]),
                        "liqPrice":  float(p.get("liqPrice","0")),
                        "posIdx":    p.get("positionIdx","0")
                    }
    except Exception as e:
        print(f"[포지션조회 오류] {e}")
    return None

def set_leverage_bybit(leverage):
    """레버리지 설정"""
    try:
        d = bybit_request("POST","/v5/position/set-leverage", body={
            "category":"linear","symbol":BYBIT_SYMBOL,
            "buyLeverage":str(leverage),"sellLeverage":str(leverage)
        })
        return d.get("retCode")==0
    except Exception as e:
        print(f"[레버리지설정 오류] {e}")
    return False

def set_margin_mode_bybit():
    """격리마진 설정"""
    try:
        d = bybit_request("POST","/v5/position/switch-isolated", body={
            "category":"linear","symbol":BYBIT_SYMBOL,
            "tradeMode":1,  # 1=격리, 0=교차
            "buyLeverage":"5","sellLeverage":"5"
        })
        if d.get("retCode") in (0, 110026):  # 110026=이미 격리마진
            return True
    except Exception as e:
        print(f"[마진모드 오류] {e}")
    return False

def place_order_bybit(side, qty, leverage, tp_price, sl_price):
    """
    시장가 주문 + TP/SL 동시 설정
    side: "Buy" or "Sell"
    """
    try:
        set_margin_mode_bybit()
        set_leverage_bybit(leverage)
        time.sleep(0.3)

        body = {
            "category":    "linear",
            "symbol":      BYBIT_SYMBOL,
            "side":        side,
            "orderType":   "Market",
            "qty":         str(qty),
            "takeProfit":  str(round(tp_price, 1)),
            "stopLoss":    str(round(sl_price, 1)),
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice",
            "timeInForce": "GTC",
            "positionIdx": 0   # 단방향 모드
        }
        d = bybit_request("POST","/v5/order/create", body=body)
        if d.get("retCode")==0:
            return {"success":True,"orderId":d["result"]["orderId"]}
        else:
            return {"success":False,"error":d.get("retMsg","")}
    except Exception as e:
        return {"success":False,"error":str(e)}

def close_position_bybit(side, qty):
    """포지션 청산"""
    close_side = "Sell" if side=="Buy" else "Buy"
    try:
        body = {
            "category":"linear","symbol":BYBIT_SYMBOL,
            "side":close_side,"orderType":"Market",
            "qty":str(qty),"reduceOnly":True,
            "timeInForce":"GTC","positionIdx":0
        }
        d = bybit_request("POST","/v5/order/create",body=body)
        return d.get("retCode")==0
    except Exception as e:
        print(f"[청산 오류] {e}")
    return False

def get_min_qty_bybit(price):
    """최소 주문 수량 계산 (BTC 단위, 소수점 3자리)"""
    try:
        d = bybit_request("GET","/v5/market/instruments-info",
                          params={"category":"linear","symbol":BYBIT_SYMBOL})
        if d.get("retCode")==0:
            info=d["result"]["list"][0]
            min_qty=float(info["lotSizeFilter"]["minOrderQty"])
            qty_step=float(info["lotSizeFilter"]["qtyStep"])
            return min_qty, qty_step
    except: pass
    return 0.001, 0.001

# ═══════════════════════════════════════
# 분석 엔진
# ═══════════════════════════════════════
def calc_atr(cs,p=14):
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
                "tp1":None,"sl":None,"rr1":None,"leverage":None}

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

    # 레버리지 결정
    if total>=9:   leverage=10; grade="공격적"
    elif total>=8: leverage=7;  grade="중간"
    else:          leverage=5;  grade="보수적"

    return {"sig":sig,"total":total,"price":price,"is_long":is_long,
            "tp1":tp1,"tp2":tp2,"tp3":tp3,"sl":sl_price,"sl_basis":sl_basis,
            "sl_dist":round(sl_dist,3),"rr1":rr1,"rr2":rr2,"rr3":rr3,
            "atr":round(atr,1),"leverage":leverage,"grade":grade,
            "near_fvg":near_fvg,"near_ob":near_ob,"vol":vol,"trend":tr,
            "htf_trend":htf_trend,"funding":funding,"oi_info":oi_info}

# ═══════════════════════════════════════
# 포지션 크기 계산
# ═══════════════════════════════════════
def calc_position(price, sl_price, risk_pct, leverage):
    """
    손실금액 = 포지션크기 × SL거리
    포지션크기 = 손실금액 / SL거리
    BTC수량 = 포지션크기 / 현재가
    증거금 = 포지션크기 / 레버리지
    """
    risk_amt = TOTAL_SEED * risk_pct
    sl_dist  = abs(price - sl_price) / price
    if sl_dist == 0: return None
    pos_size = risk_amt / sl_dist
    margin   = pos_size / leverage
    btc_qty  = pos_size / price

    # 최소 수량 체크
    min_qty, qty_step = get_min_qty_bybit(price)
    btc_qty = max(btc_qty, min_qty)
    # 수량 단위에 맞게 반올림
    btc_qty = round(round(btc_qty / qty_step) * qty_step, 6)

    return {
        "pos_size":  round(pos_size, 2),
        "margin":    round(margin, 2),
        "btc_qty":   btc_qty,
        "risk_amt":  round(risk_amt, 2),
        "sl_dist":   round(sl_dist*100, 3)
    }

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

# 현재 포지션 추적
active_trade = None  # {"side", "entry", "tp1", "sl", "qty", "leverage", "time", "tf"}

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

def update_after_close(direction, pnl, risk_amt, date_key, month_key):
    """포지션 종료 후 상태 업데이트"""
    cd=cooldown_state[direction]
    cd["last_ts"]=int(time.time())
    if pnl < 0:
        daily_loss[date_key]+=abs(pnl)
        cd["consec_lose"]+=1; cd["consec_win"]=0
        if cd["consec_lose"]>=CONSEC_LOSE_LIMIT:
            daily_blocked[date_key].add(direction)
        cd["cooldown"]=min(cd["cooldown"]*COOLDOWN_MULT, MAX_COOLDOWN)
        # 월간 손실 체크
        # (간단히 일손실 누적으로 근사)
    else:
        cd["consec_win"]+=1; cd["consec_lose"]=0
        cd["cooldown"]=BASE_COOLDOWN

# ═══════════════════════════════════════
# 포지션 모니터링
# ═══════════════════════════════════════
def monitor_active_position(price):
    """
    활성 포지션 TP/SL 도달 여부 확인
    Bybit이 자동으로 TP/SL 처리하지만
    봇에서도 추적하여 텔레그램 알림 전송
    """
    global active_trade
    if not active_trade: return

    pos = get_position_bybit()
    now = datetime.datetime.now()
    date_key  = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")

    # 포지션이 닫힌 경우 (TP or SL 도달)
    if pos is None:
        entry  = active_trade["entry"]
        tp1    = active_trade["tp1"]
        sl     = active_trade["sl"]
        side   = active_trade["side"]
        qty    = active_trade["qty"]
        lev    = active_trade["leverage"]
        tf     = active_trade["tf"]
        is_long= side=="Buy"

        # 어떤 결과인지 판단
        if is_long:
            if price >= tp1 * 0.999:
                result="WIN"; pnl=round((price-entry)*qty,2)
                emoji="✅"; reason="TP1 도달"
            elif price <= sl * 1.001:
                result="LOSE"; pnl=round((price-entry)*qty,2)
                emoji="❌"; reason="SL 도달"
            else:
                result="CLOSED"; pnl=round((price-entry)*qty,2)
                emoji="⏹️"; reason="수동청산 또는 청산"
        else:
            if price <= tp1 * 1.001:
                result="WIN"; pnl=round((entry-price)*qty,2)
                emoji="✅"; reason="TP1 도달"
            elif price >= sl * 0.999:
                result="LOSE"; pnl=round((entry-price)*qty,2)
                emoji="❌"; reason="SL 도달"
            else:
                result="CLOSED"; pnl=round((entry-price)*qty,2)
                emoji="⏹️"; reason="수동청산 또는 청산"

        duration = (now - active_trade["time"]).seconds // 60

        # 원인 분석
        analysis = ""
        if result == "LOSE":
            sl_dist = abs(entry-sl)/entry*100
            analysis = (
                f"\n\n📊 <b>원인 분석</b>\n"
                f"├ SL 거리: {sl_dist:.3f}%\n"
                f"├ SL 기준: {active_trade.get('sl_basis','')}\n"
                f"├ 보유시간: {duration}분\n"
                f"└ 개선 포인트: SL이 너무 좁거나 추세 역행"
            )
        elif result == "WIN":
            profit_pct = pnl / (active_trade.get("margin",1)) * 100
            analysis = (
                f"\n\n📊 <b>수익 분석</b>\n"
                f"├ 수익률(증거금): +{profit_pct:.1f}%\n"
                f"├ 보유시간: {duration}분\n"
                f"└ 패턴: {tf} {side} 성공"
            )

        msg = (
            f"{emoji} <b>포지션 종료 — {result}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📌 {BYBIT_SYMBOL} | {tf} | {side}\n"
            f"💰 진입가: ${entry:,.1f}\n"
            f"💰 종료가: ${price:,.1f}\n"
            f"🎯 TP1:   ${tp1:,.1f}\n"
            f"🛑 SL:    ${sl:,.1f}\n"
            f"📦 수량:  {qty} BTC\n"
            f"⚡ 레버리지: {lev}x\n"
            f"💵 손익:  {pnl:+.2f} USDT\n"
            f"⏱ 사유: {reason}"
            f"{analysis}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {now.strftime('%Y-%m-%d %H:%M:%S')} KST"
        )
        send_telegram(msg)

        direction = "LONG" if is_long else "SHORT"
        update_after_close(direction, pnl, 0, date_key, month_key)
        active_trade = None
        print(f"  포지션 종료: {result} PnL={pnl:+.2f} USDT")

# ═══════════════════════════════════════
# 메인 루프
# ═══════════════════════════════════════
def run():
    global active_trade

    # Railway 서버 IP 확인
    try:
        ip_r = requests.get("https://api.ipify.org?format=json", timeout=5)
        server_ip = ip_r.json()["ip"]
        print(f"  Railway 서버 IP: {server_ip}")
    except Exception as e:
        print(f"  IP 확인 실패: {e}")

    print("="*60)
    print("  BTC 자동매매 봇 v1")
    print(f"  시드: ${TOTAL_SEED} | 마진: 격리 | API: OKX→Bybit")
    print(f"  리스크: {RISK_LOW*100:.0f}%/{RISK_BASE*100:.0f}%/{RISK_HIGH*100:.0f}% (켈리)")
    print(f"  일손실: {DAILY_MAX_LOSS_PCT*100:.0f}% | 월손실: {MONTHLY_MAX_LOSS_PCT*100:.0f}%")
    print("="*60)

    # API 키 확인
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        print("❌ BYBIT_API_KEY 또는 BYBIT_API_SECRET 환경변수 없음")
        send_telegram("❌ 자동매매 봇 시작 실패: Bybit API 키 없음")
        return

    # 잔고 확인
    balance = get_balance_bybit()
    print(f"  Bybit 잔고: ${balance:.2f} USDT")

    send_telegram(
        "🤖 <b>BTC 자동매매 봇 v1 시작</b>\n\n"
        f"📌 {BYBIT_SYMBOL} 선물 | 격리마진\n"
        f"💼 시드: ${TOTAL_SEED} USDT\n"
        f"💰 Bybit 잔고: ${balance:.2f} USDT\n"
        f"⚡ 켈리 리스크: {RISK_LOW*100:.0f}%/{RISK_BASE*100:.0f}%/{RISK_HIGH*100:.0f}%\n"
        f"⏱ 쿨다운: 2h | 연속LOSE {CONSEC_LOSE_LIMIT}회 차단\n"
        f"🛡️ 일손실 {DAILY_MAX_LOSS_PCT*100:.0f}% | 월손실 {MONTHLY_MAX_LOSS_PCT*100:.0f}%\n\n"
        "시그널 분석 시작..."
    )

    htf_candles=[]; htf_last_upd=0; cycle=0

    while True:
        cycle+=1
        now_str=datetime.datetime.now().strftime("%H:%M:%S")
        now_ts=int(time.time())
        print(f"\n[{now_str}] 사이클 #{cycle}")

        try:
            ticker=get_ticker_okx()
            price=float(ticker["lastPrice"])
            chg=float(ticker["price24hPcnt"])*100
            print(f"  현재가: ${price:,.1f} ({chg:+.2f}%)")

            # 활성 포지션 모니터링
            monitor_active_position(price)

            # 이미 포지션 있으면 신규 진입 안 함
            if active_trade:
                pos=get_position_bybit()
                if pos:
                    print(f"  활성포지션: {pos['side']} {pos['size']}BTC "
                          f"진입${pos['entryPrice']:,.1f} "
                          f"미실현PnL:{pos['unrealPnl']:+.2f}")
                time.sleep(CHECK_INTERVAL); continue

            fund_raw=get_funding_okx()
            funding=check_funding(fund_raw)
            print(f"  펀딩비: {funding['rate_pct']:+.4f}% ({funding['status']})")

            oi_hist=get_oi_okx()
            oi_info=check_oi_trend(oi_hist)

            if now_ts-htf_last_upd>300:
                htf_candles=get_klines_okx(HTF,limit=200)
                htf_last_upd=now_ts
            htf_trend=check_htf_trend(htf_candles) if htf_candles else {
                "direction":"bull","strength":1,"ema50":0,"ema200":0,
                "strong_bull":False,"strong_bear":False,"hhhl":False,"lllh":False}

            best_signal = None  # 가장 높은 점수 시그널 선택

            for tf in TIMEFRAMES:
                tf_label=TF_LABEL[tf]
                try:
                    candles=get_klines_okx(tf,limit=200)
                    result=analyze(candles,price,htf_trend,funding,oi_info)
                    sig=result["sig"]

                    if sig in ("WAIT","FILTERED_MOMENTUM","FILTERED_HTF","FILTERED_FUND"):
                        print(f"  [{tf_label}] {sig}")
                        time.sleep(0.5); continue

                    direction="LONG" if "LONG" in sig else "SHORT"
                    ok,reason=can_trade(direction,now_ts)

                    print(f"  [{tf_label}] {sig:15s} 점수:{result['total']}/10 "
                          f"LEV:{result['leverage']}x"
                          +(f" → 차단: {reason}" if not ok else ""))

                    if ok and result["rr1"] and result["rr1"]>=MIN_RR:
                        if best_signal is None or result["total"]>best_signal["total"]:
                            best_signal = {**result, "tf_label":tf_label}

                    time.sleep(0.5)
                except Exception as e:
                    print(f"  ⚠️ [{tf_label}] 오류: {e}")

            # 최고 점수 시그널로 진입
            if best_signal:
                sig       = best_signal["sig"]
                direction = "LONG" if "LONG" in sig else "SHORT"
                is_long   = best_signal["is_long"]
                leverage  = best_signal["leverage"]
                tp1       = best_signal["tp1"]
                sl        = best_signal["sl"]
                sl_basis  = best_signal["sl_basis"]
                rr1       = best_signal["rr1"]
                tf_label  = best_signal["tf_label"]
                atr       = best_signal["atr"]

                risk_pct  = get_risk_pct(direction)
                pos_info  = calc_position(price, sl, risk_pct, leverage)

                if not pos_info:
                    print("  ⚠️ 포지션 크기 계산 실패")
                else:
                    side = "Buy" if is_long else "Sell"
                    qty  = pos_info["btc_qty"]

                    print(f"\n  🚀 주문 실행: {side} {qty}BTC @ ${price:,.1f}")
                    print(f"     TP: ${tp1:,.1f} | SL: ${sl:,.1f} | LEV: {leverage}x")
                    print(f"     증거금: ${pos_info['margin']} | 리스크: ${pos_info['risk_amt']}")

                    order = place_order_bybit(side, qty, leverage, tp1, sl)

                    if order["success"]:
                        active_trade = {
                            "side":      side,
                            "entry":     price,
                            "tp1":       tp1,
                            "tp2":       best_signal["tp2"],
                            "tp3":       best_signal["tp3"],
                            "sl":        sl,
                            "sl_basis":  sl_basis,
                            "qty":       qty,
                            "leverage":  leverage,
                            "margin":    pos_info["margin"],
                            "risk_amt":  pos_info["risk_amt"],
                            "tf":        tf_label,
                            "sig":       sig,
                            "time":      datetime.datetime.now(),
                            "order_id":  order["orderId"]
                        }
                        cooldown_state[direction]["last_ts"]=now_ts

                        tp1_pct=abs(tp1-price)/price*100
                        sl_pct =abs(sl-price)/price*100

                        send_telegram(
                            f"{'🚀' if 'STRONG' in sig and is_long else '🟢' if is_long else '💥' if 'STRONG' in sig else '🔴'} "
                            f"<b>{'LONG' if is_long else 'SHORT'} 자동진입 완료!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 {BYBIT_SYMBOL} | {tf_label} | 격리마진\n"
                            f"⚡ 레버리지: {leverage}x ({best_signal['grade']})\n\n"
                            f"💰 진입가:  ${price:,.1f}\n"
                            f"🎯 TP1:    ${tp1:,.1f}  (+{tp1_pct:.2f}%)  R:R 1:{rr1}\n"
                            f"🎯 TP2:    ${best_signal['tp2']:,.1f}\n"
                            f"🎯 TP3:    ${best_signal['tp3']:,.1f}\n"
                            f"🛑 SL:     ${sl:,.1f}  (-{sl_pct:.2f}%) [{sl_basis}]\n\n"
                            f"📦 수량:   {qty} BTC\n"
                            f"💵 증거금: ${pos_info['margin']} USDT\n"
                            f"⚠️ 최대손실: ${pos_info['risk_amt']} USDT "
                            f"({risk_pct*100:.1f}%)\n"
                            f"📏 ATR:    ${atr:,}\n\n"
                            f"⚡ 시그널 강도: {best_signal['total']}/10\n"
                            f"🔑 주문ID: {order['orderId']}\n"
                            f"🕐 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} KST"
                        )
                        print(f"  ✅ 주문 성공! ID: {order['orderId']}")
                    else:
                        err=order["error"]
                        print(f"  ❌ 주문 실패: {err}")
                        send_telegram(
                            f"❌ <b>주문 실패</b>\n"
                            f"사유: {err}\n"
                            f"시그널: {sig} | {tf_label}\n"
                            f"진입가: ${price:,.1f}\n"
                            f"→ 원인 분석 후 재시도"
                        )

        except Exception as e:
            print(f"  ❌ 오류: {e}")
            send_telegram(f"⚠️ 봇 오류 발생\n{str(e)[:200]}")

        print(f"  → {CHECK_INTERVAL}초 후 재실행...")
        time.sleep(CHECK_INTERVAL)

if __name__=="__main__":
    run()
