#!/usr/bin/env python3
"""Append-only historical persistence for trades, decisions and snapshots."""
import json
import logging
import os
from datetime import datetime, timezone

import decision_timeline
import version_history


TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TRADING_DIR)
DEFAULT_HISTORY_DIR = os.path.join(PROJECT_DIR, 'data', 'history')
DEFAULT_TRADES_FILE = os.path.join(DEFAULT_HISTORY_DIR, 'trades.jsonl')
DEFAULT_DECISIONS_FILE = os.path.join(DEFAULT_HISTORY_DIR, 'decisions.jsonl')
DEFAULT_SNAPSHOTS_FILE = os.path.join(DEFAULT_HISTORY_DIR, 'snapshots.jsonl')


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _iso(value=None):
    if value is None:
        return _now_iso()
    if isinstance(value, str):
        return value
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    except Exception:
        return None


def _metadata_flags(details):
    if not isinstance(details, dict):
        return []
    metadata = details.get('metadata') if isinstance(details.get('metadata'), dict) else {}
    text = ' '.join(str(details.get(key) or '') for key in ('source', 'module', 'reason', 'description')).lower()
    text = f'{text} ' + ' '.join(str(value or '') for value in metadata.values()).lower()
    flags = []
    for token in ('backfill', 'backfilled', 'imported', 'recovered', 'synthetic'):
        if token in text:
            flags.append(token)
    for key in ('backfilled', 'imported', 'recovered', 'synthetic'):
        if details.get(key) is True or metadata.get(key) is True:
            flags.append(key)
    return sorted(set(flags))


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _duration_seconds(opened_at, closed_at):
    if not opened_at or not closed_at:
        return None
    try:
        start = datetime.fromisoformat(str(opened_at).replace('Z', '+00:00'))
        end = datetime.fromisoformat(str(closed_at).replace('Z', '+00:00'))
        return round((end - start).total_seconds(), 4)
    except Exception:
        return None


def _pnl_pct(side, entry_price, exit_price):
    entry = _float_or_none(entry_price)
    exit_ = _float_or_none(exit_price)
    if not entry or exit_ is None:
        return None
    if str(side or '').upper() == 'SHORT':
        return round((entry - exit_) / entry * 100, 4)
    return round((exit_ - entry) / entry * 100, 4)


def _result(pnl_usdt):
    pnl = _float_or_none(pnl_usdt)
    if pnl is None:
        return None
    if pnl > 0:
        return 'WIN'
    if pnl < 0:
        return 'LOSS'
    return 'BREAKEVEN'


def _normalise_exit_reason(reason):
    value = str(reason or '').upper()
    mapping = {
        'PARTIAL_TP': 'PARTIAL',
        'STALE_EXIT': 'STALE',
        'CLOSED_TP': 'TP',
        'CLOSED_SL': 'SL',
        'CLOSED_MANUAL': 'MANUAL',
    }
    return mapping.get(value, value or None)


def normalise_regime(value):
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


def _bot_version():
    return version_history.current_version()


