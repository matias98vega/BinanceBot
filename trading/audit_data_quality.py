#!/usr/bin/env python3
"""Local data quality auditor for BinanceBot runtime/history files."""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone


TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TRADING_DIR)
MAX_APPEND_GAP_SECONDS = 6 * 60 * 60
VALID_SIDES = {'LONG', 'SHORT'}
VALID_STATUS = {'OPEN', 'CLOSED'}
VALID_REGIMES = {'bull', 'bear', 'sideways', 'neutral', 'unknown', 'bullish', 'bearish'}
SENSITIVE_MARKERS = ('key', 'secret', 'token', 'signature', 'header', 'cookie', 'authorization')
CAPITAL_LEDGER_TYPES = {
    'external_deposit',
    'external_withdrawal',
    'rebalance',
    'realized_pnl',
    'commission',
    'funding_fee',
}


class AuditReport:
    def __init__(self):
        self.files_checked = 0
        self.records_checked = 0
        self.errors = []
        self.warnings = []
        self.missing_fields = defaultdict(Counter)
        self.complete_records = defaultdict(int)
        self.total_records = defaultdict(int)
        self.recommendations = set()
        self.totals_by_type = defaultdict(float)
        self.critical_examples = []
        self.possible_false_positives = []
        self.incomplete_examples = defaultdict(list)

    def error(self, path, message):
        self.errors.append(f'{_display_path(path)}: {message}')

    def warning(self, path, message):
        self.warnings.append(f'{_display_path(path)}: {message}')

    def missing(self, path, field):
        self.missing_fields[_display_path(path)][field] += 1

    def completeness(self, path, complete):
        key = _display_path(path)
        self.total_records[key] += 1
        if complete:
            self.complete_records[key] += 1

    def critical_example(self, path, message, record=None):
        if len(self.critical_examples) >= 25:
            return
        self.critical_examples.append(_example_payload(path, message, record))

    def false_positive(self, path, message, record=None):
        if len(self.possible_false_positives) >= 25:
            return
        self.possible_false_positives.append(_example_payload(path, message, record))

    def incomplete_example(self, path, record, missing_fields, recent=False):
        key = _display_path(path)
        bucket = self.incomplete_examples[key]
        if len(bucket) >= 10:
            bucket.pop(0)
        bucket.append({
            'line': record.get('_audit_line'),
            'timestamp': _timestamp_value(record) or 'N/D',
            'missing': list(missing_fields),
            'age': 'recent' if recent else 'historical',
            'trade_id': _get_nested(record, 'identification.trade_id') or record.get('trade_id'),
            'symbol': _get_nested(record, 'identification.symbol') or record.get('symbol'),
        })


def _display_path(path):
    try:
        return os.path.relpath(path, PROJECT_DIR)
    except Exception:
        return str(path)


def _project_path(project_dir, *parts):
    return os.path.join(project_dir, *parts)


def _public_record_fields(record):
    if not isinstance(record, dict):
        return {}
    keys = (
        'trade_id', 'symbol', 'side', 'direction', 'status', 'event_type',
        'timestamp', 'recorded_at', 'opened_at', 'closed_at', 'pnl_usdt',
        'entry_price', 'exit_price', 'exit_reason',
    )
    return {key: record.get(key) for key in keys if record.get(key) not in (None, '')}


def _example_payload(path, message, record=None):
    payload = {
        'path': _display_path(path),
        'message': message,
    }
    if isinstance(record, dict):
        trade_id = record.get('trade_id')
        payload.update({
            'line': record.get('_audit_line'),
            'trade_id': trade_id,
            'symbol': record.get('symbol'),
            'side': record.get('side') or record.get('direction'),
            'status': record.get('status'),
            'event_type': record.get('event_type'),
            'timestamp': _timestamp_value(record),
            'pnl_usdt': record.get('pnl_usdt'),
            'is_partial': _is_partial_trade_id(trade_id),
            'fields': _public_record_fields(record),
        })
    return payload


def _parse_ts(value):
    if value in (None, ''):
        return None, 'missing'
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc), None
        except Exception as exc:
            return None, str(exc)
    try:
        text = str(value).replace('Z', '+00:00')
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), None
    except Exception as exc:
        return None, str(exc)


