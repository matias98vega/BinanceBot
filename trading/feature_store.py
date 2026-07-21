#!/usr/bin/env python3
"""Passive append-only feature store for future learning workflows."""
import json
import logging
import math
import os
from datetime import datetime, timezone

import history
import version_history


DEFAULT_FEATURES_FILE = os.path.join(history.DEFAULT_HISTORY_DIR, 'features.jsonl')
SENSITIVE_MARKERS = ('key', 'secret', 'token', 'signature', 'header', 'cookie', 'authorization')


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except Exception:
            return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def _float_or_none(value):
    try:
        if value is None or value == '':
            return None
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _safe_scalar(value):
    if isinstance(value, float):
        return _float_or_none(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _sanitize(value, depth=0):
    if depth > 5:
        return None
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_s = str(key)
            if any(marker in key_s.lower() for marker in SENSITIVE_MARKERS):
                continue
            clean[key_s] = _sanitize(item, depth + 1)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, depth + 1) for item in list(value)[:100]]
    return _safe_scalar(value)


def _pct_distance(price, level):
    price_f = _float_or_none(price)
    level_f = _float_or_none(level)
    if not price_f or level_f is None:
        return None
    return round((price_f - level_f) / price_f * 100, 8)


def _risk_reward(entry_price, sl, tp, side):
    entry = _float_or_none(entry_price)
    sl_f = _float_or_none(sl)
    tp_f = _float_or_none(tp)
    if not entry or sl_f is None or tp_f is None:
        return None
    side = str(side or '').upper()
    if side == 'SHORT':
        risk = abs(sl_f - entry)
        reward = abs(entry - tp_f)
    else:
        risk = abs(entry - sl_f)
        reward = abs(tp_f - entry)
    if risk == 0:
        return None
    return round(reward / risk, 8)


def _btc_value(ctx, *keys):
    if not isinstance(ctx, dict):
        return None
    for key in keys:
        if key in ctx:
            return ctx.get(key)
    return None


def _normalise_regime(value):
    value = str(value or '').strip().lower()
    mapping = {
        'bullish': 'bull',
        'bull': 'bull',
        'bearish': 'bear',
        'bear': 'bear',
        'sideways': 'sideways',
        'chop': 'sideways',
        'range': 'sideways',
        'neutral': 'neutral',
        'neutro': 'neutral',
    }
    return mapping.get(value, 'unknown')


