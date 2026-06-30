#!/usr/bin/env python3
"""Passive insights generated from analytics_engine stats."""
import json
import logging
import os
from datetime import datetime, timezone

import analytics_engine
import history


DEFAULT_INSIGHTS_FILE = os.path.join(history.DEFAULT_HISTORY_DIR, 'insights.json')
SCHEMA_VERSION = 1
MIN_SAMPLE = 5
WARN_WIN_RATE_DROP_PCT = 10.0
WARN_PROFIT_FACTOR_DROP_PCT = 20.0

CATEGORIES = (
    'GENERAL',
    'RENDIMIENTO',
    'RIESGO',
    'SIMBOLOS',
    'LONG_VS_SHORT',
    'REGIMEN',
    'TEMPORAL',
    'SALIDAS',
)


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct_change(current, previous):
    current = _float(current)
    previous = _float(previous)
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 4)


def _fmt_pct(value):
    return f'{_float(value):.1f}%'


def _fmt_money(value):
    return f'{_float(value):+.2f} USDT'


def _fmt_ratio(value):
    if value is None:
        return 'N/D'
    return f'{_float(value):.2f}'


def _name(value):
    text = str(value or 'UNKNOWN')
    return text.replace('_', ' ')


def _empty_insights(warnings=None):
    return {
        'schema_version': SCHEMA_VERSION,
        'generated_at': _now_iso(),
        'source': {
            'stats_file': analytics_engine.DEFAULT_STATS_FILE,
            'source_type': 'analytics_engine.stats',
        },
        'warnings': warnings or [],
        'insights': {category: [] for category in CATEGORIES},
        'alerts': [],
        'summary': [],
    }


def _insight(tipo, categoria, prioridad, texto, datos=None, confianza='media'):
    return {
        'tipo': tipo,
        'categoria': categoria,
        'prioridad': prioridad,
        'texto': texto,
        'datos_utilizados': datos or {},
        'confianza': confianza,
    }


def _add(target, item):
    target['insights'].setdefault(item['categoria'], []).append(item)
    if item['tipo'] == 'ALERTA':
        target.setdefault('alerts', []).append(item)
    return item


def _bucket_closed(bucket):
    return int((bucket or {}).get('closed') or 0)


def _bucket_trades(bucket):
    return int((bucket or {}).get('trades') or 0)


def _best(items, key, min_closed=1):
    candidates = [(name, bucket) for name, bucket in items if _bucket_closed(bucket) >= min_closed]
    if not candidates:
        return None, None
    return max(candidates, key=lambda pair: _float((pair[1] or {}).get(key)))


def _worst(items, key, min_closed=1):
    candidates = [(name, bucket) for name, bucket in items if _bucket_closed(bucket) >= min_closed]
    if not candidates:
        return None, None
    return min(candidates, key=lambda pair: _float((pair[1] or {}).get(key)))


def _best_profit_factor(items, min_closed=1):
    candidates = []
    for name, bucket in items:
        if _bucket_closed(bucket) < min_closed:
            continue
        pf = bucket.get('profit_factor')
        if pf is None:
            continue
        candidates.append((name, bucket))
    if not candidates:
        return None, None
    return max(candidates, key=lambda pair: _float(pair[1].get('profit_factor')))


def _period_pair(rows):
    if not isinstance(rows, dict) or len(rows) < 2:
        return None, None, None, None
    keys = sorted(rows)
    previous_key, current_key = keys[-2], keys[-1]
    return current_key, rows.get(current_key), previous_key, rows.get(previous_key)