def _timestamp_value(record):
    for key in ('timestamp', 'recorded_at', 'opened_at', 'closed_at', 'entry_time', 'exit_time', 'last_check', 'last_attempt'):
        value = record.get(key) if isinstance(record, dict) else None
        if value not in (None, ''):
            return value
    return None


def _validate_timestamp(report, path, record, previous_dt=None, require=True):
    value = _timestamp_value(record)
    dt, err = _parse_ts(value)
    if err == 'missing':
        if require:
            report.warning(path, 'timestamp ausente')
            report.missing(path, 'timestamp')
        return None
    if err:
        report.error(path, f'timestamp invalido: {value}')
        return None
    now = datetime.now(timezone.utc)
    if dt > now:
        report.error(path, f'timestamp futuro: {value}')
    if previous_dt and dt < previous_dt:
        report.warning(path, f'timestamp fuera de orden: {value}')
    if previous_dt and (dt - previous_dt).total_seconds() > MAX_APPEND_GAP_SECONDS:
        report.warning(path, f'gap grande entre registros: {round((dt - previous_dt).total_seconds() / 3600, 2)}h')
    return dt


def _is_number(value, positive=False, allow_zero=True):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if positive and (number < 0 or (number == 0 and not allow_zero)):
        return False
    return True


def _get_nested(record, dotted):
    current = record
    for part in dotted.split('.'):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _first_present(container, keys):
    if not isinstance(container, dict):
        return None
    for key in keys:
        value = container.get(key)
        if value not in (None, ''):
            return value
    return None


def _is_partial_trade_id(trade_id):
    text = str(trade_id or '')
    return text.endswith(':partial') or ':partial' in text


def _base_trade_id(trade_id):
    text = str(trade_id or '')
    if ':partial' in text:
        return text.split(':partial', 1)[0]
    return text


def _looks_recovered_or_imported(record):
    text = ' '.join(str(record.get(key) or '') for key in ('exit_reason', 'source', 'description', 'event_type')).lower()
    return any(token in text for token in ('recovery', 'recovered', 'import', 'reconcile', 'manual'))


def _looks_binance_error(data):
    text = ' '.join(str(data.get(key) or '') for key in (
        'last_error', 'last_message', 'status', 'pending_reason', 'blocked_reason',
    )).lower()
    return any(token in text for token in ('binance', 'http', 'transfer', 'api', 'code=', '-5013', '-2010'))


def _has_sensitive_metadata(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in SENSITIVE_MARKERS):
                return True
            if _has_sensitive_metadata(item):
                return True
    elif isinstance(value, list):
        return any(_has_sensitive_metadata(item) for item in value)
    return False


def _read_json(path, report, required=False):
    if not os.path.exists(path):
        if required:
            report.warning(path, 'archivo faltante')
        return None
    report.files_checked += 1
    try:
        if os.path.getsize(path) == 0:
            report.warning(path, 'archivo vacio')
            return None
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            report.error(path, 'JSON no es objeto dict')
            return None
        report.records_checked += 1
        return data
    except json.JSONDecodeError as exc:
        report.error(path, f'JSON corrupto: {exc}')
        return None
    except Exception as exc:
        report.error(path, f'no se pudo leer JSON: {exc}')
        return None


def _read_jsonl(path, report, required=False):
    records = []
    if not os.path.exists(path):
        if required:
            report.warning(path, 'archivo faltante')
        return records
    report.files_checked += 1
    try:
        if os.path.getsize(path) == 0:
            report.warning(path, 'archivo vacio')
            return records
        previous_dt = None
        with open(path, encoding='utf-8') as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.strip()
                if not line:
                    report.warning(path, f'linea vacia #{lineno}')
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    report.error(path, f'linea corrupta #{lineno}: {exc}')
                    continue
                if not isinstance(record, dict):
                    report.error(path, f'linea #{lineno} no es objeto dict')
                    continue
                record['_audit_line'] = lineno
                previous_dt = _validate_timestamp(report, path, record, previous_dt=previous_dt, require=False) or previous_dt
                records.append(record)
                report.records_checked += 1
    except Exception as exc:
        report.error(path, f'no se pudo leer JSONL: {exc}')
    return records


