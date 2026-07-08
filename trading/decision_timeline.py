#!/usr/bin/env python3
"""Append-only decision timeline for operational observability."""
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import version_history

TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TRADING_DIR)
DEFAULT_TIMELINE_FILE = os.path.join(PROJECT_DIR, 'data', 'history', 'timeline.jsonl')
MAX_TIMELINE_BYTES = 5 * 1024 * 1024
KEEP_RECENT_BYTES = 4 * 1024 * 1024

LEVELS = {'INFO', 'WARNING', 'ERROR', 'CRITICAL'}
CATEGORIES = {
    'SYSTEM', 'MARKET', 'SIGNAL', 'FILTER', 'SIZING', 'RISK', 'REBALANCE',
    'ORDER', 'PROTECTION', 'GUARDIAN', 'CAPITAL', 'BLACKLIST', 'ANALYTICS',
}

CATEGORY_LABELS_ES = {
    'SYSTEM': 'SISTEMA',
    'MARKET': 'MERCADO',
    'SIGNAL': 'SEÑALES',
    'FILTER': 'FILTROS',
    'SIZING': 'SIZING',
    'RISK': 'RIESGO',
    'REBALANCE': 'REBALANCEO',
    'ORDER': 'ÓRDENES',
    'PROTECTION': 'PROTECCIÓN',
    'GUARDIAN': 'GUARDIAN',
    'CAPITAL': 'CAPITAL',
    'BLACKLIST': 'BLACKLIST',
    'ANALYTICS': 'ANALÍTICAS',
    'POSITION': 'POSICIONES',
}
SENSITIVE_MARKERS = ('key', 'secret', 'signature', 'token', 'api_key', 'api_secret')


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _safe_str(value, limit=500):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + '...'


def _sanitize(value, depth=0):
    if depth > 5:
        return '<max_depth>'
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            key_s = str(key)
            if any(marker in key_s.lower() for marker in SENSITIVE_MARKERS):
                safe[key_s] = '<redacted>'
            else:
                safe[key_s] = _sanitize(item, depth + 1)
        return safe
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth + 1) for item in list(value)[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return _safe_str(value)


def _normalise_level(level):
    value = str(level or 'INFO').upper()
    return value if value in LEVELS else 'INFO'


def _normalise_category(category):
    value = str(category or 'SYSTEM').upper()
    return value if value in CATEGORIES else 'SYSTEM'


def _rotate_if_needed(path=DEFAULT_TIMELINE_FILE, max_bytes=MAX_TIMELINE_BYTES, keep_bytes=KEEP_RECENT_BYTES):
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= max_bytes:
            return
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - keep_bytes))
            data = f.read()
        first_newline = data.find(b'\n')
        if first_newline >= 0:
            data = data[first_newline + 1:]
        with open(path, 'wb') as f:
            f.write(data)
    except Exception as exc:
        logging.warning('decision timeline rotation failed: %s', exc)


def record_event(
    event,
    message,
    level='INFO',
    category='SYSTEM',
    symbol=None,
    direction=None,
    details=None,
    cycle_id=None,
    related_trade_id=None,
    timestamp=None,
    path=DEFAULT_TIMELINE_FILE,
):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate_if_needed(path)
        record = {
            'event_id': uuid.uuid4().hex,
            'timestamp': timestamp or _now_iso(),
            'cycle_id': cycle_id,
            'level': _normalise_level(level),
            'category': _normalise_category(category),
            'event': _safe_str(event, 120),
            'symbol': symbol,
            'direction': str(direction).upper() if direction else None,
            'message': _safe_str(message, 1000),
            'details': _sanitize(details or {}),
            'related_trade_id': related_trade_id,
        }
        version_history.attach_version_metadata(record)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        return record
    except Exception as exc:
        logging.warning('decision timeline write failed: %s', exc)
        return None


def record_cycle_start(cycle_id=None, details=None, **kwargs):
    return record_event('cycle_start', 'Bot cycle started', 'INFO', 'SYSTEM', cycle_id=cycle_id, details=details, **kwargs)