def _add_general(target, stats):
    general = stats.get('general') or {}
    closed = int(general.get('closed_trades') or general.get('closed') or 0)
    if closed <= 0:
        _add(target, _insight('INFO', 'GENERAL', 'BAJA', 'Todavia no hay trades cerrados suficientes para generar conclusiones.', {'closed': closed}, 'alta'))
        return

    _add(target, _insight(
        'OBSERVACION', 'GENERAL', 'MEDIA',
        f'Profit Factor actual: {_fmt_ratio(general.get("profit_factor"))}.',
        {'profit_factor': general.get('profit_factor'), 'closed': closed},
        'alta' if closed >= MIN_SAMPLE else 'media',
    ))
    _add(target, _insight(
        'OBSERVACION', 'GENERAL', 'MEDIA',
        f'Expectancy actual: {_fmt_money(general.get("expectancy"))} por trade.',
        {'expectancy': general.get('expectancy'), 'closed': closed},
        'alta' if closed >= MIN_SAMPLE else 'media',
    ))

    best = general.get('best_trade') or {}
    worst = general.get('worst_trade') or {}
    if best:
        _add(target, _insight('OBSERVACION', 'RENDIMIENTO', 'MEDIA', f'Mayor ganancia historica: {_name(best.get("symbol"))} con {_fmt_money(best.get("pnl_usdt"))}.', best, 'alta'))
    if worst:
        _add(target, _insight('OBSERVACION', 'RIESGO', 'ALTA', f'Mayor perdida historica: {_name(worst.get("symbol"))} con {_fmt_money(worst.get("pnl_usdt"))}.', worst, 'alta'))

    if _float(general.get('expectancy')) < 0 and closed >= MIN_SAMPLE:
        _add(target, _insight('ALERTA', 'RIESGO', 'ALTA', 'La expectancy esta negativa con muestra suficiente.', {'expectancy': general.get('expectancy'), 'closed': closed}, 'alta'))
    if _float(general.get('max_drawdown_usdt')) > 0 and closed >= MIN_SAMPLE:
        _add(target, _insight('OBSERVACION', 'RIESGO', 'MEDIA', f'Drawdown maximo registrado: {_fmt_money(-abs(_float(general.get("max_drawdown_usdt"))))}.', {'max_drawdown_usdt': general.get('max_drawdown_usdt')}, 'media'))


def _add_symbol_insights(target, stats):
    symbols = stats.get('by_symbol') or {}
    items = list(symbols.items())
    if not items:
        return
    most_profitable = _best(items, 'pnl_total')
    least_profitable = _worst(items, 'pnl_total')
    best_wr = _best(items, 'win_rate', min_closed=MIN_SAMPLE)
    best_pf = _best_profit_factor(items, min_closed=MIN_SAMPLE)
    most_traded = max(items, key=lambda pair: _bucket_trades(pair[1]))

    if most_profitable[0]:
        name, bucket = most_profitable
        _add(target, _insight('OBSERVACION', 'SIMBOLOS', 'ALTA', f'{name} es el simbolo mas rentable.', {'symbol': name, **bucket}, 'alta' if _bucket_closed(bucket) >= MIN_SAMPLE else 'media'))
    if least_profitable[0]:
        name, bucket = least_profitable
        _add(target, _insight('OBSERVACION', 'SIMBOLOS', 'ALTA', f'{name} es el simbolo menos rentable.', {'symbol': name, **bucket}, 'alta' if _bucket_closed(bucket) >= MIN_SAMPLE else 'media'))
        if _float(bucket.get('pnl_total')) < 0 and _bucket_closed(bucket) >= MIN_SAMPLE:
            _add(target, _insight('ALERTA', 'SIMBOLOS', 'ALTA', f'{name} esta en PnL negativo con muestra suficiente.', {'symbol': name, **bucket}, 'alta'))
    if best_wr[0]:
        name, bucket = best_wr
        _add(target, _insight('OBSERVACION', 'SIMBOLOS', 'MEDIA', f'{name} tiene el mayor win rate.', {'symbol': name, **bucket}, 'alta'))
    if best_pf[0]:
        name, bucket = best_pf
        _add(target, _insight('OBSERVACION', 'SIMBOLOS', 'MEDIA', f'{name} tiene el mayor Profit Factor.', {'symbol': name, **bucket}, 'alta'))
    if most_traded[0]:
        name, bucket = most_traded
        _add(target, _insight('OBSERVACION', 'SIMBOLOS', 'BAJA', f'{name} es el simbolo mas operado.', {'symbol': name, **bucket}, 'media'))


