#!/usr/bin/env python3
"""Append-only historical persistence for trades, decisions and snapshots."""
import json
import logging
import os
from datetime import datetime, timezone


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


def _bot_version():
    path = os.path.join(PROJECT_DIR, 'VERSION')
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip() or None
    except Exception:
        return None


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
            'market_regime': market_regime,
            'strategy_version': strategy_version or os.environ.get('STRATEGY_VERSION') or 'current',
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
        self._append(self.trades_file, record)
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
        self._append(self.trades_file, record)
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
            'market_regime': market_regime,
            'btc_context': btc_context or {},
            'details': details or {},
        }
        self._append(self.decisions_file, record)
        return record

    def record_snapshot(self, market=None, capital=None, exposure=None, positions=None,
                        max_positions=None, timestamp=None, details=None):
        record = {
            'event_type': 'MARKET_SNAPSHOT',
            'timestamp': _iso(timestamp),
            'market': market or {},
            'capital': capital or {},
            'exposure': exposure or {},
            'positions': positions or {},
            'max_positions': max_positions or {},
            'details': details or {},
        }
        self._append(self.snapshots_file, record)
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
