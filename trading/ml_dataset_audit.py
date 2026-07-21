#!/usr/bin/env python3
"""Formal, reproducible and read-only audit of BinanceBot ML dataset readiness."""
import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import feature_store
import history
import version_history
from config_loader import PROJECT_DIR

AUDIT_SCHEMA_VERSION = 1
DEFAULT_ANALYTICS_FILE = os.path.join(PROJECT_DIR, 'trading', 'trade_analytics.jsonl')
DEFAULT_OUTPUT_HINT = os.path.join(PROJECT_DIR, 'data', 'analysis', 'ml_dataset_audit')
MINIMUM_FEATURES = ('rsi', 'atr', 'ema20', 'ema50', 'volume_ratio', 'score')
NUMERIC_FEATURES = {
    'btc_price': (0, None), 'btc_change_1h': (None, None), 'btc_change_4h': (None, None),
    'btc_change_daily': (None, None), 'volatility': (0, None), 'atr': (0, None),
    'atr_pct': (0, None), 'rsi': (0, 100), 'macd': (None, None), 'macd_hist': (None, None),
    'ema20': (0, None), 'ema50': (0, None), 'ema200': (0, None),
    'distance_to_ema20_pct': (None, None), 'distance_to_ema50_pct': (None, None),
    'distance_to_ema200_pct': (None, None), 'volume_ratio': (0, None), 'spread': (0, None),
    'btc_correlation': (-1, 1), 'hour_utc': (0, 23), 'weekday': (0, 6), 'score': (0, None),
    'confidence': (0, 1), 'capital': (0, None), 'notional': (0, None), 'quantity': (0, None),
    'leverage': (0, None), 'sl_pct': (0, None), 'tp_pct': (0, None), 'max_positions': (0, None),
    'exposure': (0, None), 'entry_price': (0, None),
}
FORBIDDEN_INPUT_KEYS = {
    'pnl', 'pnl_usdt', 'pnl_pct', 'exit_reason', 'exit_price', 'closed_at', 'closing_timestamp',
    'duration_seconds', 'duration_minutes', 'max_favorable_excursion', 'max_adverse_excursion', 'result',
}


def _read_jsonl(path):
    rows, errors = [], []
    if not path or not os.path.isfile(path):
        return rows, errors
    with open(path, encoding='utf-8') as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    row = dict(row); row['_source_line'] = line_no; rows.append(row)
                else:
                    errors.append(f'{path}:{line_no}:not_object')
            except Exception as exc:
                errors.append(f'{path}:{line_no}:{type(exc).__name__}')
    return rows, errors


def _dt(value):
    if value in (None, ''):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value), timezone.utc)
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result).astimezone(timezone.utc)
    except Exception:
        return None


def _iso(value):
    parsed = _dt(value)
    return parsed.replace(microsecond=0).isoformat().replace('+00:00', 'Z') if parsed else None