def record_cycle_end(cycle_id=None, message='Bot cycle finished', details=None, **kwargs):
    return record_event('cycle_end', message, 'INFO', 'SYSTEM', cycle_id=cycle_id, details=details, **kwargs)


def record_signal_evaluated(symbol, direction, message='Signal evaluated', details=None, **kwargs):
    return record_event('signal_evaluated', message, 'INFO', 'SIGNAL', symbol=symbol, direction=direction, details=details, **kwargs)


def record_signal_rejected(symbol, direction, reason, details=None, **kwargs):
    return record_event('signal_rejected', f'{symbol} rejected: {reason}', 'INFO', 'SIGNAL', symbol=symbol, direction=direction, details=details, **kwargs)


def record_sizing_decision(wallet, accepted, requested=None, maximum=None, details=None, **kwargs):
    level = 'INFO' if accepted else 'WARNING'
    event = 'sizing_accepted' if accepted else 'sizing_rejected'
    msg = f'{wallet} {"allowed" if accepted else "rejected"}'
    if requested is not None:
        msg += f': requested {float(requested):.2f} USDT'
    if maximum is not None:
        msg += f' / max {float(maximum):.2f} USDT'
    return record_event(event, msg, level, 'SIZING', details=details, **kwargs)


def record_rebalance_event(event, message, level='INFO', details=None, **kwargs):
    return record_event(event, message, level, 'REBALANCE', details=details, **kwargs)


def record_order_event(event, symbol, direction, message, level='INFO', details=None, related_trade_id=None, **kwargs):
    return record_event(event, message, level, 'ORDER', symbol=symbol, direction=direction, details=details, related_trade_id=related_trade_id, **kwargs)


def record_protection_event(event, symbol, direction, message, level='INFO', details=None, related_trade_id=None, **kwargs):
    return record_event(event, message, level, 'PROTECTION', symbol=symbol, direction=direction, details=details, related_trade_id=related_trade_id, **kwargs)


def record_guardian_event(event, symbol=None, direction=None, message='Guardian event', level='INFO', details=None, related_trade_id=None, **kwargs):
    return record_event(event, message, level, 'GUARDIAN', symbol=symbol, direction=direction, details=details, related_trade_id=related_trade_id, **kwargs)


def read_recent_events(limit=20, category=None, symbol=None, path=DEFAULT_TIMELINE_FILE):
    events = []
    category_filter = str(category).upper() if category else None
    symbol_filter = str(symbol).upper() if symbol else None
    if not os.path.exists(path):
        return events
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if category_filter and str(event.get('category', '')).upper() != category_filter:
                    continue
                if symbol_filter and str(event.get('symbol', '')).upper() != symbol_filter:
                    continue
                events.append(event)
    except Exception as exc:
        logging.warning('decision timeline read failed: %s', exc)
        return []
    events.sort(key=lambda item: item.get('timestamp') or '')
    return list(reversed(events[-max(int(limit or 20), 1):]))


def compact_event_for_telegram(event):
    if not isinstance(event, dict):
        return ''
    ts = str(event.get('timestamp') or '')
    time_part = ts[11:16] if len(ts) >= 16 else '--:--'
    category = str(event.get('category') or 'SYSTEM').upper()
    category_label = CATEGORY_LABELS_ES.get(category, category)
    level = str(event.get('level') or 'INFO').upper()
    icon = '\U0001F6A8' if level == 'CRITICAL' else '\u274C' if level == 'ERROR' else '\u26A0\uFE0F' if level == 'WARNING' else '\u2705'
    if event.get('event', '').endswith('rejected') or 'reject' in str(event.get('event', '')).lower():
        icon = '\U0001F6AB'
    symbol = f' {event.get("symbol")}' if event.get('symbol') else ''
    direction = f' {event.get("direction")}' if event.get('direction') else ''
    message = event.get('message') or event.get('event') or ''
    return f'{time_part} | {category_label}\n{icon}{symbol}{direction} {message}'.strip()
