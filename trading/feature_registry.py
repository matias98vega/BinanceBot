#!/usr/bin/env python3
"""Canonical passive feature schema and local pre-entry derivations."""
import math
from datetime import datetime, timezone

FEATURE_SCHEMA_VERSION=2
FEATURE_CAPTURE_VERSION='preentry-context-v2'

def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def _number(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError):return None
def _pct(a,b):
    a,b=_number(a),_number(b)
    return ((a/b)-1)*100 if a is not None and b not in (None,0) else None
def _ret(values,n):
    return _pct(values[-1],values[-1-n]) if len(values)>n else None
def _slope(values,n=3):
    return _pct(values[-1],values[-1-n])/n if len(values)>n else None
def _std_returns(values,n):
    values=values[-(n+1):]
    returns=[_pct(values[i],values[i-1]) for i in range(1,len(values))]
    returns=[x for x in returns if x is not None]
    if len(returns)<2:return None
    mean=sum(returns)/len(returns)
    return math.sqrt(sum((x-mean)**2 for x in returns)/(len(returns)-1))
def _ema(values,period):
    if not values:return []
    alpha=2/(period+1);out=[values[0]]
    for value in values[1:]:out.append(alpha*value+(1-alpha)*out[-1])
    return out
def _rsi(values,period=14):
    if len(values)<period+2:return None
    gains=[];losses=[]
    for a,b in zip(values[-period-1:-1],values[-period:]):
        delta=b-a;gains.append(max(delta,0));losses.append(max(-delta,0))
    gain=sum(gains)/period;loss=sum(losses)/period
    return 100 if loss==0 else 100-(100/(1+gain/loss))
def _atr(highs,lows,closes,period=14):
    if len(closes)<period+1:return None
    tr=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
    return sum(tr[-period:])/period
def _safe_div(a,b):
    a,b=_number(a),_number(b)
    return a/b if a is not None and b not in (None,0) else None

FEATURE_REGISTRY={
 'ema20_slope_pct':('numeric','pct_per_candle','1h EMA20 local slope','KEEP_WITH_NORMALIZATION'),
 'ema50_slope_pct':('numeric','pct_per_candle','1h EMA50 local slope','KEEP_WITH_NORMALIZATION'),
 'ema20_50_spread_pct':('numeric','pct','EMA20 relative to EMA50','KEEP'),
 'price_above_ema20':('boolean','flag','pre-entry close vs EMA20','KEEP'),
 'price_above_ema50':('boolean','flag','pre-entry close vs EMA50','KEEP'),
 'trend_alignment':('categorical','category','price/EMA20/EMA50 ordering','KEEP'),
 'return_1_candle':('numeric','pct','1h close return','KEEP'),
 'return_3_candles':('numeric','pct','3h close return','KEEP'),
 'return_6_candles':('numeric','pct','6h close return','KEEP'),
 'return_12_candles':('numeric','pct','12h close return','KEEP'),
 'rsi_delta_1':('numeric','points','RSI current minus previous close RSI','KEEP_WITH_NORMALIZATION'),
 'rsi_delta_3':('numeric','points','RSI current minus three-close RSI','KEEP_WITH_NORMALIZATION'),
 'macd_hist_delta_1':('numeric','price','MACD histogram delta','KEEP_WITH_NORMALIZATION'),
 'macd_hist_acceleration':('numeric','price','second difference MACD histogram','KEEP_WITH_NORMALIZATION'),
 'atr_pct_delta':('numeric','pct_points','current vs previous ATR percent','KEEP_WITH_NORMALIZATION'),
 'atr_expansion_ratio':('numeric','ratio','current ATR / prior ATR','KEEP_WITH_NORMALIZATION'),
 'realized_volatility_short':('numeric','pct','std of six 1h returns','KEEP'),
 'realized_volatility_medium':('numeric','pct','std of 24 1h returns','KEEP'),
 'volatility_ratio_short_medium':('numeric','ratio','short / medium realized volatility','KEEP'),
 'candle_range_pct':('numeric','pct','last high-low / close','KEEP'),
 'body_to_range_ratio':('numeric','ratio','absolute open-close / high-low','KEEP'),
 'wick_imbalance':('numeric','ratio','upper minus lower wick / range','KEEP'),
 'volume_ratio_short':('numeric','ratio','last volume / six-candle mean','KEEP'),
 'volume_ratio_medium':('numeric','ratio','last volume / 24-candle mean','KEEP'),
 'volume_trend':('numeric','ratio','six-candle mean / prior six mean','KEEP'),
 'quote_volume':('numeric','quote_asset','last base volume times close','KEEP_WITH_NORMALIZATION'),
 'distance_to_recent_high_pct':('numeric','pct','distance to 24-candle high','KEEP'),
 'distance_to_recent_low_pct':('numeric','pct','distance from 24-candle low','KEEP'),
 'range_position':('numeric','ratio','position inside 24-candle high-low','KEEP'),
 'higher_highs_count':('numeric','count','higher highs in last six transitions','KEEP'),
 'lower_lows_count':('numeric','count','lower lows in last six transitions','KEEP'),
 'asset_return_minus_btc_1h':('numeric','pct','asset 1h return minus BTC 1h','KEEP'),
 'asset_return_minus_btc_4h':('numeric','pct','asset 4h return minus BTC 4h','KEEP'),
 'expected_tp_pct':('numeric','pct','candidate target distance','KEEP'),
 'expected_sl_pct':('numeric','pct','candidate stop distance','KEEP'),
 'reward_risk_ratio':('numeric','ratio','expected reward / expected risk','KEEP'),
 'stop_distance_atr':('numeric','ratio','stop distance / ATR','KEEP'),
 'target_distance_atr':('numeric','ratio','target distance / ATR','KEEP'),
 'concurrent_open_positions':('numeric','count','positions before order','KEEP'),
 'same_side_open_positions':('numeric','count','same-side positions before order','KEEP'),
 'opposite_side_open_positions':('numeric','count','opposite-side positions before order','KEEP'),
}