def _audit_trade_records(path, records, report):
    opens = set()
    closes = set()
    open_status = set()
    all_ids = {record.get('trade_id') for record in records if isinstance(record, dict) and record.get('trade_id')}
    base_ids = {_base_trade_id(trade_id) for trade_id in all_ids}
    for record in records:
        trade_id = record.get('trade_id')
        status = str(record.get('status') or '').upper()
        event_type = str(record.get('event_type') or '').upper()
        if trade_id and (event_type == 'TRADE_OPEN' or status == 'OPEN'):
            opens.add(trade_id)
            base_ids.add(_base_trade_id(trade_id))
    for record in records:
        required = ['trade_id', 'symbol', 'side', 'status']
        complete = True
        for field in required:
            if record.get(field) in (None, ''):
                report.warning(path, f'{field} ausente')
                report.missing(path, field)
                complete = False
        trade_id = record.get('trade_id')
        status = str(record.get('status') or '').upper()
        side = str(record.get('side') or record.get('direction') or '').upper()
        event_type = str(record.get('event_type') or '').upper()
        if side and side not in VALID_SIDES:
            report.error(path, f'side invalido trade_id={trade_id}: {side}')
            report.critical_example(path, f'side invalido trade_id={trade_id}: {side}', record)
            complete = False
        if status and status not in VALID_STATUS:
            report.error(path, f'status invalido trade_id={trade_id}: {status}')
            report.critical_example(path, f'status invalido trade_id={trade_id}: {status}', record)
            complete = False
        if trade_id and (event_type == 'TRADE_OPEN' or status == 'OPEN'):
            if status == 'OPEN':
                open_status.add(trade_id)
        if trade_id and (event_type == 'TRADE_CLOSE' or status == 'CLOSED'):
            closes.add(trade_id)
            if trade_id not in opens and event_type == 'TRADE_CLOSE':
                base_id = _base_trade_id(trade_id)
                if _is_partial_trade_id(trade_id) and base_id in base_ids:
                    message = f'cierre parcial sin apertura exacta pero base relacionado existe trade_id={trade_id} base={base_id}'
                    report.warning(path, message)
                    report.false_positive(path, message, record)
                elif _looks_recovered_or_imported(record):
                    message = f'cierre recuperado/importado sin apertura previa trade_id={trade_id}'
                    report.warning(path, message)
                    report.false_positive(path, message, record)
                else:
                    message = f'cierre total sin apertura previa trade_id={trade_id}'
                    report.error(path, message)
                    report.critical_example(path, message, record)
            if record.get('pnl_usdt') is None:
                report.error(path, f'pnl_usdt faltante en CLOSED trade_id={trade_id}')
                report.critical_example(path, f'pnl_usdt faltante en CLOSED trade_id={trade_id}', record)
                report.missing(path, 'pnl_usdt')
                complete = False
            if not _is_number(record.get('exit_price'), positive=True, allow_zero=False):
                report.error(path, f'exit_price invalido en CLOSED trade_id={trade_id}')
                report.critical_example(path, f'exit_price invalido en CLOSED trade_id={trade_id}', record)
                complete = False
        if record.get('entry_price') is not None and not _is_number(record.get('entry_price'), positive=True, allow_zero=False):
            report.error(path, f'entry_price invalido trade_id={trade_id}')
            complete = False
        duration = record.get('duration_seconds')
        if duration is None:
            duration = record.get('duration_minutes')
        if duration is not None and _is_number(duration) and float(duration) < 0:
            report.error(path, f'duration negativa trade_id={trade_id}')
            report.critical_example(path, f'duration negativa trade_id={trade_id}', record)
            complete = False
        report.completeness(path, complete)
    for trade_id in sorted(open_status - closes):
        report.warning(path, f'trade abierto sin cierre trade_id={trade_id}')


