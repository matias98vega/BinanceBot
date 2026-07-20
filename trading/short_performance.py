#!/usr/bin/env python3
"""Read-only exploratory SHORT performance analysis."""
import math
import random
import statistics
from datetime import datetime

import analytics_engine
import feature_store
import history
import version_history


NUMERIC_FEATURES = (
    'pnl_usdt', 'pnl_pct', 'capital_notional', 'leverage', 'duration_minutes',
    'btc_change_4h', 'btc_change_1h', 'btc_price', 'rsi', 'macd_hist',
    'ema20', 'ema50', 'distance_to_ema20_pct', 'distance_to_ema50_pct',
    'atr', 'atr_pct', 'volatility', 'volume_ratio', 'btc_correlation',
    'hour_utc', 'weekday', 'score',
)
BAND_FEATURES = (
    'btc_change_4h', 'rsi', 'macd_hist', 'atr_pct', 'volume_ratio',
    'distance_to_ema20_pct', 'distance_to_ema50_pct', 'capital_notional', 'hour_utc',
)


def _nested(data, *keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _first_number(*values):
    for value in values:
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def _feature_index(path):
    records, invalid = analytics_engine._iter_jsonl(path)
    index = {}
    for record in records:
        trade_id = _nested(record, 'identification', 'trade_id') or record.get('trade_id')
        if trade_id:
            index.setdefault(trade_id, record)
            index.setdefault(analytics_engine._base_trade_id(trade_id), record)
    return index, invalid


def _opened_at(trade):
    return trade.get('opened_at') or trade.get('entry_time') or trade.get('timestamp')


def _entry_features(trade, feature):
    extra = feature.get('extra') if isinstance(feature, dict) else {}
    btc = trade.get('btc_context') if isinstance(trade.get('btc_context'), dict) else {}
    opened = analytics_engine._parse_dt(_opened_at(trade))
    entry = _first_number(_nested(feature, 'symbol_indicators', 'entry_price'), trade.get('entry_price'))
    ema20 = _first_number(_nested(feature, 'symbol_indicators', 'ema20'), (trade.get('extra') or {}).get('ema20'))
    ema50 = _first_number(_nested(feature, 'symbol_indicators', 'ema50'), (trade.get('extra') or {}).get('ema50'))

    def distance(ema):
        return ((entry - ema) / ema * 100) if entry is not None and ema not in (None, 0) else None

    return {
        'pnl_usdt': _number(trade.get('pnl_usdt')),
        'pnl_pct': _number(trade.get('pnl_pct')),
        'capital_notional': analytics_engine._sizing_value(trade),
        'leverage': _number(_nested(feature, 'capital', 'leverage')),
        'duration_minutes': _number(trade.get('duration_minutes')),
        'btc_change_4h': _first_number(_nested(feature, 'market', 'btc_change_4h'), btc.get('change_4h')),
        'btc_change_1h': _number(btc.get('change_1h')),
        'btc_price': _first_number(_nested(feature, 'market', 'btc_price'), btc.get('btc_price')),
        'rsi': _first_number(_nested(feature, 'symbol_indicators', 'rsi'), trade.get('rsi')),
        'macd_hist': _first_number(_nested(feature, 'symbol_indicators', 'macd_hist'), (trade.get('extra') or {}).get('macd_hist')),
        'ema20': ema20,
        'ema50': ema50,
        'distance_to_ema20_pct': _number(_nested(feature, 'symbol_indicators', 'distance_to_ema20_pct')) if feature else distance(ema20),
        'distance_to_ema50_pct': _number(_nested(feature, 'symbol_indicators', 'distance_to_ema50_pct')) if feature else distance(ema50),
        'atr': _first_number(_nested(feature, 'market', 'atr'), trade.get('atr')),
        'atr_pct': _number(trade.get('atr_pct')),
        'volatility': _first_number(_nested(feature, 'market', 'volatility'), trade.get('volatility')),
        'volume_ratio': _first_number(_nested(feature, 'market', 'volume_ratio'), (trade.get('extra') or {}).get('volume_ratio')),
        'btc_correlation': _first_number((extra or {}).get('btc_correlation'), (trade.get('extra') or {}).get('btc_correlation')),
        'hour_utc': _number(_nested(feature, 'market', 'hour_utc')) if feature else (opened.hour if opened else None),
        'weekday': _number(_nested(feature, 'market', 'weekday')) if feature else (opened.weekday() if opened else None),
        'score': _first_number(_nested(feature, 'scoring', 'score_total'), trade.get('score')),
    }


def _quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (position - low)


def _describe(values):
    values = [value for value in values if value is not None]
    return {
        'count': len(values),
        'mean': statistics.fmean(values) if values else None,
        'median': statistics.median(values) if values else None,
        'p25': _quantile(values, 0.25),
        'p75': _quantile(values, 0.75),
    }


def _comparison(rows, name):
    wins = [row['features'].get(name) for row in rows if row['result'] == 'WIN']
    losses = [row['features'].get(name) for row in rows if row['result'] == 'LOSS']
    win_stats, loss_stats = _describe(wins), _describe(losses)
    difference = None
    relative = None
    effect = None
    if win_stats['mean'] is not None and loss_stats['mean'] is not None:
        difference = win_stats['mean'] - loss_stats['mean']
        if loss_stats['mean'] != 0:
            relative = difference / abs(loss_stats['mean']) * 100
        clean_wins = [value for value in wins if value is not None]
        clean_losses = [value for value in losses if value is not None]
        if len(clean_wins) > 1 and len(clean_losses) > 1:
            pooled = math.sqrt((statistics.variance(clean_wins) + statistics.variance(clean_losses)) / 2)
            effect = difference / pooled if pooled else None
    return {'valid': win_stats['count'] + loss_stats['count'], 'winners': win_stats, 'losers': loss_stats, 'mean_difference': difference, 'relative_difference_percent': relative, 'cohen_d': effect}


def _bucket(rows):
    return analytics_engine._diagnostic_bucket([row['trade'] for row in rows])


def _band_analysis(rows, feature, min_sample):
    valid = [row for row in rows if row['features'].get(feature) is not None]
    if len(valid) < min_sample:
        return {'status': 'INSUFFICIENT_SAMPLE', 'valid': len(valid), 'excluded': len(rows) - len(valid)}
    values = [row['features'][feature] for row in valid]
    lower, upper = _quantile(values, 1 / 3), _quantile(values, 2 / 3)
    groups = {'LOW': [], 'MEDIUM': [], 'HIGH': []}
    for row in valid:
        value = row['features'][feature]
        groups['LOW' if value <= lower else 'MEDIUM' if value <= upper else 'HIGH'].append(row)
    return {'status': 'OK', 'valid': len(valid), 'excluded': len(rows) - len(valid), 'bounds': {'low_max': lower, 'medium_max': upper}, 'bands': {name: _bucket(group) for name, group in groups.items()}}


def _bootstrap_ci(values, iterations=2000):
    if len(values) < 2:
        return None
    rng = random.Random(42)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(iterations))
    return {'iterations': iterations, 'seed': 42, 'lower_95': _quantile(means, 0.025), 'upper_95': _quantile(means, 0.975)}