def registry_records():
    return [{'name':name,'schema':FEATURE_SCHEMA_VERSION,'type':v[0],'unit':v[1],
      'source':'already-loaded candidate klines/state','timeframe':'1h unless named otherwise',
      'formula':v[2],'available_before_entry':True,'side_applicability':'BOTH',
      'missing_policy':'optional_null','leakage_risk':'LOW_PREENTRY_CAPTURED',
      'stability_status':'NOT_YET_OBSERVED','recommendation':v[3]} for name,v in FEATURE_REGISTRY.items()]

def build_preentry_context(candidate,closes,highs,lows,volumes,opens=None,klines=None,btc_context=None):
    captured_at=_now();missing=[];quality=[]
    closes=[_number(x) for x in closes];highs=[_number(x) for x in highs];lows=[_number(x) for x in lows];volumes=[_number(x) for x in volumes]
    if not closes or any(x is None for x in closes+highs+lows+volumes):
        return {'feature_schema_version':FEATURE_SCHEMA_VERSION,'feature_capture_version':FEATURE_CAPTURE_VERSION,'capture':{'captured_at':captured_at,'quality_flags':['MISSING_KLINE_HISTORY'],'missing_fields':['klines']},'features':{}}
    opens=[_number(x) for x in (opens or closes)];e20=_ema(closes,20);e50=_ema(closes,50)
    rsi_now=_rsi(closes);rsi_1=_rsi(closes[:-1]);rsi_3=_rsi(closes[:-3])
    fast=_ema(closes,12);slow=_ema(closes,26);macd=[a-b for a,b in zip(fast,slow)];signal=_ema(macd,9);hist=[a-b for a,b in zip(macd,signal)]
    atr_now=_atr(highs,lows,closes);atr_prev=_atr(highs[:-1],lows[:-1],closes[:-1])
    last=closes[-1];recent_high=max(highs[-24:]);recent_low=min(lows[-24:]);span=recent_high-recent_low
    candle_range=highs[-1]-lows[-1];upper=highs[-1]-max(opens[-1],last);lower=min(opens[-1],last)-lows[-1]
    entry=_number(candidate.get('price'));sl=_number(candidate.get('sl'));tp=_number(candidate.get('tp'));atr=_number(candidate.get('atr'))
    risk=abs(entry-sl) if entry is not None and sl is not None else None;reward=abs(tp-entry) if entry is not None and tp is not None else None
    btc=btc_context or {};btc1=_number(btc.get('change_1h'));btc4=_number(btc.get('change_4h'))
    features={
      'ema20_slope_pct':_slope(e20),'ema50_slope_pct':_slope(e50),'ema20_50_spread_pct':_pct(e20[-1],e50[-1]),
      'price_above_ema20':last>e20[-1],'price_above_ema50':last>e50[-1],
      'trend_alignment':'BULL' if last>e20[-1]>e50[-1] else 'BEAR' if last<e20[-1]<e50[-1] else 'MIXED',
      'return_1_candle':_ret(closes,1),'return_3_candles':_ret(closes,3),'return_6_candles':_ret(closes,6),'return_12_candles':_ret(closes,12),
      'rsi_delta_1':rsi_now-rsi_1 if rsi_now is not None and rsi_1 is not None else None,'rsi_delta_3':rsi_now-rsi_3 if rsi_now is not None and rsi_3 is not None else None,
      'macd_hist_delta_1':hist[-1]-hist[-2] if len(hist)>1 else None,'macd_hist_acceleration':hist[-1]-2*hist[-2]+hist[-3] if len(hist)>2 else None,
      'atr_pct_delta':_pct(_safe_div(atr_now,last),_safe_div(atr_prev,closes[-2])),'atr_expansion_ratio':_safe_div(atr_now,atr_prev),
      'realized_volatility_short':_std_returns(closes,6),'realized_volatility_medium':_std_returns(closes,24),
      'volatility_ratio_short_medium':_safe_div(_std_returns(closes,6),_std_returns(closes,24)),
      'candle_range_pct':_safe_div(candle_range,last)*100 if last else None,'body_to_range_ratio':_safe_div(abs(last-opens[-1]),candle_range),
      'wick_imbalance':_safe_div(upper-lower,candle_range),'volume_ratio_short':_safe_div(volumes[-1],sum(volumes[-6:])/min(6,len(volumes))),
      'volume_ratio_medium':_safe_div(volumes[-1],sum(volumes[-24:])/min(24,len(volumes))),
      'volume_trend':_safe_div(sum(volumes[-6:])/min(6,len(volumes)),sum(volumes[-12:-6])/max(1,len(volumes[-12:-6]))),
      'quote_volume':volumes[-1]*last,'distance_to_recent_high_pct':_pct(recent_high,last),'distance_to_recent_low_pct':_pct(last,recent_low),
      'range_position':_safe_div(last-recent_low,span),'higher_highs_count':sum(highs[i]>highs[i-1] for i in range(max(1,len(highs)-6),len(highs))),
      'lower_lows_count':sum(lows[i]<lows[i-1] for i in range(max(1,len(lows)-6),len(lows))),
      'asset_return_minus_btc_1h':(_ret(closes,1)-btc1) if _ret(closes,1) is not None and btc1 is not None else None,
      'asset_return_minus_btc_4h':(_ret(closes,4)-btc4) if _ret(closes,4) is not None and btc4 is not None else None,
      'expected_tp_pct':abs(_pct(tp,entry)) if tp is not None else None,'expected_sl_pct':abs(_pct(sl,entry)) if sl is not None else None,
      'reward_risk_ratio':_safe_div(reward,risk),'stop_distance_atr':_safe_div(risk,atr),'target_distance_atr':_safe_div(reward,atr),
    }
    for key,value in features.items():
        if value is None:missing.append(key)
    candle_open=None;candle_close=None
    if klines:
        candle_open=klines[-1][0];candle_close=klines[-1][6] if len(klines[-1])>6 else None
        if candle_close and datetime.now(timezone.utc).timestamp()*1000<float(candle_close):quality.append('OPEN_CANDLE_CONTAMINATION_RISK')
    else:quality.append('UNKNOWN_CANDLE_BOUNDARY')
    return {'feature_schema_version':FEATURE_SCHEMA_VERSION,'feature_capture_version':FEATURE_CAPTURE_VERSION,
      'capture':{'captured_at':captured_at,'source_timestamp':candle_close,'candle_open_time':candle_open,'candle_close_time':candle_close,
      'missing_fields':missing,'quality_flags':quality,'available_before_entry':True},'features':features}

def safe_build_preentry_context(*args,**kwargs):
    try:
        return build_preentry_context(*args,**kwargs)
    except Exception:
        return None

def enrich_bot_context(candidate,state):
    try:
        context=candidate.get('passive_feature_context')
        if not isinstance(context,dict):
            return candidate
        positions=(state or {}).get('positions') or []
        side=str(candidate.get('direction') or '').lower()
        context['features'].update({
          'concurrent_open_positions':len(positions),
          'same_side_open_positions':sum(str(p.get('direction')).lower()==side for p in positions),
          'opposite_side_open_positions':sum(str(p.get('direction')).lower()!=side for p in positions),
        })
    except Exception:
        pass
    return candidate