class HistoryStore:
    def __init__(self, trades_file=DEFAULT_TRADES_FILE, decisions_file=DEFAULT_DECISIONS_FILE,
                 snapshots_file=DEFAULT_SNAPSHOTS_FILE):
        self.trades_file = trades_file
        self.decisions_file = decisions_file
        self.snapshots_file = snapshots_file

    def record_trade_open(
        self,
        trade_id,
        symbol,
        side,
        opened_at=None,
        entry_price=None,
        quantity=None,
        capital_used=None,
        wallet=None,
        score=None,
        atr=None,
        atr_pct=None,
        rsi=None,
        volatility=None,
        btc_context=None,
        market_regime=None,
        strategy_version=None,
        bot_version=None,
        fees=None,
        extra=None,
    ):
        record = {
            'event_type': 'TRADE_OPEN',
            'recorded_at': _now_iso(),
            'trade_id': trade_id,
            'symbol': symbol,
            'side': str(side or '').upper() or None,
            'opened_at': _iso(opened_at),
            'closed_at': None,
            'duration_seconds': None,
            'duration_minutes': None,
            'entry_price': _float_or_none(entry_price),
            'quantity': _float_or_none(quantity),
            'capital_used': _float_or_none(capital_used),
            'wallet': wallet,
            'score': _float_or_none(score),
            'atr': _float_or_none(atr),
            'atr_pct': _float_or_none(atr_pct),
            'rsi': _float_or_none(rsi),
            'volatility': _float_or_none(volatility),
            'btc_context': btc_context or {},
            'regime': normalise_regime(market_regime or (btc_context or {}).get('trend') or (btc_context or {}).get('regime')),
            'market_regime': market_regime,
            'strategy_version': strategy_version or version_history.STRATEGY_VERSION,
            'bot_version': bot_version or _bot_version(),
            'exit_price': None,
            'exit_reason': None,
            'pnl_pct': None,
            'pnl_usdt': None,
            'fees': _float_or_none(fees),
            'status': 'OPEN',
            'result': None,
        }
        if isinstance(extra, dict):
            record['extra'] = extra
        version_history.attach_version_metadata(record)
        self._append(self.trades_file, record)
        decision_timeline.record_event(
            'history_trade_open',
            f'{symbol} {record["side"]} trade open stored',
            category='ANALYTICS',
            symbol=symbol,
            direction=record['side'],
            related_trade_id=trade_id,
            details={'wallet': wallet, 'capital_used': record.get('capital_used')},
        )
        return record

    def record_trade_close(
        self,
        trade_id,
        closed_at=None,
        exit_price=None,
        exit_reason=None,
        pnl_usdt=None,
        entry_price=None,
        opened_at=None,
        side=None,
        symbol=None,
        fees=None,
        extra=None,
    ):
        closed_iso = _iso(closed_at)
        opened_iso = _iso(opened_at) if opened_at is not None else None
        duration = _duration_seconds(opened_iso, closed_iso)
        record = {
            'event_type': 'TRADE_CLOSE',
            'recorded_at': _now_iso(),
            'trade_id': trade_id,
            'symbol': symbol,
            'side': str(side or '').upper() or None,
            'opened_at': opened_iso,
            'closed_at': closed_iso,
            'duration_seconds': duration,
            'duration_minutes': round(duration / 60, 4) if duration is not None else None,
            'entry_price': _float_or_none(entry_price),
            'exit_price': _float_or_none(exit_price),
            'exit_reason': _normalise_exit_reason(exit_reason),
            'pnl_pct': _pnl_pct(side, entry_price, exit_price),
            'pnl_usdt': _float_or_none(pnl_usdt),
            'fees': _float_or_none(fees),
            'status': 'CLOSED',
            'result': _result(pnl_usdt),
        }
        if isinstance(extra, dict):
            record['extra'] = extra
        version_history.attach_version_metadata(record)
        self._append(self.trades_file, record)
        decision_timeline.record_event(
            'history_trade_close',
            f'{symbol or trade_id} trade close stored: {record["exit_reason"]}',
            category='ANALYTICS',
            symbol=symbol,
            direction=record['side'],
            related_trade_id=trade_id,
            details={'pnl_usdt': record.get('pnl_usdt'), 'result': record.get('result')},
        )
        return record

    def record_decision(self, decision, symbol=None, side=None, reason=None, steps=None,
                        score=None, market_regime=None, btc_context=None, timestamp=None,
                        details=None):
        record = {
            'event_type': 'DECISION',
            'timestamp': _iso(timestamp),
            'decision': decision,
            'symbol': symbol,
            'side': str(side or '').upper() or None,
            'reason': reason,
            'steps': steps or [],
            'score': _float_or_none(score),
            'regime': normalise_regime(market_regime or (btc_context or {}).get('trend') or (btc_context or {}).get('regime')),
            'market_regime': market_regime,
            'btc_context': btc_context or {},
            'details': details or {},
        }
        version_history.attach_version_metadata(record)
        self._append(self.decisions_file, record)
        decision_timeline.record_event(
            'history_decision',
            f'Decision stored: {decision}',
            category='ANALYTICS',
            symbol=symbol,
            direction=record['side'],
            details={'reason': reason, 'score': record.get('score')},
        )
        return record

    def record_snapshot(self, market=None, capital=None, exposure=None, positions=None,
                        max_positions=None, timestamp=None, details=None):
        market_payload = market or {}
        details_payload = details or {}
        source_timestamp = _iso(timestamp) if timestamp is not None else None
        backfill_flags = _metadata_flags(details_payload)
        now_iso = _now_iso()
        event_timestamp = source_timestamp if backfill_flags else now_iso
        record = {
            'event_type': 'MARKET_SNAPSHOT',
            'timestamp': event_timestamp,
            'recorded_at': now_iso,
            'generated_at': now_iso,
            'regime': normalise_regime(market_payload.get('regime') or market_payload.get('trend') or market_payload.get('btc_trend')),
            'market': market_payload,
            'capital': capital or {},
            'exposure': exposure or {},
            'positions': positions or {},
            'max_positions': max_positions or {},
            'details': details_payload,
        }
        if source_timestamp and source_timestamp != record['timestamp']:
            record['source_timestamp'] = source_timestamp
        if isinstance(details_payload, dict):
            if details_payload.get('source'):
                record['source'] = details_payload.get('source')
            if details_payload.get('module'):
                record['module'] = details_payload.get('module')
            if isinstance(details_payload.get('metadata'), dict):
                record['metadata'] = details_payload.get('metadata')
        version_history.attach_version_metadata(record)
        self._append(self.snapshots_file, record)
        decision_timeline.record_event(
            'history_snapshot',
            'Market snapshot stored',
            category='ANALYTICS',
            details={'market': record.get('market'), 'max_positions': record.get('max_positions')},
        )
        return record

    def get_trade(self, trade_id):
        merged = None
        for record in self._iter_jsonl(self.trades_file):
            if record.get('trade_id') != trade_id:
                continue
            if merged is None:
                merged = {}
            merged.update({k: v for k, v in record.items() if v is not None})
        return merged

    def _append(self, path, record):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')

    def _iter_jsonl(self, path):
        if not os.path.exists(path):
            return
        with open(path, encoding='utf-8') as f:
            for lineno, line in enumerate(f, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    logging.warning('history JSONL invalid path=%s line=%s error=%s', path, lineno, exc)
                    continue
                if isinstance(data, dict):
                    yield data


DEFAULT_STORE = HistoryStore()


def record_trade_open(**kwargs):
    return DEFAULT_STORE.record_trade_open(**kwargs)


def record_trade_close(**kwargs):
    return DEFAULT_STORE.record_trade_close(**kwargs)


def record_decision(**kwargs):
    return DEFAULT_STORE.record_decision(**kwargs)


def record_snapshot(**kwargs):
    return DEFAULT_STORE.record_snapshot(**kwargs)


def get_trade(trade_id):
    return DEFAULT_STORE.get_trade(trade_id)