def _audit_feature_records(path, records, report, trade_ids=None):
    unknown = 0
    total = 0
    trade_ids = trade_ids or set()
    recent_start = max(0, len(records) - 10)
    for idx, record in enumerate(records):
        total += 1
        complete = True
        missing_fields = []
        checks = {
            'trade_id': _get_nested(record, 'identification.trade_id') or record.get('trade_id'),
            'symbol': _get_nested(record, 'identification.symbol') or record.get('symbol'),
            'market.regime': _get_nested(record, 'market.regime'),
            'market.btc_price': _get_nested(record, 'market.btc_price'),
            'market.btc_change_4h': _get_nested(record, 'market.btc_change_4h'),
            'scoring.score_total': _get_nested(record, 'scoring.score_total'),
            'capital.position_final': _get_nested(record, 'capital.position_final'),
            'symbol_indicators.entry_price': _get_nested(record, 'symbol_indicators.entry_price'),
        }
        for field, value in checks.items():
            if value in (None, ''):
                report.warning(path, f'{field} faltante')
                report.missing(path, field)
                missing_fields.append(field)
                complete = False
        regime = str(checks['market.regime'] or 'unknown').lower()
        if regime == 'unknown':
            unknown += 1
        if regime not in VALID_REGIMES:
            report.warning(path, f'market.regime no canonico: {regime}')
        trade_id = checks['trade_id']
        if trade_id and trade_ids and trade_id not in trade_ids:
            report.warning(path, f'feature sin trade relacionado trade_id={trade_id}')
            missing_fields.append('trade_relation')
        report.completeness(path, complete)
        if not complete or missing_fields:
            report.incomplete_example(path, record, missing_fields, recent=idx >= recent_start)
    if total and unknown / total > 0.5:
        report.warning(path, f'unknown regime excesivo: {unknown}/{total}')
        report.recommendations.add('Revisar persistencia de market.regime en Feature Store.')


def _audit_capital_ledger(path, records, report):
    for record in records:
        complete = True
        movement_type = str(record.get('type') or '').lower()
        amount = record.get('amount')
        if not movement_type:
            report.error(path, 'type faltante')
            report.missing(path, 'type')
            complete = False
        elif movement_type not in CAPITAL_LEDGER_TYPES:
            report.warning(path, f'type no reconocido: {movement_type}')
        if not _is_number(amount):
            report.error(path, f'amount invalido type={movement_type}')
            report.missing(path, 'amount')
            complete = False
        else:
            amount_f = float(amount)
            report.totals_by_type[movement_type] += amount_f
            if movement_type in {'external_deposit', 'external_withdrawal'} and amount_f < 0:
                report.error(path, f'{movement_type} negativo')
                complete = False
        if not record.get('asset'):
            report.error(path, f'asset faltante type={movement_type}')
            report.missing(path, 'asset')
            complete = False
        if not record.get('timestamp'):
            report.warning(path, f'timestamp faltante type={movement_type}')
            report.missing(path, 'timestamp')
            complete = False
        if _has_sensitive_metadata(record.get('metadata')):
            report.error(path, 'metadata contiene datos sensibles')
            complete = False
        report.completeness(path, complete)


def _audit_bot_state(path, data, report):
    if not data:
        return
    market = data.get('market') if isinstance(data.get('market'), dict) else {}
    capital = data.get('capital') if isinstance(data.get('capital'), dict) else {}
    positions = data.get('positions') if isinstance(data.get('positions'), dict) else {}
    field_aliases = {
        'regime': ('regime', 'market_regime', 'btc_regime', 'trend'),
        'btc_price': ('btc_price', 'price', 'btc_last_price'),
        'btc_change_4h': ('btc_change_4h', 'change_4h', 'btc_chg_4h', 'change4h'),
    }
    for canonical, aliases in field_aliases.items():
        if _first_present(market, aliases) in (None, ''):
            report.warning(path, f'market.{canonical} faltante')
            report.missing(path, f'market.{canonical}')
    if 'directional_mode' not in market:
        report.warning(path, 'market.directional_mode faltante')
        report.missing(path, 'market.directional_mode')
    for wallet in ('spot', 'futures'):
        real = capital.get(f'{wallet}_real')
        used = capital.get(f'{wallet}_used')
        if real is not None and used is not None and _is_number(real) and _is_number(used) and float(used) - float(real) > 0.01:
            report.error(path, f'{wallet}_used mayor que {wallet}_real')
    for side in ('long', 'short'):
        state = positions.get(side) if isinstance(positions.get(side), dict) else {}
        current = state.get('current')
        maximum = state.get('max')
        if current is not None and maximum is not None and _is_number(current) and _is_number(maximum) and float(current) > float(maximum):
            report.warning(path, f'posiciones {side} actuales superan max reportado')