def _record_from_kwargs(kwargs):
    timestamp = kwargs.get('timestamp') or kwargs.get('entry_time') or kwargs.get('opened_at') or _now_iso()
    dt = _parse_dt(timestamp)
    side = str(kwargs.get('side') or kwargs.get('direction') or '').upper() or None
    btc_context = kwargs.get('btc_context') if isinstance(kwargs.get('btc_context'), dict) else {}
    reasons = kwargs.get('reasons')
    if reasons is None:
        reasons = kwargs.get('reject_reasons')
    if isinstance(reasons, str):
        reasons_list = [reasons]
    elif isinstance(reasons, (list, tuple, set)):
        reasons_list = list(reasons)
    else:
        reasons_list = []

    entry_price = kwargs.get('entry_price')
    sl = kwargs.get('sl')
    tp = kwargs.get('tp')
    ema20 = kwargs.get('ema20')
    ema50 = kwargs.get('ema50')
    ema200 = kwargs.get('ema200')
    regime = _normalise_regime(kwargs.get('regime') or kwargs.get('market_regime') or _btc_value(btc_context, 'trend', 'regime'))

    passive_context = kwargs.get('passive_context') if isinstance(kwargs.get('passive_context'), dict) else None
    feature_schema_version = passive_context.get('feature_schema_version') if passive_context else 1
    record = {
        'schema_version': feature_schema_version,
        'feature_schema_version': feature_schema_version,
        'feature_capture_version': passive_context.get('feature_capture_version') if passive_context else 'legacy-postfill-v1',
        'recorded_at': _now_iso(),
        'identification': {
            'trade_id': kwargs.get('trade_id'),
            'timestamp': dt.replace(microsecond=0).isoformat().replace('+00:00', 'Z') if dt else None,
            'symbol': kwargs.get('symbol'),
            'direction': side,
            'wallet': kwargs.get('wallet'),
            'bot_version': kwargs.get('bot_version'),
        },
        'market': {
            'regime': regime,
            'btc_regime': kwargs.get('market_regime') or _btc_value(btc_context, 'trend', 'regime'),
            'btc_price': _float_or_none(_btc_value(btc_context, 'btc_price', 'price')),
            'btc_change_4h': _float_or_none(_btc_value(btc_context, 'btc_change_4h', 'change_4h', 'btc_change_4h')),
            'btc_change_daily': _float_or_none(_btc_value(btc_context, 'btc_change_daily', 'change_24h', 'btc_change_24h')),
            'volatility': _float_or_none(kwargs.get('volatility')),
            'atr': _float_or_none(kwargs.get('atr')),
            'adx': _float_or_none(kwargs.get('adx')),
            'volume_ratio': _float_or_none(kwargs.get('volume_ratio')),
            'spread': _float_or_none(kwargs.get('spread')),
            'hour_utc': dt.hour if dt else None,
            'weekday': dt.weekday() if dt else None,
        },
        'symbol_indicators': {
            'entry_price': _float_or_none(entry_price),
            'ema20': _float_or_none(ema20),
            'ema50': _float_or_none(ema50),
            'ema200': _float_or_none(ema200),
            'rsi': _float_or_none(kwargs.get('rsi')),
            'macd': _float_or_none(kwargs.get('macd')),
            'macd_hist': _float_or_none(kwargs.get('macd_hist')),
            'atr': _float_or_none(kwargs.get('atr')),
            'volume': _float_or_none(kwargs.get('volume')),
            'distance_to_ema20_pct': _pct_distance(entry_price, ema20),
            'distance_to_ema50_pct': _pct_distance(entry_price, ema50),
            'distance_to_ema200_pct': _pct_distance(entry_price, ema200),
            'sl_pct': _float_or_none(kwargs.get('sl_pct')),
            'tp_pct': _float_or_none(kwargs.get('tp_pct')),
            'risk_reward': _float_or_none(kwargs.get('risk_reward')) or _risk_reward(entry_price, sl, tp, side),
        },
        'scoring': {
            'score_total': _float_or_none(kwargs.get('score')),
            'score_min_required': _float_or_none(kwargs.get('score_min_required')),
            'reasons': _sanitize(reasons_list),
            'reason_count': len(reasons_list),
        },
        'capital': {
            'capital_spot': _float_or_none(kwargs.get('capital_spot')),
            'capital_futures': _float_or_none(kwargs.get('capital_futures')),
            'capital_total': _float_or_none(kwargs.get('capital_total')),
            'exposure_pct': _float_or_none(kwargs.get('exposure_pct')),
            'position_calculated': _float_or_none(kwargs.get('position_calculated')),
            'position_final': _float_or_none(kwargs.get('position_final') or kwargs.get('capital_used')),
            'quantity': _float_or_none(kwargs.get('quantity')),
            'leverage': _float_or_none(kwargs.get('leverage')),
        },
        'bot_state': {
            'open_longs': kwargs.get('open_longs'),
            'open_shorts': kwargs.get('open_shorts'),
            'active_cooldowns': kwargs.get('active_cooldowns'),
            'guardian_active': kwargs.get('guardian_active'),
            'directional_mode': kwargs.get('directional_mode'),
            'current_regime': _normalise_regime(kwargs.get('current_regime') or kwargs.get('market_regime')),
        },
        'decision_context': {
            'open_reason': kwargs.get('open_reason') or kwargs.get('reject_reason'),
            'snapshot_id': kwargs.get('snapshot_id'),
            'decision_id': kwargs.get('decision_id'),
            'timeline_id': kwargs.get('timeline_id'),
        },
        'extra': _sanitize(kwargs.get('extra') or {}),
        'preentry_context': _sanitize(passive_context),
    }
    version_history.attach_version_metadata(record)
    record['identification']['bot_version'] = record.get('bot_version')
    record['identification']['strategy_version'] = record.get('strategy_version')
    record['identification']['data_schema_version'] = record.get('data_schema_version')
    return record


def record_trade_features(features_file=DEFAULT_FEATURES_FILE, **kwargs):
    try:
        record = _record_from_kwargs(kwargs)
        os.makedirs(os.path.dirname(features_file), exist_ok=True)
        with open(features_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        return record
    except Exception as exc:
        logging.warning('feature_store write failed: %s', exc)
        return None