def _add_direction_insights(target, stats):
    directions = stats.get('by_direction') or {}
    long_b = directions.get('LONG') or {}
    short_b = directions.get('SHORT') or {}
    if _bucket_closed(long_b) < MIN_SAMPLE or _bucket_closed(short_b) < MIN_SAMPLE:
        return
    long_pnl = _float(long_b.get('pnl_total'))
    short_pnl = _float(short_b.get('pnl_total'))
    winner = 'SHORT' if short_pnl > long_pnl else 'LONG'
    diff = abs(short_pnl - long_pnl)
    wr_diff = round(_float(short_b.get('win_rate')) - _float(long_b.get('win_rate')), 4)
    pf_diff = None
    if long_b.get('profit_factor') is not None and short_b.get('profit_factor') is not None:
        pf_diff = round(_float(short_b.get('profit_factor')) - _float(long_b.get('profit_factor')), 4)
    _add(target, _insight(
        'OBSERVACION', 'LONG_VS_SHORT', 'ALTA',
        f'{winner} rinde mejor que la direccion opuesta.',
        {'LONG': long_b, 'SHORT': short_b, 'pnl_diff_usdt': diff, 'win_rate_diff_short_minus_long': wr_diff, 'profit_factor_diff_short_minus_long': pf_diff},
        'alta',
    ))


def _add_regime_insights(target, stats):
    regimes = stats.get('by_regime') or {}
    items = [(name, bucket) for name, bucket in regimes.items() if name != 'UNKNOWN']
    best = _best(items, 'pnl_total')
    worst = _worst(items, 'pnl_total')
    if best[0]:
        _add(target, _insight('OBSERVACION', 'REGIMEN', 'MEDIA', f'{_name(best[0]).title()} es el regimen mas rentable.', {'regime': best[0], **best[1]}, 'alta' if _bucket_closed(best[1]) >= MIN_SAMPLE else 'media'))
    if worst[0]:
        _add(target, _insight('OBSERVACION', 'REGIMEN', 'MEDIA', f'{_name(worst[0]).title()} es el regimen menos rentable.', {'regime': worst[0], **worst[1]}, 'alta' if _bucket_closed(worst[1]) >= MIN_SAMPLE else 'media'))


def _add_temporal_insights(target, stats):
    time_stats = stats.get('time') or {}
    for group, label in (('hour', 'hora'), ('day', 'dia'), ('week', 'semana'), ('month', 'mes')):
        rows = time_stats.get(group) or {}
        items = list(rows.items())
        best = _best(items, 'pnl_total')
        worst = _worst(items, 'pnl_total')
        if best[0]:
            suffix = ' UTC' if group == 'hour' else ''
            _add(target, _insight('OBSERVACION', 'TEMPORAL', 'MEDIA', f'{best[0]}{suffix} es la mejor {label}.', {'period': best[0], 'group': group, **best[1]}, 'media'))
        if worst[0]:
            suffix = ' UTC' if group == 'hour' else ''
            _add(target, _insight('OBSERVACION', 'TEMPORAL', 'MEDIA', f'{worst[0]}{suffix} es la peor {label}.', {'period': worst[0], 'group': group, **worst[1]}, 'media'))


def _add_exit_insights(target, stats):
    exits = stats.get('by_exit_reason') or {}
    total = sum(_bucket_closed(bucket) for bucket in exits.values())
    if total <= 0:
        return
    for reason in ('TP', 'SL', 'TRAILING', 'PARTIAL', 'RECOVERY', 'MANUAL', 'EMERGENCY'):
        bucket = exits.get(reason) or {}
        count = _bucket_closed(bucket)
        pct = round(count / total * 100, 4) if total else 0.0
        _add(target, _insight('OBSERVACION', 'SALIDAS', 'BAJA', f'{reason} representa {_fmt_pct(pct)} de los cierres.', {'reason': reason, 'count': count, 'total': total, 'percent': pct}, 'alta' if total >= MIN_SAMPLE else 'media'))