def _audit_rebalance(path, data, report):
    if not data or not data.get('pending'):
        return
    for field in ('pending_reason', 'last_check', 'direction', 'amount'):
        if data.get(field) in (None, ''):
            report.error(path, f'pending=true sin {field}')
            report.missing(path, field)
    attempts = data.get('attempts')
    if attempts in (None, 0, '0') and not data.get('blocked_reason'):
        report.error(path, 'pending=true con attempts=0 sin blocked_reason')
        report.missing(path, 'blocked_reason')
    if _looks_binance_error(data):
        for field in ('last_http_status', 'last_binance_code', 'last_raw_body'):
            if data.get(field) in (None, ''):
                report.warning(path, f'error Binance sin {field}')
                report.missing(path, field)


def _collect_trade_ids(records):
    return {record.get('trade_id') for record in records if isinstance(record, dict) and record.get('trade_id')}


def audit_project(project_dir=PROJECT_DIR):
    report = AuditReport()
    trading_dir = _project_path(project_dir, 'trading')
    history_dir = _project_path(project_dir, 'data', 'history')

    trade_analytics = _read_jsonl(_project_path(trading_dir, 'trade_analytics.jsonl'), report, required=True)
    decision_snapshots = _read_jsonl(_project_path(trading_dir, 'decision_snapshots.jsonl'), report, required=True)
    bot_state = _read_json(_project_path(trading_dir, 'bot_state.json'), report, required=True)

    history_jsonl_paths = sorted(glob.glob(_project_path(history_dir, '*.jsonl')))
    history_records = {}
    for path in history_jsonl_paths:
        history_records[path] = _read_jsonl(path, report)

    trades_history = history_records.get(_project_path(history_dir, 'trades.jsonl'), [])
    all_trade_records = trade_analytics + trades_history
    trade_ids = _collect_trade_ids(all_trade_records)
    if trade_analytics:
        _audit_trade_records(_project_path(trading_dir, 'trade_analytics.jsonl'), trade_analytics, report)
    if trades_history:
        _audit_trade_records(_project_path(history_dir, 'trades.jsonl'), trades_history, report)

    features_path = _project_path(history_dir, 'features.jsonl')
    if features_path in history_records:
        _audit_feature_records(features_path, history_records[features_path], report, trade_ids=trade_ids)

    ledger_path = _project_path(history_dir, 'capital_ledger.jsonl')
    if ledger_path in history_records:
        _audit_capital_ledger(ledger_path, history_records[ledger_path], report)

    _audit_bot_state(_project_path(trading_dir, 'bot_state.json'), bot_state, report)

    for rebalance_path in (
        _project_path(history_dir, 'rebalance_status.json'),
        _project_path(project_dir, 'rebalance_status.json'),
    ):
        _audit_rebalance(rebalance_path, _read_json(rebalance_path, report), report)

    if not decision_snapshots:
        report.recommendations.add('Verificar que decision_snapshots.jsonl se este generando durante los ciclos.')
    if report.totals_by_type:
        report.recommendations.add('Revisar totales del Capital Ledger antes de integrar PnL ajustado.')
    if report.possible_false_positives:
        report.recommendations.add('Posible bug de auditor: revisar clasificacion de cierres parciales/importados antes de reparar datos.')
    if report.incomplete_examples:
        report.recommendations.add('Distinguir datos historicos antiguos de bug de recoleccion antes de migrar o rellenar campos.')
    if any('features.jsonl' in path for path in report.missing_fields):
        report.recommendations.add('Feature Store: si faltantes aparecen en registros recientes, revisar recoleccion; si son antiguos, considerar migracion.')
    if any('bot_state.json' in path for path in report.missing_fields):
        report.recommendations.add('Bot State: si faltan aliases BTC en estado reciente, revisar persistencia de market.btc_price/btc_change_4h.')
    if any('rebalance_status.json' in path for path in report.missing_fields):
        report.recommendations.add('Rebalance: validar si el faltante corresponde a error Binance real o estado bloqueado sin transferencia.')
    if report.errors:
        report.recommendations.add('Corregir errores criticos antes de usar estos datos para decisiones o reporting.')
    return report


