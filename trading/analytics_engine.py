#!/usr/bin/env python3
"""Passive analytics index built from historical JSONL files."""
import json
import logging
import os
import sys
from datetime import datetime, timezone

import capital_accounting
import feature_store
import history
import version_history


DEFAULT_STATS_FILE = os.path.join(history.DEFAULT_HISTORY_DIR, 'stats.json')
STATS_SCHEMA_VERSION = 4

EXIT_REASONS = ('TP', 'SL', 'TRAILING', 'PARTIAL', 'STALE', 'RECOVERY', 'EMERGENCY', 'MANUAL', 'UNKNOWN')
DIRECTIONS = ('LONG', 'SHORT', 'UNKNOWN')
REGIMES = ('bull', 'bear', 'sideways', 'neutral', 'unknown')
LEGACY_BOT_VERSION = 'legacy/unknown'


def _safe_accounting(default, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        logging.warning('analytics_engine capital accounting failed: %s', exc)
        return default


def _test_mode_default_stats_write_blocked(stats_file):
    if os.path.abspath(stats_file) != os.path.abspath(DEFAULT_STATS_FILE):
        return False
    env_blocked = str(os.environ.get('BINANCEBOT_TEST_MODE') or '').lower() in {'1', 'true', 'yes'}
    argv = ' '.join(str(arg).lower() for arg in sys.argv)
    unittest_blocked = 'unittest' in argv or 'discover' in argv
    return env_blocked or unittest_blocked


def _empty_bucket():
    return {
        'trades': 0,
        'closed': 0,
        'open': 0,
        'win': 0,
        'loss': 0,
        'breakeven': 0,
        'win_rate': 0.0,
        'profit_factor': None,
        'expectancy': 0.0,
        'pnl_total': 0.0,
        'pnl_average': 0.0,
        'gross_profit': 0.0,
        'gross_loss': 0.0,
        'duration_average_minutes': None,
        '_duration_sum': 0.0,
        '_duration_count': 0,
    }


def _empty_stats():
    return {
        'schema_version': STATS_SCHEMA_VERSION,
        'source': {
            'trades_file': history.DEFAULT_TRADES_FILE,
            'decisions_file': history.DEFAULT_DECISIONS_FILE,
            'snapshots_file': history.DEFAULT_SNAPSHOTS_FILE,
            'features_file': feature_store.DEFAULT_FEATURES_FILE,
            'trade_events': 0,
            'decision_events': 0,
            'snapshot_events': 0,
            'feature_events': 0,
            'invalid_trade_lines': 0,
            'invalid_decision_lines': 0,
            'invalid_snapshot_lines': 0,
            'invalid_feature_lines': 0,
        },
        'general': {
            **_empty_bucket(),
            'total_trades': 0,
            'open_trades': 0,
            'closed_trades': 0,
            'best_trade': None,
            'worst_trade': None,
            'best_trade_pct': None,
            'worst_trade_pct': None,
            'max_drawdown_usdt': 0.0,
            'max_drawdown_pending': False,
            'equity_current': 0.0,
            'equity_peak': 0.0,
            'pnl_daily': {},
            'pnl_weekly': {},
            'pnl_monthly': {},
        },
        'by_symbol': {},
        'symbol_ranking': [],
        'by_direction': {key: _empty_bucket() for key in DIRECTIONS},
        'by_regime': {key: _empty_bucket() for key in REGIMES},
        'by_exit_reason': {key: _empty_bucket() for key in EXIT_REASONS},
        'by_bot_version': {version_history.current_version(): _empty_bucket()},
        'time': {
            'hour': {},
            'day': {},
            'week': {},
            'month': {},
        },
        'decisions': {
            'total': 0,
            'by_decision': {},
            'by_symbol': {},
            'by_reason': {},
        },
        'snapshots': {
            'total': 0,
        },
        'history': {
            'trades_registered': 0,
            'decisions_registered': 0,
            'snapshots_registered': 0,
            'first_record': None,
            'last_record': None,
        },
        'trade_index': {},
        'processed_closed_trade_ids': [],
    }


def _float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def _date_keys(value):
    dt = _parse_dt(value)
    if not dt:
        return None
    iso = dt.isocalendar()
    return {
        'hour': f'{dt.hour:02d}',
        'day': dt.date().isoformat(),
        'week': f'{iso.year}-W{iso.week:02d}',
        'month': f'{dt.year:04d}-{dt.month:02d}',
    }


def _normalise_direction(value):
    value = str(value or '').upper()
    return value if value in ('LONG', 'SHORT') else 'UNKNOWN'


def _normalise_exit_reason(value):
    value = str(value or '').upper()
    mapping = {
        'PARTIAL_TP': 'PARTIAL',
        'STALE_EXIT': 'STALE',
        'CLOSED_TP': 'TP',
        'CLOSED_SL': 'SL',
        'CLOSED_MANUAL': 'MANUAL',
    }
    value = mapping.get(value, value)
    return value if value in EXIT_REASONS else 'UNKNOWN'


def _normalise_regime(value):
    value = str(value or '').strip().lower()
    mapping = {
        'bull': 'bull',
        'bullish': 'bull',
        'bear': 'bear',
        'bearish': 'bear',
        'sideways': 'sideways',
        'chop': 'sideways',
        'range': 'sideways',
        'neutral': 'neutral',
        'neutro': 'neutral',
    }
    return mapping.get(value, 'unknown')


def _trade_regime_value(trade):
    if not isinstance(trade, dict):
        return 'unknown'
    for key in ('regime', 'market_regime', 'btc_regime', 'regime_at_entry', 'context_regime'):
        value = trade.get(key)
        if value not in (None, ''):
            return _normalise_regime(value)
    context = trade.get('context')
    if isinstance(context, dict) and context.get('regime') not in (None, ''):
        return _normalise_regime(context.get('regime'))
    btc_context = trade.get('btc_context')
    if isinstance(btc_context, dict):
        for key in ('regime', 'trend'):
            if btc_context.get(key) not in (None, ''):
                return _normalise_regime(btc_context.get(key))
    return 'unknown'


def _base_trade_id(trade_id):
    value = str(trade_id or '')
    return value.split(':', 1)[0] if value else value


def _nested_value(record, *path):
    current = record
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _feature_trade_id(record):
    return record.get('trade_id') or _nested_value(record, 'identification', 'trade_id')


def _feature_regime_value(record):
    if not isinstance(record, dict):
        return 'unknown'
    candidates = (
        record.get('regime'),
        record.get('market_regime'),
        record.get('btc_regime'),
        _nested_value(record, 'market', 'regime'),
        _nested_value(record, 'market', 'btc_regime'),
        _nested_value(record, 'bot_state', 'current_regime'),
        _nested_value(record, 'btc_context', 'regime'),
        _nested_value(record, 'btc_context', 'trend'),
    )
    for value in candidates:
        regime = _normalise_regime(value)
        if regime != 'unknown':
            return regime
    return 'unknown'


def _feature_opened_at(record):
    return (
        record.get('opened_at')
        or record.get('entry_time')
        or record.get('timestamp')
        or _nested_value(record, 'identification', 'timestamp')
    )


def _feature_index(records):
    index = {}
    for record in records:
        trade_id = _feature_trade_id(record)
        if not trade_id:
            continue
        regime = _feature_regime_value(record)
        if regime == 'unknown':
            continue
        payload = {
            'trade_id': trade_id,
            'regime': regime,
            'market_regime': _nested_value(record, 'market', 'btc_regime') or record.get('market_regime') or regime,
            'opened_at': _feature_opened_at(record),
        }
        index.setdefault(trade_id, payload)
        index.setdefault(_base_trade_id(trade_id), payload)
    return index


def _enrich_trade_regime(trade, feature_lookup=None):
    if not isinstance(trade, dict):
        return trade
    if _trade_regime_value(trade) != 'unknown':
        return trade
    feature_lookup = feature_lookup or {}
    trade_id = trade.get('trade_id')
    feature = feature_lookup.get(trade_id) or feature_lookup.get(_base_trade_id(trade_id))
    if not feature:
        return trade
    enriched = dict(trade)
    enriched.setdefault('regime', feature.get('regime'))
    enriched.setdefault('market_regime', feature.get('market_regime') or feature.get('regime'))
    if not enriched.get('opened_at') and feature.get('opened_at'):
        enriched['opened_at'] = feature.get('opened_at')
    return enriched


def _result(pnl):
    pnl = _float(pnl)
    if pnl > 0:
        return 'WIN'
    if pnl < 0:
        return 'LOSS'
    return 'BREAKEVEN'


def _iter_jsonl(path):
    invalid = 0
    records = []
    if not os.path.exists(path):
        return records, invalid
    with open(path, encoding='utf-8') as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                invalid += 1
                logging.warning('analytics_engine invalid JSONL path=%s', path)
                continue
            if isinstance(data, dict):
                records.append(data)
    return records, invalid


def _merge_trade_events(records):
    trades = {}
    for record in records:
        trade_id = record.get('trade_id')
        if not trade_id:
            continue
        current = trades.setdefault(trade_id, {})
        event_type = str(record.get('event_type') or '').upper()
        status = str(record.get('status') or '').upper()
        is_open_event = event_type == 'TRADE_OPEN' or status == 'OPEN'
        opening_version_known = current.get('_opening_bot_version_known') is True
        opening_version = current.get('bot_version')
        current.update({k: v for k, v in record.items() if v is not None and k != 'bot_version'})
        if is_open_event and not opening_version_known:
            current['bot_version'] = record.get('bot_version') or LEGACY_BOT_VERSION
            current['_opening_bot_version_known'] = True
        elif opening_version_known:
            current['bot_version'] = opening_version
        elif event_type != 'TRADE_CLOSE' and record.get('bot_version'):
            current['bot_version'] = record.get('bot_version')
            current['_opening_bot_version_known'] = True
    for trade_id, trade in trades.items():
        if trade.get('_opening_bot_version_known') is not True:
            base = trades.get(_base_trade_id(trade_id))
            if base and base is not trade and base.get('_opening_bot_version_known') is True:
                trade['bot_version'] = base.get('bot_version')
                for key in ('regime', 'market_regime', 'btc_regime', 'capital_used', 'capital_at_entry', 'notional'):
                    if trade.get(key) is None and base.get(key) is not None:
                        trade[key] = base.get(key)
        trade['bot_version'] = trade.get('bot_version') or LEGACY_BOT_VERSION
    for trade in trades.values():
        trade.pop('_opening_bot_version_known', None)
    return trades


def _bot_version(trade):
    return str(trade.get('bot_version') or '').strip() or LEGACY_BOT_VERSION


def _update_version_bounds(bucket, trade):
    operation_time = trade.get('opened_at') or trade.get('entry_time') or trade.get('closed_at') or trade.get('exit_time')
    if not operation_time:
        return
    first = bucket.get('first_trade')
    last = bucket.get('last_trade')
    parsed = _parse_dt(operation_time)
    if not first or (parsed and _parse_dt(first) and parsed < _parse_dt(first)):
        bucket['first_trade'] = operation_time
    if not last or (parsed and _parse_dt(last) and parsed > _parse_dt(last)):
        bucket['last_trade'] = operation_time


def _add_open(bucket):
    bucket['trades'] += 1
    bucket['open'] += 1


def _add_closed(bucket, trade):
    pnl = _float(trade.get('pnl_usdt'))
    duration = trade.get('duration_minutes')
    result = trade.get('result') or _result(pnl)

    bucket['trades'] += 1
    bucket['closed'] += 1
    bucket['pnl_total'] = round(bucket['pnl_total'] + pnl, 8)
    if pnl > 0:
        bucket['gross_profit'] = round(bucket['gross_profit'] + pnl, 8)
    elif pnl < 0:
        bucket['gross_loss'] = round(bucket['gross_loss'] + abs(pnl), 8)

    if result == 'WIN':
        bucket['win'] += 1
    elif result == 'LOSS':
        bucket['loss'] += 1
    else:
        bucket['breakeven'] += 1

    if duration is not None:
        bucket['_duration_sum'] += _float(duration)
        bucket['_duration_count'] += 1


def _finalise_bucket(bucket):
    closed = bucket.get('closed', 0)
    if closed:
        bucket['win_rate'] = round(bucket.get('win', 0) / closed * 100, 4)
        bucket['expectancy'] = round(bucket.get('pnl_total', 0.0) / closed, 8)
        bucket['pnl_average'] = round(bucket.get('pnl_total', 0.0) / closed, 8)
    else:
        bucket['win_rate'] = 0.0
        bucket['expectancy'] = 0.0
        bucket['pnl_average'] = 0.0

    gross_loss = bucket.get('gross_loss', 0.0)
    gross_profit = bucket.get('gross_profit', 0.0)
    bucket['profit_factor'] = None if gross_loss == 0 else round(gross_profit / gross_loss, 8)

    if bucket.get('_duration_count'):
        bucket['duration_average_minutes'] = round(bucket['_duration_sum'] / bucket['_duration_count'], 4)
    else:
        bucket['duration_average_minutes'] = None

    bucket.pop('_duration_sum', None)
    bucket.pop('_duration_count', None)
    return bucket


def _update_best_worst(stats, trade):
    pnl = _float(trade.get('pnl_usdt'))
    pct = trade.get('pnl_pct')
    summary = {
        'trade_id': trade.get('trade_id'),
        'symbol': trade.get('symbol'),
        'side': trade.get('side'),
        'pnl_usdt': pnl,
        'pnl_pct': pct,
        'closed_at': trade.get('closed_at'),
    }
    general = stats['general']
    if general['best_trade'] is None or pnl > _float(general['best_trade'].get('pnl_usdt')):
        general['best_trade'] = dict(summary)
    if general['worst_trade'] is None or pnl < _float(general['worst_trade'].get('pnl_usdt')):
        general['worst_trade'] = dict(summary)
    if pct is not None:
        pct = _float(pct)
        if general['best_trade_pct'] is None or pct > _float(general['best_trade_pct'].get('pnl_pct')):
            general['best_trade_pct'] = dict(summary)
        if general['worst_trade_pct'] is None or pct < _float(general['worst_trade_pct'].get('pnl_pct')):
            general['worst_trade_pct'] = dict(summary)


def _add_pnl_to_time(stats, trade):
    keys = _date_keys(trade.get('closed_at') or trade.get('opened_at'))
    if not keys:
        return
    pnl = _float(trade.get('pnl_usdt'))
    general = stats['general']
    general['pnl_daily'][keys['day']] = round(general['pnl_daily'].get(keys['day'], 0.0) + pnl, 8)
    general['pnl_weekly'][keys['week']] = round(general['pnl_weekly'].get(keys['week'], 0.0) + pnl, 8)
    general['pnl_monthly'][keys['month']] = round(general['pnl_monthly'].get(keys['month'], 0.0) + pnl, 8)
    for bucket_name, key in keys.items():
        bucket = stats['time'][bucket_name].setdefault(key, _empty_bucket())
        _add_closed(bucket, trade)


def _add_trade(stats, trade, mark_processed=True):
    status = str(trade.get('status') or '').upper()
    symbol = trade.get('symbol') or 'UNKNOWN'
    side = _normalise_direction(trade.get('side'))
    regime = _trade_regime_value(trade)
    trade_id = trade.get('trade_id')
    version_bucket = stats.setdefault('by_bot_version', {}).setdefault(_bot_version(trade), _empty_bucket())
    _update_version_bounds(version_bucket, trade)

    if trade_id:
        current = stats.setdefault('trade_index', {}).setdefault(trade_id, {})
        current.update({k: v for k, v in {
            'trade_id': trade_id,
            'symbol': symbol,
            'side': side,
            'regime': regime,
            'market_regime': trade.get('market_regime'),
            'opened_at': trade.get('opened_at'),
            'entry_price': trade.get('entry_price'),
            'status': status,
        }.items() if v is not None})

    if status == 'CLOSED':
        reason = _normalise_exit_reason(trade.get('exit_reason'))
        _add_closed(stats['general'], trade)
        _add_closed(stats['by_symbol'].setdefault(symbol, _empty_bucket()), trade)
        _add_closed(stats['by_direction'].setdefault(side, _empty_bucket()), trade)
        _add_closed(stats['by_regime'].setdefault(regime, _empty_bucket()), trade)
        _add_closed(stats['by_exit_reason'].setdefault(reason, _empty_bucket()), trade)
        _add_closed(version_bucket, trade)
        _add_pnl_to_time(stats, trade)
        _update_best_worst(stats, trade)
        if trade_id:
            stats['trade_index'][trade_id]['closed_at'] = trade.get('closed_at')
            stats['trade_index'][trade_id]['status'] = 'CLOSED'
        if mark_processed and trade_id:
            processed = set(stats.get('processed_closed_trade_ids') or [])
            processed.add(trade_id)
            stats['processed_closed_trade_ids'] = sorted(processed)
    else:
        _add_open(stats['general'])
        _add_open(stats['by_symbol'].setdefault(symbol, _empty_bucket()))
        _add_open(stats['by_direction'].setdefault(side, _empty_bucket()))
        _add_open(stats['by_regime'].setdefault(regime, _empty_bucket()))
        _add_open(version_bucket)


def _add_decision(stats, record):
    stats['decisions']['total'] += 1
    decision = str(record.get('decision') or 'UNKNOWN').upper()
    symbol = record.get('symbol') or 'UNKNOWN'
    reason = record.get('reason') or 'UNKNOWN'
    stats['decisions']['by_decision'][decision] = stats['decisions']['by_decision'].get(decision, 0) + 1
    stats['decisions']['by_symbol'][symbol] = stats['decisions']['by_symbol'].get(symbol, 0) + 1
    stats['decisions']['by_reason'][reason] = stats['decisions']['by_reason'].get(reason, 0) + 1


def _finalise_stats(stats):
    stats['general']['total_trades'] = stats['general']['trades']
    stats['general']['open_trades'] = stats['general']['open']
    stats['general']['closed_trades'] = stats['general']['closed']
    _finalise_bucket(stats['general'])
    stats.setdefault('by_bot_version', {}).setdefault(version_history.current_version(), _empty_bucket())
    for collection in ('by_symbol', 'by_direction', 'by_regime', 'by_exit_reason', 'by_bot_version'):
        for bucket in stats[collection].values():
            _finalise_bucket(bucket)
    for time_group in stats['time'].values():
        for bucket in time_group.values():
            _finalise_bucket(bucket)
    stats['symbol_ranking'] = sorted(
        (
            {'symbol': symbol, **{k: v for k, v in bucket.items() if k in ('trades', 'closed', 'win_rate', 'pnl_total', 'profit_factor', 'expectancy')}}
            for symbol, bucket in stats['by_symbol'].items()
        ),
        key=lambda item: (item.get('pnl_total', 0.0), item.get('win_rate', 0.0), item.get('closed', 0)),
        reverse=True,
    )
    _compute_drawdown(stats)
    return stats


def _compute_drawdown(stats):
    # Drawdown is computed during rebuild from closed trades order. Incremental updates
    # update it from the current equity sequence approximation.
    if '_closed_for_drawdown' not in stats:
        stats['general']['max_drawdown_pending'] = False
        return
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in sorted(stats.pop('_closed_for_drawdown'), key=lambda t: t.get('closed_at') or ''):
        equity += _float(trade.get('pnl_usdt'))
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    stats['general']['max_drawdown_usdt'] = round(max_dd, 8)
    stats['general']['max_drawdown_pending'] = False
    stats['general']['equity_current'] = round(equity, 8)
    stats['general']['equity_peak'] = round(peak, 8)



def _diagnostic_regime(trade):
    regime = _trade_regime_value(trade)
    if regime == 'bull':
        return 'BULL'
    if regime == 'bear':
        return 'BEAR'
    if regime in ('neutral', 'sideways'):
        return 'NEUTRAL'
    return 'UNKNOWN'


def _diagnostic_exit_reason(value):
    reason = _normalise_exit_reason(value)
    if reason == 'TP':
        return 'TP'
    if reason == 'SL':
        return 'SL'
    if reason in ('STALE', 'EMERGENCY'):
        return 'PREVENTIVE'
    if reason == 'MANUAL':
        return 'MANUAL'
    if reason == 'RECOVERY':
        return 'RECONCILIATION'
    return 'OTHER_UNKNOWN'


def _loss_total(trades):
    return round(sum(abs(_float(trade.get('pnl_usdt'))) for trade in trades if _float(trade.get('pnl_usdt')) < 0), 8)


def _diagnostic_bucket(trades):
    bucket = _empty_bucket()
    for trade in trades:
        if str(trade.get('status') or '').upper() == 'CLOSED':
            _add_closed(bucket, trade)
        else:
            _add_open(bucket)
    return _finalise_bucket(bucket)


def _sizing_value(trade):
    for key in ('capital_used', 'capital_at_entry', 'notional'):
        value = trade.get(key)
        if value is not None and _float(value) > 0:
            return _float(value)
    quantity = trade.get('quantity')
    entry_price = trade.get('entry_price')
    if quantity is not None and entry_price is not None:
        value = abs(_float(quantity) * _float(entry_price))
        return value if value > 0 else None
    return None


def _average(values):
    return round(sum(values) / len(values), 8) if values else None


def _sizing_diagnostic(trades):
    samples = [(trade, _sizing_value(trade)) for trade in trades]
    samples = [(trade, value) for trade, value in samples if value is not None]
    closed = [(trade, value) for trade, value in samples if str(trade.get('status') or '').upper() == 'CLOSED']
    winners = [value for trade, value in closed if _float(trade.get('pnl_usdt')) > 0]
    losers = [value for trade, value in closed if _float(trade.get('pnl_usdt')) < 0]
    pnl_per_unit = [
        _float(trade.get('pnl_usdt')) / value
        for trade, value in closed if value > 0 and trade.get('pnl_usdt') is not None
    ]
    distribution = {'SMALL': 0, 'MEDIUM': 0, 'LARGE': 0}
    lower = upper = None
    values = sorted(value for _trade, value in samples)
    if len(values) >= 3:
        lower = values[(len(values) - 1) // 3]
        upper = values[((len(values) - 1) * 2) // 3]
        for value in values:
            if value <= lower:
                distribution['SMALL'] += 1
            elif value <= upper:
                distribution['MEDIUM'] += 1
            else:
                distribution['LARGE'] += 1
    return {
        'sample_size': len(samples),
        'average': _average(values),
        'winner_average': _average(winners),
        'loser_average': _average(losers),
        'pnl_per_unit': _average(pnl_per_unit),
        'tercile_bounds': {'small_max': lower, 'medium_max': upper},
        'distribution': distribution if len(values) >= 3 else None,
    }


def _build_version_diagnostic_from_trades(trades, version):
    selected = [trade for trade in trades if _bot_version(trade) == version]
    summary = _diagnostic_bucket(selected)
    summary['version'] = version
    for trade in selected:
        _update_version_bounds(summary, trade)

    by_side = {}
    for side in ('LONG', 'SHORT', 'UNKNOWN'):
        group = [trade for trade in selected if _normalise_direction(trade.get('side')) == side]
        if group or side != 'UNKNOWN':
            by_side[side] = _diagnostic_bucket(group)

    by_regime = {}
    for regime in ('BULL', 'BEAR', 'NEUTRAL', 'UNKNOWN'):
        group = [trade for trade in selected if _diagnostic_regime(trade) == regime]
        if group or regime != 'UNKNOWN':
            by_regime[regime] = _diagnostic_bucket(group)

    closed = [trade for trade in selected if str(trade.get('status') or '').upper() == 'CLOSED']
    by_exit_reason = {}
    for reason in ('TP', 'SL', 'PREVENTIVE', 'MANUAL', 'RECONCILIATION', 'OTHER_UNKNOWN'):
        group = [trade for trade in closed if _diagnostic_exit_reason(trade.get('exit_reason')) == reason]
        pnl = round(sum(_float(trade.get('pnl_usdt')) for trade in group), 8)
        by_exit_reason[reason] = {
            'closed': len(group),
            'pnl_total': pnl,
            'pnl_average': round(pnl / len(group), 8) if group else None,
            'closed_percent': round(len(group) / len(closed) * 100, 4) if closed else None,
            'gross_loss': _loss_total(group),
        }

    by_symbol = {}
    for symbol in sorted({trade.get('symbol') or 'UNKNOWN' for trade in selected}):
        group = [trade for trade in selected if (trade.get('symbol') or 'UNKNOWN') == symbol]
        by_symbol[symbol] = _diagnostic_bucket(group)
    symbol_ranking = [
        {'symbol': symbol, **bucket}
        for symbol, bucket in sorted(by_symbol.items(), key=lambda item: (item[1].get('pnl_total', 0), item[0]))
    ]

    total_loss = _loss_total(closed)
    symbol_losses = sorted(
        ({'name': name, 'loss': bucket.get('gross_loss', 0.0)} for name, bucket in by_symbol.items()),
        key=lambda item: item['loss'], reverse=True,
    )
    side_losses = {name: bucket.get('gross_loss', 0.0) for name, bucket in by_side.items()}
    regime_losses = {name: bucket.get('gross_loss', 0.0) for name, bucket in by_regime.items()}
    exit_losses = {name: bucket.get('gross_loss', 0.0) for name, bucket in by_exit_reason.items()}

    def share(value):
        return round(value / total_loss * 100, 4) if total_loss else None

    worst_side = max(side_losses.items(), key=lambda item: item[1]) if side_losses else (None, 0)
    worst_regime = max(regime_losses.items(), key=lambda item: item[1]) if regime_losses else (None, 0)
    sl_preventive_loss = exit_losses.get('SL', 0) + exit_losses.get('PREVENTIVE', 0)
    concentration = {
        'total_negative_pnl': total_loss,
        'top3_symbol_loss': round(sum(item['loss'] for item in symbol_losses[:3]), 8),
        'top3_symbol_loss_percent': share(sum(item['loss'] for item in symbol_losses[:3])),
        'top5_symbol_loss': round(sum(item['loss'] for item in symbol_losses[:5]), 8),
        'top5_symbol_loss_percent': share(sum(item['loss'] for item in symbol_losses[:5])),
        'largest_loss_side': worst_side[0],
        'largest_loss_side_percent': share(worst_side[1]),
        'largest_loss_regime': worst_regime[0],
        'largest_loss_regime_percent': share(worst_regime[1]),
        'sl_preventive_loss': round(sl_preventive_loss, 8),
        'sl_preventive_loss_percent': share(sl_preventive_loss),
    }
    flags = []
    if summary.get('closed', 0) < 30:
        flags.append('LOW_SAMPLE')
    if summary.get('closed', 0) and summary.get('expectancy', 0) < 0:
        flags.append('NEGATIVE_EXPECTANCY')
    if summary.get('profit_factor') is not None and summary.get('profit_factor') < 1:
        flags.append('PROFIT_FACTOR_BELOW_1')
    if (concentration.get('top3_symbol_loss_percent') or 0) > 50:
        flags.append('LOSS_CONCENTRATION_BY_SYMBOL')
    if (concentration.get('largest_loss_side_percent') or 0) > 50:
        flags.append('LOSS_CONCENTRATION_BY_SIDE')
    if (concentration.get('largest_loss_regime_percent') or 0) > 50:
        flags.append('LOSS_CONCENTRATION_BY_REGIME')

    return {
        'version': version,
        'summary': summary,
        'by_side': by_side,
        'by_regime': by_regime,
        'by_exit_reason': by_exit_reason,
        'symbol_ranking': symbol_ranking,
        'concentration': concentration,
        'sizing': _sizing_diagnostic(selected),
        'flags': flags,
        'flag_rules': {
            'LOW_SAMPLE': 'closed trades < 30',
            'NEGATIVE_EXPECTANCY': 'closed trades > 0 and expectancy < 0',
            'PROFIT_FACTOR_BELOW_1': 'profit factor is available and < 1',
            'LOSS_CONCENTRATION_BY_SYMBOL': 'top 3 symbols explain > 50% of gross losses',
            'LOSS_CONCENTRATION_BY_SIDE': 'one side explains > 50% of gross losses',
            'LOSS_CONCENTRATION_BY_REGIME': 'one regime explains > 50% of gross losses',
        },
        'normalization_rules': {
            'regime': 'bull/bullish=BULL; bear/bearish=BEAR; neutral/sideways/range=NEUTRAL; missing=UNKNOWN',
            'exit_reason': 'TP; SL; STALE/EMERGENCY=PREVENTIVE; MANUAL=MANUAL; RECOVERY=RECONCILIATION; remainder=OTHER_UNKNOWN',
            'size_bands': 'sample terciles using deterministic sorted index cutoffs; N/A below 3 samples',
        },
    }


def analyze_version_performance(version=None, trades_file=history.DEFAULT_TRADES_FILE):
    version = version or version_history.current_version()
    trade_events, invalid = _iter_jsonl(trades_file)
    if invalid:
        logging.warning('version diagnostic ignored invalid trade lines=%s path=%s', invalid, trades_file)
    merged = _merge_trade_events(trade_events)
    return _build_version_diagnostic_from_trades(list(merged.values()), version)


def get_version_diagnostic(version=None, stats_file=DEFAULT_STATS_FILE):
    version = version or version_history.current_version()
    stats = load_stats(stats_file)
    diagnostic = (stats.get('version_diagnostics') or {}).get(version)
    return diagnostic or analyze_version_performance(version)


def _stats_from_sources(trades_file, decisions_file, snapshots_file, features_file=feature_store.DEFAULT_FEATURES_FILE):
    stats = _empty_stats()
    stats['source']['trades_file'] = trades_file
    stats['source']['decisions_file'] = decisions_file
    stats['source']['snapshots_file'] = snapshots_file
    stats['source']['features_file'] = features_file

    trade_events, invalid_trades = _iter_jsonl(trades_file)
    decision_events, invalid_decisions = _iter_jsonl(decisions_file)
    snapshot_events, invalid_snapshots = _iter_jsonl(snapshots_file)
    feature_events, invalid_features = _iter_jsonl(features_file)

    stats['source']['trade_events'] = len(trade_events)
    stats['source']['decision_events'] = len(decision_events)
    stats['source']['snapshot_events'] = len(snapshot_events)
    stats['source']['feature_events'] = len(feature_events)
    stats['source']['invalid_trade_lines'] = invalid_trades
    stats['source']['invalid_decision_lines'] = invalid_decisions
    stats['source']['invalid_snapshot_lines'] = invalid_snapshots
    stats['source']['invalid_feature_lines'] = invalid_features
    stats['history']['trades_registered'] = len(trade_events)
    stats['history']['decisions_registered'] = len(decision_events)
    stats['history']['snapshots_registered'] = len(snapshot_events)

    feature_lookup = _feature_index(feature_events)
    merged = _merge_trade_events(trade_events)
    versions = {_bot_version(trade) for trade in merged.values()} | {version_history.current_version()}
    stats['version_diagnostics'] = {
        version: _build_version_diagnostic_from_trades(list(merged.values()), version)
        for version in versions
    }
    stats['_closed_for_drawdown'] = []
    for trade in merged.values():
        trade = _enrich_trade_regime(trade, feature_lookup)
        _add_trade(stats, trade, mark_processed=True)
        if str(trade.get('status') or '').upper() == 'CLOSED':
            stats['_closed_for_drawdown'].append(trade)
    for record in decision_events:
        _add_decision(stats, record)
    stats['snapshots']['total'] = len(snapshot_events)
    _set_history_bounds(stats, trade_events, decision_events, snapshot_events)
    return _finalise_stats(stats)


def _record_time(record):
    for key in ('recorded_at', 'timestamp', 'opened_at', 'closed_at', 'entry_time', 'exit_time'):
        value = record.get(key)
        if value and _parse_dt(value):
            return value
    return None


def _set_history_bounds(stats, *groups):
    values = []
    for records in groups:
        for record in records:
            value = _record_time(record)
            if value:
                values.append(value)
    if not values:
        return
    values.sort(key=lambda v: _parse_dt(v) or datetime.max.replace(tzinfo=timezone.utc))
    stats['history']['first_record'] = values[0]
    stats['history']['last_record'] = values[-1]


def save_stats(stats, stats_file=DEFAULT_STATS_FILE):
    if _test_mode_default_stats_write_blocked(stats_file):
        logging.debug('analytics stats write suppressed in test mode path=%s', stats_file)
        return stats_file
    os.makedirs(os.path.dirname(stats_file), exist_ok=True)
    with open(stats_file, 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    return stats_file


def rebuild_statistics(trades_file=history.DEFAULT_TRADES_FILE, decisions_file=history.DEFAULT_DECISIONS_FILE,
                       snapshots_file=history.DEFAULT_SNAPSHOTS_FILE, stats_file=DEFAULT_STATS_FILE,
                       features_file=feature_store.DEFAULT_FEATURES_FILE):
    stats = _stats_from_sources(trades_file, decisions_file, snapshots_file, features_file)
    save_stats(stats, stats_file)
    return stats


def load_stats(stats_file=DEFAULT_STATS_FILE, rebuild_if_missing=True, trades_file=history.DEFAULT_TRADES_FILE,
               decisions_file=history.DEFAULT_DECISIONS_FILE, snapshots_file=history.DEFAULT_SNAPSHOTS_FILE,
               features_file=feature_store.DEFAULT_FEATURES_FILE):
    if not os.path.exists(stats_file):
        if rebuild_if_missing:
            if _test_mode_default_stats_write_blocked(stats_file):
                return _empty_stats()
            return rebuild_statistics(trades_file, decisions_file, snapshots_file, stats_file, features_file)
        return _empty_stats()
    try:
        with open(stats_file, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            if rebuild_if_missing and data.get('schema_version') != STATS_SCHEMA_VERSION:
                if _test_mode_default_stats_write_blocked(stats_file):
                    return data
                return rebuild_statistics(trades_file, decisions_file, snapshots_file, stats_file, features_file)
            return data
    except json.JSONDecodeError as exc:
        logging.warning('analytics_engine stats.json corrupt path=%s error=%s', stats_file, exc)
    except Exception as exc:
        logging.warning('analytics_engine stats.json unreadable path=%s error=%s', stats_file, exc)
    if rebuild_if_missing:
        return rebuild_statistics(trades_file, decisions_file, snapshots_file, stats_file, features_file)
    return _empty_stats()


def _rehydrate_working_stats(stats):
    # Reintroduce private duration counters as zero so one incremental update can
    # be finalised without reading history. Existing averages remain in stats.
    return stats


def update_trade(trade, stats_file=DEFAULT_STATS_FILE, trades_file=history.DEFAULT_TRADES_FILE,
                 decisions_file=history.DEFAULT_DECISIONS_FILE, snapshots_file=history.DEFAULT_SNAPSHOTS_FILE,
                 features_file=feature_store.DEFAULT_FEATURES_FILE):
    try:
        stats = load_stats(stats_file, True, trades_file, decisions_file, snapshots_file, features_file)
        trade_id = trade.get('trade_id') if isinstance(trade, dict) else None
        if not trade_id:
            return stats
        if str(trade.get('status') or '').upper() != 'CLOSED':
            return stats
        processed = set(stats.get('processed_closed_trade_ids') or [])
        if trade_id in processed:
            return stats

        stats.setdefault('source', {})
        stats['source']['trade_events'] = stats['source'].get('trade_events', 0) + 1
        stats.setdefault('history', {})
        stats['history']['trades_registered'] = stats['history'].get('trades_registered', 0) + 1
        current_time = _record_time(trade)
        if current_time:
            first = stats['history'].get('first_record')
            last = stats['history'].get('last_record')
            if not first or (_parse_dt(current_time) and _parse_dt(first) and _parse_dt(current_time) < _parse_dt(first)):
                stats['history']['first_record'] = current_time
            if not last or (_parse_dt(current_time) and _parse_dt(last) and _parse_dt(current_time) > _parse_dt(last)):
                stats['history']['last_record'] = current_time
        indexed = stats.get('trade_index', {}).get(trade_id, {})
        was_open = str(indexed.get('status') or '').upper() == 'OPEN'
        merged_trade = dict(indexed)
        merged_trade.update({k: v for k, v in trade.items() if v is not None})
        if _trade_regime_value(merged_trade) == 'unknown':
            feature_events, _ = _iter_jsonl(features_file)
            merged_trade = _enrich_trade_regime(merged_trade, _feature_index(feature_events))

        # Updating mature aggregate averages incrementally is less invasive than
        # rebuilding; derive enough counters from public fields before adding.
        _prepare_incremental_buckets(stats)
        _add_trade(stats, merged_trade, mark_processed=True)
        if was_open:
            _convert_open_to_closed(stats, merged_trade)
        _update_incremental_drawdown(stats, merged_trade)
        version_events, _invalid_versions = _iter_jsonl(trades_file)
        version_trades = _merge_trade_events(version_events)
        versions = {_bot_version(item) for item in version_trades.values()} | {version_history.current_version()}
        stats['version_diagnostics'] = {
            version: _build_version_diagnostic_from_trades(list(version_trades.values()), version)
            for version in versions
        }
        stats = _finalise_stats(stats)
        save_stats(stats, stats_file)
        return stats
    except Exception as exc:
        logging.warning('analytics_engine update_trade failed: %s', exc)
        return load_stats(stats_file, True, trades_file, decisions_file, snapshots_file, features_file)


def _prepare_incremental_buckets(stats):
    def prep(bucket):
        if '_duration_sum' not in bucket:
            avg = bucket.get('duration_average_minutes')
            count = bucket.get('closed', 0) if avg is not None else 0
            bucket['_duration_sum'] = _float(avg) * count
            bucket['_duration_count'] = count
        bucket.setdefault('trades', bucket.get('total_trades', bucket.get('closed', 0) + bucket.get('open', 0)))
        bucket.setdefault('closed', bucket.get('closed_trades', 0))
        bucket.setdefault('open', bucket.get('open_trades', 0))
        bucket.setdefault('win', 0)
        bucket.setdefault('loss', 0)
        bucket.setdefault('breakeven', 0)
        bucket.setdefault('pnl_total', 0.0)
        bucket.setdefault('gross_profit', 0.0)
        bucket.setdefault('gross_loss', 0.0)

    prep(stats['general'])
    for collection in ('by_symbol', 'by_direction', 'by_regime', 'by_exit_reason', 'by_bot_version'):
        for bucket in stats.get(collection, {}).values():
            prep(bucket)
    for time_group in stats.get('time', {}).values():
        for bucket in time_group.values():
            prep(bucket)


def _convert_bucket_open_to_closed(bucket):
    if not isinstance(bucket, dict):
        return
    if bucket.get('open', 0) > 0:
        bucket['open'] -= 1
    if bucket.get('open_trades', 0) > 0:
        bucket['open_trades'] -= 1
    if bucket.get('trades', 0) > 0:
        bucket['trades'] -= 1
    if bucket.get('total_trades', 0) > 0:
        bucket['total_trades'] -= 1


def _convert_open_to_closed(stats, trade):
    symbol = trade.get('symbol') or 'UNKNOWN'
    side = _normalise_direction(trade.get('side'))
    regime = _trade_regime_value(trade)
    _convert_bucket_open_to_closed(stats['general'])
    for bucket in (
        stats.get('by_symbol', {}).get(symbol),
        stats.get('by_direction', {}).get(side),
        stats.get('by_regime', {}).get(regime),
        stats.get('by_bot_version', {}).get(_bot_version(trade)),
    ):
        _convert_bucket_open_to_closed(bucket)


def _update_incremental_drawdown(stats, trade):
    general = stats['general']
    equity = _float(general.get('equity_current')) + _float(trade.get('pnl_usdt'))
    peak = max(_float(general.get('equity_peak')), equity)
    max_dd = max(_float(general.get('max_drawdown_usdt')), peak - equity)
    general['equity_current'] = round(equity, 8)
    general['equity_peak'] = round(peak, 8)
    general['max_drawdown_usdt'] = round(max_dd, 8)
    general['max_drawdown_pending'] = False


def get_general_stats(stats_file=DEFAULT_STATS_FILE):
    return load_stats(stats_file).get('general', {})


def get_symbol_stats(symbol=None, stats_file=DEFAULT_STATS_FILE):
    data = load_stats(stats_file).get('by_symbol', {})
    if symbol is None:
        return data
    return data.get(symbol, {})


def get_direction_stats(direction=None, stats_file=DEFAULT_STATS_FILE):
    data = load_stats(stats_file).get('by_direction', {})
    if direction is None:
        return data
    return data.get(_normalise_direction(direction), {})


def get_exit_reason_stats(reason=None, stats_file=DEFAULT_STATS_FILE):
    data = load_stats(stats_file).get('by_exit_reason', {})
    if reason is None:
        return data
    return data.get(_normalise_exit_reason(reason), {})


def get_time_stats(group=None, stats_file=DEFAULT_STATS_FILE):
    data = load_stats(stats_file).get('time', {})
    if group is None:
        return data
    return data.get(group, {})


def get_external_deposits(ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return user-provided external deposits recorded in the capital ledger."""
    return _safe_accounting(0.0, capital_accounting.get_external_deposits, ledger_file, asset)


def get_external_withdrawals(ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return external withdrawals recorded in the capital ledger."""
    return _safe_accounting(0.0, capital_accounting.get_external_withdrawals, ledger_file, asset)


def get_net_external_flows(ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return deposits minus withdrawals. Positive values are user capital inflows."""
    return _safe_accounting(0.0, capital_accounting.get_net_external_flows, ledger_file, asset)


def get_total_commissions(ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return commissions recorded in the capital ledger."""
    return _safe_accounting(0.0, capital_accounting.get_total_commissions, ledger_file, asset)


def get_total_funding(ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return funding fees recorded in the capital ledger."""
    return _safe_accounting(0.0, capital_accounting.get_total_funding, ledger_file, asset)


def get_realized_trading_pnl(ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return realized trading PnL explicitly recorded in the capital ledger."""
    return _safe_accounting(0.0, capital_accounting.get_realized_trading_pnl, ledger_file, asset)


def get_adjusted_equity(current_equity, ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return current equity excluding external capital flows.

    Formula: current_equity - external_deposits + external_withdrawals.
    Deposits are removed because they are not trading performance; withdrawals are
    added back because they reduce current equity without representing a loss.
    """
    return _safe_accounting(None, capital_accounting.get_adjusted_equity, current_equity, ledger_file, asset)


def get_adjusted_pnl(current_equity, starting_equity=0.0,
                     ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return PnL adjusted for external capital flows.

    Formula: adjusted_equity - starting_equity.
    """
    return _safe_accounting(
        None,
        capital_accounting.get_adjusted_pnl,
        current_equity,
        starting_equity,
        ledger_file,
        asset,
    )


def get_adjusted_roi(current_equity, starting_equity,
                     ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return adjusted ROI percentage.

    Formula: adjusted_pnl / starting_equity * 100. Returns None when the
    denominator is missing or zero.
    """
    return _safe_accounting(
        None,
        capital_accounting.get_adjusted_roi,
        current_equity,
        starting_equity,
        ledger_file,
        asset,
    )


def get_trading_equity(current_equity, ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return equity attributable to trading after removing external flows."""
    return get_adjusted_equity(current_equity, ledger_file=ledger_file, asset=asset)


def get_capital_accounting_stats(current_equity=None, starting_equity=0.0,
                                 ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE, asset=None,
                                 unrealized_pnl=None):
    """Return passive accounting metrics without changing historical analytics."""
    return _safe_accounting(
        {
            'external_deposits': 0.0,
            'external_withdrawals': 0.0,
            'net_external_flows': 0.0,
            'commissions': 0.0,
            'funding': 0.0,
            'realized_trading_pnl': 0.0,
        },
        capital_accounting.get_accounting_summary,
        current_equity,
        starting_equity,
        ledger_file,
        asset,
        unrealized_pnl,
    )


def get_live_capital_accounting_stats(
        ledger_file=capital_accounting.capital_ledger.DEFAULT_LEDGER_FILE,
        asset=None, observer=None):
    """Return accounting metrics enriched by one read-only live observation.

    The observer values managed Spot quantity at the current read-only ticker
    and reuses Futures unrealized PnL reported by the account. A quantity or
    price mismatch makes the observation explicitly incomplete; it is never
    converted to zero and wallet dust is not attributed to a managed trade.
    """
    if observer is None:
        import reconcile_capital_ledger
        observer = reconcile_capital_ledger.observe_capital
    try:
        observation = observer() or {}
    except Exception as exc:
        observation = {'errors': [f'live_observation_failed:{type(exc).__name__}']}
    errors = sorted(set(observation.get('errors') or []))
    current_spot = observation.get('baseline_spot_unrealized_pnl')
    current_futures = observation.get('baseline_futures_unrealized_pnl')
    current_total = observation.get('baseline_unrealized_pnl')
    complete = not errors and current_total is not None and observation.get('observed_equity') is not None
    summary = get_capital_accounting_stats(
        current_equity=observation.get('observed_equity'), ledger_file=ledger_file,
        asset=asset, unrealized_pnl=current_total if complete else None,
    )
    summary.update({
        'current_spot_unrealized_pnl': current_spot if complete else None,
        'current_futures_unrealized_pnl': current_futures if complete else None,
        'current_unrealized_pnl': current_total if complete else None,
        'current_unrealized_pnl_by_position': observation.get('open_positions_at_bootstrap') or [],
        'observation_timestamp': observation.get('timestamp'),
        'observation_complete': complete,
        'missing_fields': errors,
        'observation_source': observation.get('observation_source'),
        'observed_equity': observation.get('observed_equity'),
    })
    if not complete:
        summary['accounting_status'] = 'INCOMPLETE_DATA'
    return summary
