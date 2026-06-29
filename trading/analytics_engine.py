#!/usr/bin/env python3
"""Passive analytics index built from historical JSONL files."""
import json
import logging
import os
from datetime import datetime, timezone

import history


DEFAULT_STATS_FILE = os.path.join(history.DEFAULT_HISTORY_DIR, 'stats.json')

EXIT_REASONS = ('TP', 'SL', 'TRAILING', 'PARTIAL', 'STALE', 'RECOVERY', 'EMERGENCY', 'MANUAL', 'UNKNOWN')
DIRECTIONS = ('LONG', 'SHORT', 'UNKNOWN')
REGIMES = ('BULL', 'BEAR', 'SIDEWAYS', 'NEUTRAL', 'UNKNOWN')


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
        'schema_version': 1,
        'source': {
            'trades_file': history.DEFAULT_TRADES_FILE,
            'decisions_file': history.DEFAULT_DECISIONS_FILE,
            'snapshots_file': history.DEFAULT_SNAPSHOTS_FILE,
            'trade_events': 0,
            'decision_events': 0,
            'snapshot_events': 0,
            'invalid_trade_lines': 0,
            'invalid_decision_lines': 0,
            'invalid_snapshot_lines': 0,
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
    value = str(value or '').lower()
    if value in ('bull', 'bullish'):
        return 'BULL'
    if value in ('bear', 'bearish'):
        return 'BEAR'
    if value in ('sideways', 'chop', 'range'):
        return 'SIDEWAYS'
    if value in ('neutral', 'neutro'):
        return 'NEUTRAL'
    return 'UNKNOWN'


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
        current.update({k: v for k, v in record.items() if v is not None})
    return trades


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
    regime = _normalise_regime(trade.get('market_regime'))
    trade_id = trade.get('trade_id')

    if trade_id:
        current = stats.setdefault('trade_index', {}).setdefault(trade_id, {})
        current.update({k: v for k, v in {
            'trade_id': trade_id,
            'symbol': symbol,
            'side': side,
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
    for collection in ('by_symbol', 'by_direction', 'by_regime', 'by_exit_reason'):
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


def _stats_from_sources(trades_file, decisions_file, snapshots_file):
    stats = _empty_stats()
    stats['source']['trades_file'] = trades_file
    stats['source']['decisions_file'] = decisions_file
    stats['source']['snapshots_file'] = snapshots_file

    trade_events, invalid_trades = _iter_jsonl(trades_file)
    decision_events, invalid_decisions = _iter_jsonl(decisions_file)
    snapshot_events, invalid_snapshots = _iter_jsonl(snapshots_file)

    stats['source']['trade_events'] = len(trade_events)
    stats['source']['decision_events'] = len(decision_events)
    stats['source']['snapshot_events'] = len(snapshot_events)
    stats['source']['invalid_trade_lines'] = invalid_trades
    stats['source']['invalid_decision_lines'] = invalid_decisions
    stats['source']['invalid_snapshot_lines'] = invalid_snapshots

    merged = _merge_trade_events(trade_events)
    stats['_closed_for_drawdown'] = []
    for trade in merged.values():
        _add_trade(stats, trade, mark_processed=True)
        if str(trade.get('status') or '').upper() == 'CLOSED':
            stats['_closed_for_drawdown'].append(trade)
    for record in decision_events:
        _add_decision(stats, record)
    stats['snapshots']['total'] = len(snapshot_events)
    return _finalise_stats(stats)


def save_stats(stats, stats_file=DEFAULT_STATS_FILE):
    os.makedirs(os.path.dirname(stats_file), exist_ok=True)
    with open(stats_file, 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    return stats_file


def rebuild_statistics(trades_file=history.DEFAULT_TRADES_FILE, decisions_file=history.DEFAULT_DECISIONS_FILE,
                       snapshots_file=history.DEFAULT_SNAPSHOTS_FILE, stats_file=DEFAULT_STATS_FILE):
    stats = _stats_from_sources(trades_file, decisions_file, snapshots_file)
    save_stats(stats, stats_file)
    return stats


def load_stats(stats_file=DEFAULT_STATS_FILE, rebuild_if_missing=True, trades_file=history.DEFAULT_TRADES_FILE,
               decisions_file=history.DEFAULT_DECISIONS_FILE, snapshots_file=history.DEFAULT_SNAPSHOTS_FILE):
    if not os.path.exists(stats_file):
        if rebuild_if_missing:
            return rebuild_statistics(trades_file, decisions_file, snapshots_file, stats_file)
        return _empty_stats()
    try:
        with open(stats_file, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError as exc:
        logging.warning('analytics_engine stats.json corrupt path=%s error=%s', stats_file, exc)
    except Exception as exc:
        logging.warning('analytics_engine stats.json unreadable path=%s error=%s', stats_file, exc)
    if rebuild_if_missing:
        return rebuild_statistics(trades_file, decisions_file, snapshots_file, stats_file)
    return _empty_stats()


def _rehydrate_working_stats(stats):
    # Reintroduce private duration counters as zero so one incremental update can
    # be finalised without reading history. Existing averages remain in stats.
    return stats


def update_trade(trade, stats_file=DEFAULT_STATS_FILE, trades_file=history.DEFAULT_TRADES_FILE,
                 decisions_file=history.DEFAULT_DECISIONS_FILE, snapshots_file=history.DEFAULT_SNAPSHOTS_FILE):
    try:
        stats = load_stats(stats_file, True, trades_file, decisions_file, snapshots_file)
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
        indexed = stats.get('trade_index', {}).get(trade_id, {})
        was_open = str(indexed.get('status') or '').upper() == 'OPEN'
        merged_trade = dict(indexed)
        merged_trade.update({k: v for k, v in trade.items() if v is not None})

        # Updating mature aggregate averages incrementally is less invasive than
        # rebuilding; derive enough counters from public fields before adding.
        _prepare_incremental_buckets(stats)
        _add_trade(stats, merged_trade, mark_processed=True)
        if was_open:
            _convert_open_to_closed(stats, merged_trade)
        _update_incremental_drawdown(stats, merged_trade)
        stats = _finalise_stats(stats)
        save_stats(stats, stats_file)
        return stats
    except Exception as exc:
        logging.warning('analytics_engine update_trade failed: %s', exc)
        return load_stats(stats_file, True, trades_file, decisions_file, snapshots_file)


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
    for collection in ('by_symbol', 'by_direction', 'by_regime', 'by_exit_reason'):
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
    regime = _normalise_regime(trade.get('market_regime'))
    _convert_bucket_open_to_closed(stats['general'])
    for bucket in (
        stats.get('by_symbol', {}).get(symbol),
        stats.get('by_direction', {}).get(side),
        stats.get('by_regime', {}).get(regime),
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