def format_report(report):
    lines = [
        'DATA QUALITY AUDIT',
        '',
        f'Archivos revisados: {report.files_checked}',
        f'Registros revisados: {report.records_checked}',
        f'Errores criticos: {len(report.errors)}',
        f'Warnings: {len(report.warnings)}',
        '',
        'Campos faltantes:',
    ]
    if report.missing_fields:
        for path, counter in sorted(report.missing_fields.items()):
            fields = ', '.join(f'{field}={count}' for field, count in sorted(counter.items()))
            lines.append(f'- {path}: {fields}')
    else:
        lines.append('- ninguno')
    lines.extend(['', 'Completitud:'])
    if report.total_records:
        for path, total in sorted(report.total_records.items()):
            complete = report.complete_records.get(path, 0)
            pct = (complete / total * 100) if total else 0
            lines.append(f'- {path}: {complete}/{total} ({pct:.1f}%)')
    else:
        lines.append('- sin registros auditables')
    if report.totals_by_type:
        lines.extend(['', 'Capital Ledger totales por tipo:'])
        for movement_type, amount in sorted(report.totals_by_type.items()):
            lines.append(f'- {movement_type}: {amount:.8f}')
    lines.extend(['', 'Ejemplos de errores criticos:'])
    if report.critical_examples:
        for item in report.critical_examples:
            details = [
                f'path={item.get("path")}',
                f'line={item.get("line")}',
                f'trade_id={item.get("trade_id")}',
                f'symbol={item.get("symbol")}',
                f'side={item.get("side")}',
                f'status={item.get("status")}',
                f'event_type={item.get("event_type")}',
                f'timestamp={item.get("timestamp")}',
                f'pnl={item.get("pnl_usdt")}',
                f'partial={item.get("is_partial")}',
            ]
            lines.append(f'- {item.get("message")} | ' + ' | '.join(details))
            if item.get('fields'):
                lines.append(f'  campos: {item.get("fields")}')
    else:
        lines.append('- ninguno')
    lines.extend(['', 'Posibles falsos positivos:'])
    if report.possible_false_positives:
        for item in report.possible_false_positives:
            lines.append(
                f'- {item.get("message")} | path={item.get("path")} line={item.get("line")} '
                f'trade_id={item.get("trade_id")} base={_base_trade_id(item.get("trade_id"))}'
            )
    else:
        lines.append('- ninguno')
    if report.incomplete_examples:
        lines.extend(['', 'Ultimos registros incompletos por archivo:'])
        for path, examples in sorted(report.incomplete_examples.items()):
            lines.append(f'- {path}:')
            for example in examples[-10:]:
                lines.append(
                    f'  line={example.get("line")} timestamp={example.get("timestamp")} '
                    f'age={example.get("age")} trade_id={example.get("trade_id")} '
                    f'symbol={example.get("symbol")} missing={example.get("missing")}'
                )
    lines.extend(['', 'Errores criticos:'])
    lines.extend([f'- {item}' for item in report.errors] or ['- ninguno'])
    lines.extend(['', 'Warnings:'])
    lines.extend([f'- {item}' for item in report.warnings[:50]] or ['- ninguno'])
    if len(report.warnings) > 50:
        lines.append(f'- ... {len(report.warnings) - 50} warnings adicionales')
    lines.extend(['', 'Recomendaciones:'])
    lines.extend([f'- {item}' for item in sorted(report.recommendations)] or ['- ninguna'])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Audit BinanceBot collected data quality.')
    parser.add_argument('--project-dir', default=PROJECT_DIR, help='Project root directory')
    args = parser.parse_args(argv)
    report = audit_project(args.project_dir)
    print(format_report(report))
    return 1 if report.errors else 0


if __name__ == '__main__':
    sys.exit(main())