def _approx_drawdown(rows):
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: item['trade'].get('closed_at') or ''):
        equity += _number(row['trade'].get('pnl_usdt')) or 0
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _candidate(name, rows, predicate):
    retained = [row for row in rows if predicate(row)]
    original, filtered = _bucket(rows), _bucket(retained)
    regimes = {}
    symbols = {}
    for row in retained:
        regime = analytics_engine._diagnostic_regime(row['trade'])
        regimes[regime] = regimes.get(regime, 0) + (_number(row['trade'].get('pnl_usdt')) or 0)
        symbol = row['trade'].get('symbol') or 'UNKNOWN'
        symbols[symbol] = symbols.get(symbol, 0) + (_number(row['trade'].get('pnl_usdt')) or 0)
    return {
        'name': name, 'exploratory_only': True, 'retained': len(retained), 'discarded': len(rows) - len(retained),
        'coverage_percent': len(retained) / len(rows) * 100 if rows else None,
        'original': original, 'filtered': filtered, 'approx_drawdown': _approx_drawdown(retained),
        'impact_by_regime': {key: round(value, 8) for key, value in regimes.items()},
        'impact_by_symbol': {key: round(value, 8) for key, value in symbols.items()},
    }


def build_report(version=None, min_sample=5, top=10, trades_file=history.DEFAULT_TRADES_FILE, features_file=feature_store.DEFAULT_FEATURES_FILE):
    version = version or version_history.current_version()
    events, invalid_trades = analytics_engine._iter_jsonl(trades_file)
    merged = analytics_engine._merge_trade_events(events)
    features, invalid_features = _feature_index(features_file)
    selected = [
        trade for trade in merged.values()
        if analytics_engine._bot_version(trade) == version and analytics_engine._normalise_direction(trade.get('side')) == 'SHORT'
    ]
    rows = []
    for trade in selected:
        feature = features.get(trade.get('trade_id')) or features.get(analytics_engine._base_trade_id(trade.get('trade_id')))
        pnl = _number(trade.get('pnl_usdt')) or 0
        status = str(trade.get('status') or '').upper()
        rows.append({'trade': trade, 'feature': feature, 'features': _entry_features(trade, feature or {}), 'result': 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'BREAKEVEN', 'status': status})
    closed = [row for row in rows if row['status'] == 'CLOSED']
    wins = [row for row in closed if row['result'] == 'WIN']
    losses = [row for row in closed if row['result'] == 'LOSS']

    comparisons = {name: _comparison(closed, name) for name in NUMERIC_FEATURES}
    missingness = {
        name: {'missing': sum(row['features'].get(name) is None for row in rows), 'available': sum(row['features'].get(name) is not None for row in rows), 'missing_percent': sum(row['features'].get(name) is None for row in rows) / len(rows) * 100 if rows else None}
        for name in NUMERIC_FEATURES
    }
    bands = {name: _band_analysis(closed, name, min_sample) for name in BAND_FEATURES}
    regimes = {name: _bucket([row for row in rows if analytics_engine._diagnostic_regime(row['trade']) == name]) for name in ('BULL', 'BEAR', 'NEUTRAL', 'UNKNOWN')}
    exits = {}
    for name in ('TP', 'SL', 'PREVENTIVE', 'MANUAL', 'RECONCILIATION', 'OTHER_UNKNOWN'):
        group = [row for row in closed if analytics_engine._diagnostic_exit_reason(row['trade'].get('exit_reason')) == name]
        exits[name] = _bucket(group)

    symbols = []
    for symbol in sorted({row['trade'].get('symbol') or 'UNKNOWN' for row in rows}):
        group = [row for row in rows if (row['trade'].get('symbol') or 'UNKNOWN') == symbol]
        bucket = _bucket(group)
        regimes_in_group = [analytics_engine._diagnostic_regime(row['trade']) for row in group]
        exits_in_group = [analytics_engine._diagnostic_exit_reason(row['trade'].get('exit_reason')) for row in group if row['status'] == 'CLOSED']
        bucket.update({
            'symbol': symbol,
            'dominant_regime': max(set(regimes_in_group), key=regimes_in_group.count) if regimes_in_group else None,
            'dominant_exit_reason': max(set(exits_in_group), key=exits_in_group.count) if exits_in_group else None,
            'average_notional': _describe([row['features']['capital_notional'] for row in group])['mean'],
            'sufficient_sample': bucket['closed'] >= min_sample,
        })
        symbols.append(bucket)
    symbols.sort(key=lambda item: (item['pnl_total'], item['symbol']))
    total_loss = sum(abs(_number(row['trade'].get('pnl_usdt')) or 0) for row in losses)
    symbol_losses = sorted((item['gross_loss'] for item in symbols), reverse=True)
    concentration = {f'top{count}_loss_percent': sum(symbol_losses[:count]) / total_loss * 100 if total_loss else None for count in (1, 3, 5)}

    preventive = [row for row in closed if analytics_engine._diagnostic_exit_reason(row['trade'].get('exit_reason')) == 'PREVENTIVE']
    sl_rows = [row for row in closed if analytics_engine._diagnostic_exit_reason(row['trade'].get('exit_reason')) == 'SL']
    def focused(group):
        return {'summary': _bucket(group), 'regimes': {name: sum(analytics_engine._diagnostic_regime(row['trade']) == name for row in group) for name in ('BULL', 'BEAR', 'NEUTRAL', 'UNKNOWN')}, 'symbols': sorted({row['trade'].get('symbol') for row in group}), 'comparisons': {name: _describe([row['features'].get(name) for row in group]) for name in ('btc_change_4h', 'rsi', 'macd_hist', 'volume_ratio', 'atr_pct', 'distance_to_ema20_pct', 'distance_to_ema50_pct', 'hour_utc', 'capital_notional')}, 'limitation': 'No post-entry counterfactual TP/SL outcome is inferred.'}

    crosses = {'regime_btc4h': {}, 'regime_exit': {}, 'regime_symbol': {}, 'regime_size': {}, 'regime_hour': {}}

    def add_cross(section, key, group):
        if len(group) >= min_sample:
            crosses[section][key] = _bucket(group)

    for regime in ('BULL', 'BEAR', 'NEUTRAL', 'UNKNOWN'):
        regime_rows = [row for row in closed if analytics_engine._diagnostic_regime(row['trade']) == regime]
        for sign, predicate in (('BTC4H_NEGATIVE', lambda value: value is not None and value < 0), ('BTC4H_NONNEGATIVE', lambda value: value is not None and value >= 0)):
            add_cross('regime_btc4h', f'{regime}x{sign}', [row for row in regime_rows if predicate(row['features'].get('btc_change_4h'))])
        for reason in ('TP', 'SL', 'PREVENTIVE', 'MANUAL', 'RECONCILIATION', 'OTHER_UNKNOWN'):
            add_cross('regime_exit', f'{regime}x{reason}', [row for row in regime_rows if analytics_engine._diagnostic_exit_reason(row['trade'].get('exit_reason')) == reason])
        for symbol in sorted({row['trade'].get('symbol') or 'UNKNOWN' for row in regime_rows}):
            add_cross('regime_symbol', f'{regime}x{symbol}', [row for row in regime_rows if (row['trade'].get('symbol') or 'UNKNOWN') == symbol])
        for feature, section in (('capital_notional', 'regime_size'), ('hour_utc', 'regime_hour')):
            analysis = bands.get(feature) or {}
            if analysis.get('status') != 'OK':
                continue
            lower = analysis['bounds']['low_max']
            upper = analysis['bounds']['medium_max']
            for label, predicate in (
                ('LOW', lambda value: value is not None and value <= lower),
                ('MEDIUM', lambda value: value is not None and lower < value <= upper),
                ('HIGH', lambda value: value is not None and value > upper),
            ):
                add_cross(section, f'{regime}x{label}', [row for row in regime_rows if predicate(row['features'].get(feature))])

    candidates = [
        _candidate('exclude_neutral', closed, lambda row: analytics_engine._diagnostic_regime(row['trade']) != 'NEUTRAL'),
        _candidate('require_btc_4h_negative', closed, lambda row: row['features'].get('btc_change_4h') is not None and row['features']['btc_change_4h'] < 0),
        _candidate('require_volume_ratio_at_least_1', closed, lambda row: row['features'].get('volume_ratio') is not None and row['features']['volume_ratio'] >= 1),
        _candidate('require_rsi_at_most_50', closed, lambda row: row['features'].get('rsi') is not None and row['features']['rsi'] <= 50),
    ]
    summary = _bucket(rows)
    flags = []
    if summary['closed'] and summary['expectancy'] < 0: flags.append('SHORT_NEGATIVE_EXPECTANCY')
    if summary['profit_factor'] is not None and summary['profit_factor'] < 1: flags.append('SHORT_PROFIT_FACTOR_BELOW_1')
    if (concentration.get('top3_loss_percent') or 0) > 50: flags.append('SHORT_LOSS_CONCENTRATION_BY_SYMBOL')
    neutral_loss = regimes['NEUTRAL']['gross_loss']
    if total_loss and neutral_loss / total_loss > 0.5: flags.append('SHORT_LOSS_CONCENTRATION_IN_NEUTRAL')
    if preventive and exits['PREVENTIVE']['pnl_total'] < 0: flags.append('SHORT_PREVENTIVE_CLOSE_DRAG')
    if sl_rows and exits['SL']['pnl_total'] < 0: flags.append('SHORT_SL_DRAG')
    if len(closed) < 30: flags.append('LOW_SAMPLE')
    if any((item['missing_percent'] or 0) > 40 for item in missingness.values()): flags.append('FEATURE_MISSINGNESS_HIGH')
    flags.append('WALK_FORWARD_INSUFFICIENT_SAMPLE')

    return {
        'version': version, 'parameters': {'min_sample': min_sample, 'top': top},
        'universe': {'total': len(rows), 'closed': len(closed), 'open': len(rows) - len(closed), 'winners': len(wins), 'losers': len(losses), 'invalid_trade_lines': invalid_trades, 'invalid_feature_lines': invalid_features},
        'summary': summary, 'regimes': regimes, 'exit_reasons': exits,
        'winner_loser_comparison': comparisons, 'bands': bands, 'crosses': crosses,
        'symbols': {'worst': symbols[:top], 'best': list(reversed(symbols[-top:])), 'insufficient_sample': [item['symbol'] for item in symbols if not item['sufficient_sample']], 'concentration': concentration},
        'preventive_closes': focused(preventive), 'sl_closes': focused(sl_rows),
        'data_quality': {'opening_snapshot_found': sum(row['feature'] is not None for row in rows), 'opening_snapshot_missing': sum(row['feature'] is None for row in rows), 'missingness': missingness, 'complete': sum(row['feature'] is not None and all(row['features'].get(name) is not None for name in BAND_FEATURES) for row in rows), 'partial': sum(row['feature'] is not None and any(row['features'].get(name) is None for name in BAND_FEATURES) for row in rows), 'recovered': sum('recovered' in str(row['trade'].get('trade_id')).lower() for row in rows), 'legacy': sum(analytics_engine._bot_version(row['trade']) == analytics_engine.LEGACY_BOT_VERSION for row in rows)},
        'bootstrap': {'pnl_mean_95_ci': _bootstrap_ci([row['features']['pnl_usdt'] for row in closed if row['features']['pnl_usdt'] is not None])},
        'candidate_rules': candidates, 'walk_forward': {'status': 'WALK_FORWARD_INSUFFICIENT_SAMPLE', 'required_closed': 60, 'available_closed': len(closed)},
        'flags': flags,
        'flag_rules': {'LOW_SAMPLE': 'closed < 30', 'FEATURE_MISSINGNESS_HIGH': 'any reported feature missingness > 40%', 'concentration': 'share of gross losses > 50%', 'drag': 'group exists and group PnL < 0', 'WALK_FORWARD_INSUFFICIENT_SAMPLE': 'closed < 60'},
        'limitations': ['Exploratory multiple comparisons; no multiplicity-adjusted confirmatory inference.', 'Associations are not causal.', 'Entry features only; no post-entry feature is used as an explanatory variable.', 'Candidate rules are offline hypotheses and do not modify trading behavior.'],
    }