def _add_period_comparisons(target, stats):
    time_stats = stats.get('time') or {}
    for group, label in (('day', 'diario'), ('week', 'semanal'), ('month', 'mensual')):
        current_key, current, previous_key, previous = _period_pair((time_stats.get(group) or {}))
        if not current or not previous:
            continue
        if _bucket_closed(current) < MIN_SAMPLE or _bucket_closed(previous) < MIN_SAMPLE:
            continue
        wr_delta = round(_float(current.get('win_rate')) - _float(previous.get('win_rate')), 4)
        if wr_delta <= -WARN_WIN_RATE_DROP_PCT:
            _add(target, _insight('ALERTA', 'RIESGO', 'ALTA', f'Win Rate {label} cayo {_fmt_pct(abs(wr_delta))}.', {'current_period': current_key, 'previous_period': previous_key, 'current': current, 'previous': previous, 'delta_points': wr_delta}, 'alta'))
        pf_delta = _pct_change(current.get('profit_factor'), previous.get('profit_factor'))
        if pf_delta is not None:
            if pf_delta <= -WARN_PROFIT_FACTOR_DROP_PCT:
                _add(target, _insight('ALERTA', 'RIESGO', 'ALTA', f'Profit Factor {label} cayo {_fmt_pct(abs(pf_delta))}.', {'current_period': current_key, 'previous_period': previous_key, 'current': current, 'previous': previous, 'delta_percent': pf_delta}, 'alta'))
            elif pf_delta > 0:
                _add(target, _insight('OBSERVACION', 'RENDIMIENTO', 'MEDIA', f'Profit Factor {label} mejoro {_fmt_pct(pf_delta)}.', {'current_period': current_key, 'previous_period': previous_key, 'delta_percent': pf_delta}, 'alta'))


def _build_summary(insights):
    priority_order = {'ALTA': 0, 'MEDIA': 1, 'BAJA': 2}
    flat = []
    for items in insights.get('insights', {}).values():
        flat.extend(items)
    flat.sort(key=lambda item: (priority_order.get(item.get('prioridad'), 3), 0 if item.get('tipo') == 'ALERTA' else 1))
    return flat[:8]


def save_insights(insights, insights_file=DEFAULT_INSIGHTS_FILE):
    os.makedirs(os.path.dirname(insights_file), exist_ok=True)
    with open(insights_file, 'w', encoding='utf-8') as f:
        json.dump(insights, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    return insights_file


def rebuild_insights(stats_file=analytics_engine.DEFAULT_STATS_FILE, insights_file=DEFAULT_INSIGHTS_FILE):
    stats = analytics_engine.load_stats(stats_file=stats_file, rebuild_if_missing=False)
    insights = _empty_insights()
    insights['source']['stats_file'] = stats_file
    _add_general(insights, stats)
    _add_symbol_insights(insights, stats)
    _add_direction_insights(insights, stats)
    _add_regime_insights(insights, stats)
    _add_temporal_insights(insights, stats)
    _add_exit_insights(insights, stats)
    _add_period_comparisons(insights, stats)
    insights['summary'] = _build_summary(insights)
    save_insights(insights, insights_file)
    return insights


def load_insights(insights_file=DEFAULT_INSIGHTS_FILE, rebuild_if_missing=True, stats_file=analytics_engine.DEFAULT_STATS_FILE):
    if not os.path.exists(insights_file):
        if rebuild_if_missing:
            return rebuild_insights(stats_file=stats_file, insights_file=insights_file)
        return _empty_insights()
    try:
        with open(insights_file, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError as exc:
        logging.warning('insights_engine insights.json corrupt path=%s error=%s', insights_file, exc)
    except Exception as exc:
        logging.warning('insights_engine insights.json unreadable path=%s error=%s', insights_file, exc)
    if rebuild_if_missing:
        rebuilt = rebuild_insights(stats_file=stats_file, insights_file=insights_file)
        rebuilt.setdefault('warnings', []).append('insights.json corrupto; reconstruido desde stats.json')
        save_insights(rebuilt, insights_file)
        return rebuilt
    return _empty_insights(['insights.json corrupto'])


def get_general_insights(insights_file=DEFAULT_INSIGHTS_FILE):
    data = load_insights(insights_file)
    return data.get('insights', {}).get('GENERAL', [])


def get_symbol_insights(insights_file=DEFAULT_INSIGHTS_FILE):
    data = load_insights(insights_file)
    return data.get('insights', {}).get('SIMBOLOS', [])


def get_risk_insights(insights_file=DEFAULT_INSIGHTS_FILE):
    data = load_insights(insights_file)
    risk = list(data.get('insights', {}).get('RIESGO', []))
    risk.extend(data.get('alerts', []))
    return risk


def get_temporal_insights(insights_file=DEFAULT_INSIGHTS_FILE):
    data = load_insights(insights_file)
    return data.get('insights', {}).get('TEMPORAL', [])


def get_all_insights(insights_file=DEFAULT_INSIGHTS_FILE):
    return load_insights(insights_file)