def _num(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _base_id(trade_id):
    return str(trade_id or '').split(':partial', 1)[0]


def _regime(value):
    value = history.normalise_regime(value)
    return 'neutral' if value == 'sideways' else value


def _nested(row, *path):
    value = row
    for key in path:
        if not isinstance(value, dict): return None
        value = value.get(key)
    return value


def _first(*values):
    return next((v for v in values if v not in (None, '', [], {})), None)


def _open_ts(row):
    return _iso(_first(row.get('opened_at'), row.get('entry_time'), row.get('timestamp'), _nested(row, 'identification', 'timestamp')))


def _close_ts(row):
    return _iso(_first(row.get('closed_at'), row.get('exit_time'), row.get('timestamp')))


def _is_open(row):
    return str(row.get('event_type') or '').upper() == 'TRADE_OPEN' or (str(row.get('status') or '').upper() == 'OPEN' and not row.get('exit_time') and not row.get('closed_at'))


def _is_close(row):
    return str(row.get('event_type') or '').upper() == 'TRADE_CLOSE' or str(row.get('status') or '').upper() == 'CLOSED'


def _is_recovered(row):
    text = json.dumps(row, sort_keys=True).lower()
    return bool(row.get('recovered_existing_position') or any(x in text for x in ('recovered', 'recovery_reason', 'backfill', 'synthetic')))


def _exit_reason(value):
    value = str(value or '').upper()
    return {'STALE_EXIT':'STALE','PARTIAL_TP':'PARTIAL','CLOSED_TP':'TP','CLOSED_SL':'SL'}.get(value, value or None)

def _feature_id(row):
    return _first(_nested(row, 'identification', 'trade_id'), row.get('trade_id'))


def _feature_ts(row):
    return _iso(_first(_nested(row, 'identification', 'timestamp'), row.get('timestamp'), row.get('entry_time')))


def _feature_values(row, opening):
    market = row.get('market') if isinstance(row.get('market'), dict) else {}
    ind = row.get('symbol_indicators') if isinstance(row.get('symbol_indicators'), dict) else {}
    scoring = row.get('scoring') if isinstance(row.get('scoring'), dict) else {}
    capital = row.get('capital') if isinstance(row.get('capital'), dict) else {}
    btc = opening.get('btc_context') if isinstance(opening.get('btc_context'), dict) else {}
    extra = opening.get('extra') if isinstance(opening.get('extra'), dict) else {}
    ts = _dt(_open_ts(opening))
    return {
        'symbol': _first(_nested(row, 'identification', 'symbol'), opening.get('symbol')),
        'side': str(_first(_nested(row, 'identification', 'direction'), opening.get('side')) or '').upper() or None,
        'bot_version': _first(_nested(row, 'identification', 'bot_version'), row.get('bot_version'), opening.get('bot_version')),
        'strategy_version': _first(_nested(row, 'identification', 'strategy_version'), row.get('strategy_version'), opening.get('strategy_version')),
        'regime': _regime(_first(market.get('regime'), opening.get('regime'), opening.get('market_regime'))),
        'btc_regime': _regime(_first(market.get('btc_regime'), btc.get('trend'))),
        'btc_price': _first(market.get('btc_price'), btc.get('btc_price')),
        'btc_change_1h': _first(market.get('btc_change_1h'), btc.get('change_1h'), btc.get('btc_change_1h')),
        'btc_change_4h': _first(market.get('btc_change_4h'), btc.get('change_4h'), btc.get('btc_change_4h')),
        'btc_change_daily': _first(market.get('btc_change_daily'), btc.get('change_24h')),
        'volatility': _first(market.get('volatility'), opening.get('volatility')),
        'atr': _first(ind.get('atr'), market.get('atr'), opening.get('atr')),
        'atr_pct': _first(opening.get('atr_pct'), extra.get('atr_pct')),
        'rsi': _first(ind.get('rsi'), opening.get('rsi'), extra.get('rsi')),
        'macd': ind.get('macd'), 'macd_hist': _first(ind.get('macd_hist'), extra.get('macd_hist')),
        'ema20': _first(ind.get('ema20'), extra.get('ema20')), 'ema50': _first(ind.get('ema50'), extra.get('ema50')),
        'ema200': ind.get('ema200'), 'distance_to_ema20_pct': ind.get('distance_to_ema20_pct'),
        'distance_to_ema50_pct': ind.get('distance_to_ema50_pct'), 'distance_to_ema200_pct': ind.get('distance_to_ema200_pct'),
        'volume_ratio': _first(ind.get('volume_ratio'), market.get('volume_ratio'), extra.get('volume_ratio')),
        'spread': market.get('spread'), 'btc_correlation': _first(market.get('btc_correlation'), extra.get('btc_correlation')),
        'hour_utc': market.get('hour_utc') if market.get('hour_utc') is not None else (ts.hour if ts else None),
        'weekday': market.get('weekday') if market.get('weekday') is not None else (ts.weekday() if ts else None),
        'score': _first(scoring.get('score_total'), opening.get('score')), 'confidence': scoring.get('confidence'),
        'capital': _first(capital.get('position_final'), opening.get('capital_used'), opening.get('capital_at_entry')),
        'notional': capital.get('notional'), 'quantity': _first(capital.get('quantity'), opening.get('quantity')),
        'leverage': capital.get('leverage'), 'sl_pct': ind.get('sl_pct'), 'tp_pct': ind.get('tp_pct'),
        'max_positions': capital.get('max_positions'), 'exposure': _first(capital.get('exposure_pct'), capital.get('position_calculated')),
        'entry_price': _first(ind.get('entry_price'), opening.get('entry_price')),
    }


def _percentile(values, fraction):
    if not values: return None
    ordered = sorted(values); pos = (len(ordered)-1)*fraction; lo=int(pos); hi=min(lo+1,len(ordered)-1)
    return ordered[lo] + (ordered[hi]-ordered[lo])*(pos-lo)


def _fingerprint(paths, options):
    hashes = {}
    for name, path in sorted(paths.items()):
        if os.path.isfile(path):
            h=hashlib.sha256()
            with open(path,'rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
            hashes[name]=h.hexdigest()
        else: hashes[name]='MISSING'
    payload={'schema':AUDIT_SCHEMA_VERSION,'commit':_git_commit(),'source_hashes':hashes,'options':options}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest(), hashes


def _git_commit():
    try: return subprocess.check_output(['git','rev-parse','--short','HEAD'],cwd=PROJECT_DIR,text=True).strip()
    except Exception: return 'unknown'


def _chronology_report(manifest, closed):
    durations = [r['labels']['duration_seconds'] for r in closed if r['labels']['duration_seconds'] is not None]
    ordered = sorted((_dt(r['opening_timestamp']) for r in manifest if _dt(r['opening_timestamp'])))
    gaps = [(b-a).total_seconds() for a,b in zip(ordered, ordered[1:]) if (b-a).total_seconds() > 6*3600]
    density_day = Counter((r['opening_timestamp'] or 'unknown')[:10] for r in manifest)
    density_week = Counter(f'{d.isocalendar().year}-W{d.isocalendar().week:02d}' if (d:=_dt(r['opening_timestamp'])) else 'unknown' for r in manifest)
    return {'flags':dict(Counter(x for r in manifest for x in r['chronology_flags'])),'duration_min':min(durations,default=None),'duration_max':max(durations,default=None),'duration_median':statistics.median(durations) if durations else None,'instant_closures':sum(d==0 for d in durations),'negative_durations':sum(d<0 for d in durations),'gaps_over_6h':len(gaps),'max_gap_seconds':max(gaps,default=None),'samples_by_day':dict(density_day),'samples_by_week':dict(density_week)}


def audit_dataset(trades_file=history.DEFAULT_TRADES_FILE, features_file=feature_store.DEFAULT_FEATURES_FILE,
                  analytics_file=DEFAULT_ANALYTICS_FILE, version=None, side=None, regime=None,
                  date_from=None, date_to=None, min_sample=60):
    paths={'trades':trades_file,'features':features_file,'trade_analytics':analytics_file}
    options={'version':version,'side':side,'regime':regime,'from':date_from,'to':date_to,'min_sample':int(min_sample)}
    fingerprint, source_hashes = _fingerprint(paths, options)
    trades, trade_errors = _read_jsonl(trades_file); analytics, analytics_errors = _read_jsonl(analytics_file); features, feature_errors = _read_jsonl(features_file)
    opens=defaultdict(list); closes=defaultdict(list); partials=defaultdict(list); sources=defaultdict(set)
    for source_name, rows in (('trades',trades),('trade_analytics',analytics)):
        for row in rows:
            tid=row.get('trade_id')
            if not tid: continue
            base=_base_id(tid)
            if ':partial' in str(tid).lower():
                if _is_close(row): partials[base].append((source_name,row))
                continue
            if _is_open(row): opens[base].append((source_name,row)); sources[base].add(source_name)
            if _is_close(row): closes[base].append((source_name,row)); sources[base].add(source_name)
    feature_map=defaultdict(list)
    for row in features:
        tid=_feature_id(row)
        if tid: feature_map[_base_id(tid)].append(row)
    manifest=[]
    for tid in sorted(set(opens)|set(closes)):
        open_rows=opens.get(tid,[]); close_rows=closes.get(tid,[]); feature_rows=feature_map.get(tid,[])
        primary_open=next((r for src,r in open_rows if src=='trades'), open_rows[0][1] if open_rows else {})
        primary_close=next((r for src,r in close_rows if src=='trades'), close_rows[0][1] if close_rows else {})
        opening_ts=_open_ts(primary_open); closing_ts=_close_ts(primary_close)
        item_side=str(primary_open.get('side') or primary_close.get('side') or '').upper() or None
        item_regime=_regime(_first(primary_open.get('regime'),primary_open.get('market_regime')))
        item_version=primary_open.get('bot_version') or version_history.classify_record(primary_open).get('version')
        if version and item_version != version: continue
        if side and item_side != side.upper(): continue
        if regime and item_regime != _regime(regime): continue
        if date_from and (not opening_ts or opening_ts[:10] < date_from): continue
        if date_to and (not opening_ts or opening_ts[:10] > date_to): continue
        reasons=[]; leakage=[]; chronology=[]; duplicate=[]
        recovered=any(_is_recovered(r) for _,r in open_rows+close_rows)
        if not open_rows: reasons.append('MISSING_RELIABLE_OPEN')
        if len([1 for src,_ in open_rows if src=='trades'])>1: reasons.append('MULTIPLE_PRIMARY_OPENS')
        if len([1 for src,_ in close_rows if src=='trades'])>1: reasons.append('MULTIPLE_PRIMARY_CLOSES')
        if len(open_rows)>1: duplicate.append('BENIGN_CROSS_SOURCE_OPEN' if len({(_open_ts(r),r.get('symbol'),r.get('side')) for _,r in open_rows})==1 else 'CONFLICTING_OPEN_SOURCES')
        close_signatures={(_close_ts(r), _num(r.get('pnl_usdt')), _exit_reason(r.get('exit_reason'))) for _,r in close_rows}
        if len(close_rows)>1: duplicate.append('BENIGN_CROSS_SOURCE_CLOSE' if len(close_signatures)==1 else 'CONFLICTING_CLOSE_SOURCES')
        if any(x.startswith('CONFLICTING') for x in duplicate): reasons.append('DUPLICATE_CONFLICT')
        if not closing_ts: reasons.append('OPEN_WITHOUT_CLOSED_LABEL')
        od,cd=_dt(opening_ts),_dt(closing_ts)
        if not od: chronology.append('MISSING_OPEN_TIMESTAMP')
        if closing_ts and not cd: chronology.append('INVALID_CLOSE_TIMESTAMP')
        if od and cd and cd < od: chronology.append('CLOSE_BEFORE_OPEN')
        if chronology: reasons.append('INVALID_CHRONOLOGY')
        if recovered: reasons.append('RECOVERED_OR_RECONSTRUCTED')
        feature=feature_rows[0] if len(feature_rows)==1 else (feature_rows[0] if feature_rows else {})
        if len(feature_rows)>1: duplicate.append('DUPLICATE_FEATURE_SOURCES')
        feature_ts=_feature_ts(feature) if feature else None
        if not feature: reasons.append('MISSING_OPEN_FEATURE_SNAPSHOT')
        elif not feature_ts: leakage.append('UNKNOWN_FEATURE_TIMESTAMP')
        elif od and _dt(feature_ts) and _dt(feature_ts)>od: leakage.append('FEATURE_AFTER_ENTRY')
        flat=_feature_values(feature,primary_open) if primary_open else {}
        raw_keys=set()
        def collect(value):
            if isinstance(value,dict):
                for k,v in value.items(): raw_keys.add(str(k).lower()); collect(v)
            elif isinstance(value,list):
                for v in value: collect(v)
        collect(feature)
        if raw_keys & FORBIDDEN_INPUT_KEYS: leakage.append('LABEL_IN_FEATURES')
        if feature and feature.get('recovered_existing_position'): leakage.append('POST_TRADE_RECONSTRUCTION')
        missing=[k for k in MINIMUM_FEATURES if flat.get(k) in (None,'')]
        invalid=[]
        for k,(low,high) in NUMERIC_FEATURES.items():
            if flat.get(k) in (None,''): continue
            n=_num(flat.get(k))
            if n is None: invalid.append(f'{k}:NON_FINITE_OR_NON_NUMERIC')
            elif low is not None and n<low or high is not None and n>high: invalid.append(f'{k}:OUT_OF_RANGE')
        if missing: reasons.append('MISSING_MINIMUM_FEATURES')
        if invalid: reasons.append('INVALID_FEATURE_VALUES')
        pnl=_num(primary_close.get('pnl_usdt')); pnl_pct=_num(primary_close.get('pnl_pct'))
        duration=None
        if od and cd: duration=(cd-od).total_seconds()
        labels={'binary_win':None if pnl is None else int(pnl>0),'pnl_usdt':pnl,'pnl_pct':pnl_pct,
                'return_on_capital':None,'r_multiple':None,'exit_reason':_exit_reason(primary_close.get('exit_reason')),'duration_seconds':duration}
        capital=_num(flat.get('capital'))
        if pnl is not None and capital and capital>0: labels['return_on_capital']=pnl/capital
        entry=_num(flat.get('entry_price')); sl_pct=_num(flat.get('sl_pct'))
        if pnl_pct is not None and sl_pct and sl_pct>0: labels['r_multiple']=pnl_pct/sl_pct
        if close_rows and pnl is None: reasons.append('MISSING_RELIABLE_PNL_LABEL')
        critical={'MISSING_RELIABLE_OPEN','MULTIPLE_PRIMARY_OPENS','MULTIPLE_PRIMARY_CLOSES','DUPLICATE_CONFLICT','INVALID_CHRONOLOGY','FEATURE_AFTER_ENTRY','LABEL_IN_FEATURES','MISSING_RELIABLE_PNL_LABEL'}
        if critical.intersection(reasons+leakage): classification='EXCLUDED'
        elif not closing_ts or recovered or missing or not feature or leakage or invalid: classification='PARTIAL'
        else: classification='TRUSTED'
        usable=['descriptive_open_trade_analysis'] if not closing_ts else []
        if classification=='PARTIAL' and closing_ts and pnl is not None: usable.append('label_analysis_without_main_ml_features')
        manifest.append({'trade_id':tid,'classification':classification,'reasons':sorted(set(reasons)),'valid_for':usable,
            'opening_timestamp':opening_ts,'closing_timestamp':closing_ts,'bot_version':item_version or 'unknown',
            'symbol':primary_open.get('symbol') or primary_close.get('symbol'),'side':item_side,'regime':item_regime,
            'feature_source':'features.jsonl' if feature else None,'feature_timestamp':feature_ts,
            'label_source':'trades.jsonl' if any(src=='trades' for src,_ in close_rows) else ('trade_analytics.jsonl' if close_rows else None),
            'is_closed':bool(closing_ts),'is_recovered':recovered,'is_partial':bool(partials.get(tid)),
            'partial_events':len(partials.get(tid,[])),'missing_features':missing,'invalid_features':invalid,
            'duplicate_sources':sorted(set(duplicate)),'leakage_flags':sorted(set(leakage)),'chronology_flags':sorted(set(chronology)),
            'source_files':sorted(sources.get(tid,set()) | ({'features'} if feature else set())),'features':flat,'labels':labels})
    counts=Counter(r['classification'] for r in manifest); closed=[r for r in manifest if r['is_closed']]
    feature_report={}
    for name in sorted(set(NUMERIC_FEATURES)|{'symbol','side','bot_version','strategy_version','regime','btc_regime'}):
        vals=[r['features'].get(name) for r in manifest]; valid_num=[_num(v) for v in vals if v not in (None,'') and _num(v) is not None]
        missing=sum(v in (None,'') for v in vals); invalid_count=sum(v not in (None,'') and name in NUMERIC_FEATURES and _num(v) is None for v in vals)
        bounds = NUMERIC_FEATURES.get(name)
        out_of_range = sum(1 for v in vals if bounds and _num(v) is not None and ((bounds[0] is not None and _num(v) < bounds[0]) or (bounds[1] is not None and _num(v) > bounds[1])))
        report={'expected_type':'numeric' if name in NUMERIC_FEATURES else 'categorical','total':len(vals),'valid':len(vals)-missing-invalid_count-out_of_range,
            'missing_count':missing,'missing_pct':round(missing/len(vals)*100,4) if vals else 0,'non_finite_or_invalid':invalid_count,'out_of_range':out_of_range,
            'cardinality':len({str(v) for v in vals if v not in (None,'')})}
        if valid_num:
            report.update({'mean':statistics.fmean(valid_num),'median':statistics.median(valid_num),'p25':_percentile(valid_num,.25),'p75':_percentile(valid_num,.75),'min':min(valid_num),'max':max(valid_num),'constant':len(set(valid_num))==1})
        report['source']='features.jsonl/opening event fallback'; report['relative_timestamp']='at_or_before_entry_required'
        report['leakage_risk']='UNKNOWN_TIMESTAMP_RISK' if any('UNKNOWN_FEATURE_TIMESTAMP' in r['leakage_flags'] for r in manifest) else 'NO_CONFIRMED_LEAKAGE_DETECTED'
        report['recommendation']='INCLUDE' if name in MINIMUM_FEATURES and report['missing_pct']<=10 else ('INCLUDE_WITH_TRANSFORM' if name in ('symbol','side','regime','bot_version') else 'OPTIONAL' if report['missing_pct']<50 else 'BLOCKED')
        report['coverage_by_version']=dict(Counter(r['bot_version'] for r in manifest if r['features'].get(name) not in (None,'')))
        report['coverage_by_side']=dict(Counter(r['side'] for r in manifest if r['features'].get(name) not in (None,'')))
        report['coverage_by_regime']=dict(Counter(r['regime'] for r in manifest if r['features'].get(name) not in (None,'')))
        report['coverage_by_month']=dict(Counter((r['opening_timestamp'] or 'unknown')[:7] for r in manifest if r['features'].get(name) not in (None,'')))
        feature_report[name]=report
    label_names=('binary_win','pnl_usdt','pnl_pct','return_on_capital','r_multiple','exit_reason','duration_seconds')
    label_report={}
    for name in label_names:
        present=sum(r['labels'].get(name) is not None for r in closed)
        label_report[name]={'closed_total':len(closed),'available':present,'coverage_pct':round(present/len(closed)*100,4) if closed else 0,
          'source':'closing trade event','convention':('1 iff closed pnl_usdt > 0; 0 otherwise' if name=='binary_win' else 'no inference from equity/balance'),
          'reliability':'HIGH' if name in ('binary_win','pnl_usdt','exit_reason','duration_seconds') else 'CONDITIONAL',
          'baseline_suitable':name in ('binary_win','pnl_usdt','exit_reason','duration_seconds') and present==len(closed),'ml_suitable':name=='binary_win' and present==len(closed)}
    segment_fields={'bot_version':'bot_version','side':'side','regime':'regime','symbol':'symbol','month':'opening_timestamp','week':'opening_timestamp','hour_utc':'opening_timestamp','exit_reason':None,'source':'feature_source'}
    segments={}
    for segment,field in segment_fields.items():
        buckets=defaultdict(list)
        for row in manifest:
            if segment=='month': key=(row['opening_timestamp'] or 'unknown')[:7]
            elif segment=='week':
                d=_dt(row['opening_timestamp']); key=f'{d.isocalendar().year}-W{d.isocalendar().week:02d}' if d else 'unknown'
            elif segment=='hour_utc':
                d=_dt(row['opening_timestamp']); key=str(d.hour) if d else 'unknown'
            elif segment=='exit_reason': key=row['labels'].get('exit_reason') or 'OPEN_OR_UNKNOWN'
            else: key=row.get(field) or 'unknown'
            buckets[str(key)].append(row)
        segments[segment]={}
        for key,rows in sorted(buckets.items()):
            c=Counter(r['classification'] for r in rows); labs=[r['labels']['binary_win'] for r in rows if r['labels']['binary_win'] is not None]
            segments[segment][key]={'total':len(rows),'TRUSTED':c['TRUSTED'],'PARTIAL':c['PARTIAL'],'EXCLUDED':c['EXCLUDED'],
              'usable_pct':round((c['TRUSTED']+c['PARTIAL'])/len(rows)*100,4),'closed_labelable':len(labs),
              'wins':sum(labs),'losses':len(labs)-sum(labs),'pnl_total_diagnostic':sum((r['labels']['pnl_usdt'] or 0) for r in rows)}
    trusted_closed=sorted([r for r in manifest if r['classification']=='TRUSTED' and r['labels']['binary_win'] is not None],key=lambda r:r['opening_timestamp'] or '')
    split={'status':'TEMPORAL_SPLIT_INSUFFICIENT_SAMPLE','flags':[],'blocks':{}}
    if len(trusted_closed)>=max(3,min_sample):
        a=max(1,int(len(trusted_closed)*.6)); b=max(a+1,int(len(trusted_closed)*.8)); blocks={'train':trusted_closed[:a],'validation':trusted_closed[a:b],'test':trusted_closed[b:]}
        split={'status':'READY','flags':[],'blocks':{name:{'from':rows[0]['opening_timestamp'] if rows else None,'to':rows[-1]['opening_timestamp'] if rows else None,'count':len(rows),'wins':sum(r['labels']['binary_win'] for r in rows),'losses':sum(1-r['labels']['binary_win'] for r in rows),'sides':dict(Counter(r['side'] for r in rows)),'regimes':dict(Counter(r['regime'] for r in rows)),'versions':dict(Counter(r['bot_version'] for r in rows))} for name,rows in blocks.items()}}
        if len({r['bot_version'] for r in trusted_closed})>1: split['flags'].append('VERSION_MIXING_RISK')
        if len({r['regime'] for r in trusted_closed})<2: split['flags'].append('REGIME_COVERAGE_GAP')
    else: split['flags'].append('TEMPORAL_SPLIT_INSUFFICIENT_SAMPLE')
    labs=[r['labels']['binary_win'] for r in trusted_closed]; positive=sum(labs); majority=(max(positive,len(labs)-positive)/len(labs) if labs else None)
    if labs and (positive/len(labs)<.2 or positive/len(labs)>.8): split['flags'].append('CLASS_IMBALANCE')
    if any(r['is_partial'] for r in manifest): split['flags'].append('DEPENDENT_SAMPLES_RISK')
    critical_leak=sum(any(x in ('FEATURE_AFTER_ENTRY','LABEL_IN_FEATURES','CURRENT_STATE_LEAKAGE','CLOSING_DATA_IN_OPEN_SNAPSHOT') for x in r['leakage_flags']) for r in manifest)
    critical_missing=max((feature_report[k]['missing_pct'] for k in MINIMUM_FEATURES),default=100)
    baseline_blocks=[]
    if not manifest: baseline_blocks.append('NO_TRADE_SAMPLES')
    if not trusted_closed: baseline_blocks.append('NO_TRUSTED_CLOSED_SAMPLES')
    if critical_leak: baseline_blocks.append('CRITICAL_LEAKAGE_DETECTED')
    if split['status']!='READY': baseline_blocks.append('TEMPORAL_SPLIT_INSUFFICIENT_SAMPLE')
    if critical_missing>25: baseline_blocks.append('MINIMUM_FEATURE_MISSINGNESS_ABOVE_25_PERCENT')
    ml_blocks=list(baseline_blocks)
    if len(trusted_closed)<min_sample: ml_blocks.append(f'TRUSTED_SAMPLE_BELOW_{min_sample}')
    if any(x in split['flags'] for x in ('REGIME_COVERAGE_GAP','CLASS_IMBALANCE')): ml_blocks.append('SEGMENT_COVERAGE_OR_IMBALANCE')
    if 'VERSION_MIXING_RISK' in split['flags']: ml_blocks.append('VERSION_AWARE_EVALUATION_REQUIRED')
    if 'DEPENDENT_SAMPLES_RISK' in split['flags']: ml_blocks.append('DEPENDENT_SAMPLE_GROUPING_REQUIRED')
    ml_blocks.append('FEATURE_STABILITY_NOT_YET_VALIDATED')
    reasons=Counter(reason for r in manifest for reason in r['reasons'])
    inventory={'recovered':sum(r['is_recovered'] for r in manifest),'partials':sum(r['is_partial'] for r in manifest),
      'legacy':sum(str(r['bot_version']).startswith('legacy') or r['bot_version']=='v1.0-alpha' for r in manifest),
      'unknown_version':sum(r['bot_version']=='unknown' for r in manifest),'unknown_regime':sum(r['regime']=='unknown' for r in manifest),
      'feature_reconstructed':sum('POST_TRADE_RECONSTRUCTION' in r['leakage_flags'] for r in manifest),'label_reconstructed':0,
      'partials_linked':sum(r['is_partial'] and bool(r['opening_timestamp']) for r in manifest),'partials_ambiguous':sum(r['is_partial'] and not r['opening_timestamp'] for r in manifest)}
    return {'audit_schema_version':AUDIT_SCHEMA_VERSION,'generated_at':datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z'),
      'commit':_git_commit(),'dataset_fingerprint':fingerprint,'source_hashes':source_hashes,'command_options':options,
      'sources':paths,'source_errors':trade_errors+analytics_errors+feature_errors,'summary':{'total_trades':len(manifest),'closed':len(closed),'open':len(manifest)-len(closed),'TRUSTED':counts['TRUSTED'],'PARTIAL':counts['PARTIAL'],'EXCLUDED':counts['EXCLUDED'],'trusted_closed':len(trusted_closed),'wins':positive,'losses':len(labs)-positive,'positive_class_pct':round(positive/len(labs)*100,4) if labs else None,'majority_class_baseline':majority},
      'manifest':manifest,'feature_report':feature_report,'label_report':label_report,
      'leakage_report':{'confirmed_count':critical_leak,'risk_count':sum(bool(r['leakage_flags']) for r in manifest)-critical_leak,'flags':dict(Counter(x for r in manifest for x in r['leakage_flags'])),'statement':'No detected flag proves absence of leakage; unknown timestamps remain risk.'},
      'duplicates_report':{'flags':dict(Counter(x for r in manifest for x in r['duplicate_sources']))},
      'chronology_report':_chronology_report(manifest, closed),
      'inventory':inventory,'segment_coverage':segments,'imbalance':{'majority_class_baseline':majority,'wins':positive,'losses':len(labs)-positive},
      'temporal_split':split,'excluded_reasons':dict(reasons),'statistical_baseline':_baseline_artifact_status(fingerprint),
      'readiness':{'dataset_ready_for_baseline':not baseline_blocks,'dataset_ready_for_ml':not ml_blocks,'blocking_reasons':{'baseline':sorted(set(baseline_blocks)),'ml':sorted(set(ml_blocks))},'warnings':sorted(set(split['flags'])),'recommendations':['Resolve excluded/partial records through policy, never silent backfill.','Build a reproducible statistical baseline before XGBoost.','Use temporal or grouped walk-forward splits; never random-only split.']}}


def _baseline_artifact_status(dataset_fingerprint):
    path=os.path.join(PROJECT_DIR, 'data', 'analysis', 'statistical_baseline', 'summary.json')
    status={'baseline_generated':False,'baseline_stale':False,'baseline_ready':False,'ready_for_xgboost_experiment':False}
    if not os.path.exists(path): return status
    try:
        with open(path,encoding='utf-8') as f: value=json.load(f)
        status['baseline_generated']=True
        status['baseline_stale']=value.get('dataset_fingerprint') != dataset_fingerprint or value.get('commit') != _git_commit()
        status['baseline_ready']=not status['baseline_stale'] and value.get('baseline_schema_version') == 1 and value.get('commit') == _git_commit() and isinstance(value.get('options'),dict)
        status['ready_for_xgboost_experiment']=status['baseline_ready'] and bool(value.get('ready_for_xgboost_experiment'))
    except (OSError,ValueError,TypeError): status['baseline_stale']=True
    return status


def _logical(result):
    value=dict(result); value.pop('generated_at',None); return value


def _atomic_json(path,value):
    os.makedirs(os.path.dirname(os.path.abspath(path)),exist_ok=True); tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(value,f,indent=2,sort_keys=True); f.write('\n')
    os.replace(tmp,path)


def write_artifacts(result, output):
    os.makedirs(output,exist_ok=True)
    mapping={'summary.json':{k:v for k,v in result.items() if k!='manifest'},'feature_report.json':result['feature_report'],'label_report.json':result['label_report'],'leakage_report.json':result['leakage_report'],'duplicates_report.json':result['duplicates_report'],'temporal_split.json':result['temporal_split'],'segment_coverage.json':result['segment_coverage'],'excluded_reasons.json':result['excluded_reasons']}
    for name,value in mapping.items(): _atomic_json(os.path.join(output,name),value)
    manifest_path=os.path.join(output,'manifest.jsonl'); tmp=manifest_path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f:
        for row in result['manifest']: f.write(json.dumps(row,sort_keys=True,separators=(',',':'))+'\n')
    os.replace(tmp,manifest_path)
    readme=f"# ML Dataset Audit\n\nSchema: {AUDIT_SCHEMA_VERSION}\n\nCommit: `{result['commit']}`\n\nFingerprint: `{result['dataset_fingerprint']}`\n\nGenerated: {result['generated_at']}\n\nRules and limitations are documented in `docs/ML_DATASET.md`.\n"
    tmp=os.path.join(output,'README.md.tmp')
    with open(tmp,'w',encoding='utf-8') as f: f.write(readme)
    os.replace(tmp,os.path.join(output,'README.md'))


def format_text(result, explain=False):
    s=result['summary']; ready=result['readiness']; lines=['ML DATASET AUDIT',f"Fingerprint: {result['dataset_fingerprint']}",f"Trades: {s['total_trades']} | Closed: {s['closed']} | Open: {s['open']}",f"TRUSTED: {s['TRUSTED']} | PARTIAL: {s['PARTIAL']} | EXCLUDED: {s['EXCLUDED']}",f"Trusted closed: {s['trusted_closed']} | Wins: {s['wins']} | Losses: {s['losses']}",f"Ready baseline: {ready['dataset_ready_for_baseline']}",f"Ready ML: {ready['dataset_ready_for_ml']}",f"Baseline blockers: {', '.join(ready['blocking_reasons']['baseline']) or 'none'}",f"ML blockers: {', '.join(ready['blocking_reasons']['ml']) or 'none'}"]
    if explain: lines += ['','Rules:','- one sample per base opening trade_id; :partial events never create samples','- opening version owns the sample','- labels come only from later close events','- missing/unknown values are not imputed','- Sideways is displayed as Neutral without historical rewrite','- no detected flag proves absence of leakage']
    return '\n'.join(lines)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--json',action='store_true'); p.add_argument('--explain',action='store_true'); p.add_argument('--version'); p.add_argument('--side',choices=('LONG','SHORT')); p.add_argument('--regime'); p.add_argument('--from',dest='date_from'); p.add_argument('--to',dest='date_to'); p.add_argument('--manifest'); p.add_argument('--output'); p.add_argument('--strict',action='store_true'); p.add_argument('--min-sample',type=int,default=60)
    p.add_argument('--trades-file',default=history.DEFAULT_TRADES_FILE,help=argparse.SUPPRESS); p.add_argument('--features-file',default=feature_store.DEFAULT_FEATURES_FILE,help=argparse.SUPPRESS); p.add_argument('--analytics-file',default=DEFAULT_ANALYTICS_FILE,help=argparse.SUPPRESS)
    args=p.parse_args(argv)
    if args.min_sample<1: p.error('--min-sample must be positive')
    try:
        result=audit_dataset(args.trades_file,args.features_file,args.analytics_file,args.version,args.side,args.regime,args.date_from,args.date_to,args.min_sample)
        if args.output: write_artifacts(result,args.output)
        if args.manifest:
            tmp=args.manifest+'.tmp'; os.makedirs(os.path.dirname(os.path.abspath(args.manifest)),exist_ok=True)
            with open(tmp,'w',encoding='utf-8') as f:
                for row in result['manifest']: f.write(json.dumps(row,sort_keys=True,separators=(',',':'))+'\n')
            os.replace(tmp,args.manifest)
        print(json.dumps(result,indent=2,sort_keys=True) if args.json else format_text(result,args.explain))
        return 2 if args.strict and not result['readiness']['dataset_ready_for_ml'] else 0
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr); return 1

if __name__=='__main__': raise SystemExit(main())
