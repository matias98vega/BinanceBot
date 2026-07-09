#!/usr/bin/env python3
"""
Rebalanceo automático de capital entre Spot y Futures según contexto de mercado.

Lógica:
  - bearish  → futures 65% / spot 35%  (más capital para shorts)
  - bullish  → spot 65% / futures 35%  (más capital para longs)
  - neutral  → spot 50% / futures 50%

Filosofía de cambio de tendencia:
  Cuando el mercado cambia de dirección, NO se fuerza el rebalanceo inmediato.
  Las posiciones abiertas (de la tendencia anterior) se dejan correr hasta su
  cierre natural (TP/SL). El capital que liberan se transfiere progresivamente
  a la wallet correcta. Esto evita sacrificar riesgo por operación en días
  normales para resolver un problema que ocurre una vez cada varios días.

  El rebalanceo "paciente" trabaja sobre el capital LIBRE disponible en cada
  momento. El target se calcula sobre el capital TOTAL (libre + en posiciones)
  para saber hacia dónde ir, pero solo transfiere lo que está disponible ahora.
"""

import sys, os, logging, json
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(__file__))
import utils, config, market, capital_manager, decision_timeline, binance_client
from config_loader import PROJECT_DIR

BINANCE = binance_client.get_default_client()
REBALANCE_STATUS_FILE = os.path.join(PROJECT_DIR, 'data', 'history', 'rebalance_status.json')


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw in (None, ''):
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)

# ── Parámetros ────────────────────────────────────────────────────────────────
RATIO_BEARISH_FUTURES      = 0.65   # futures recibe 65% cuando mercado bajista
RATIO_VERY_BEARISH_FUTURES = 0.80   # futures recibe 80% cuando bajista >3 días consecutivos
VERY_BEARISH_DAYS          = 3.0    # umbral de días consecutivos bajistas
RATIO_BULLISH_SPOT         = 0.65   # spot recibe 65% cuando mercado alcista
RATIO_VERY_BULLISH_SPOT    = 0.80   # spot recibe 80% cuando alcista >3 días consecutivos
VERY_BULLISH_DAYS          = 3.0    # umbral de días consecutivos alcistas
REBALANCE_MIN_USDT         = 2.0    # no transferir menos de $2
REBALANCE_MIN_WALLET       = _env_float('REBALANCE_MIN_WALLET_USDT', 0.0)  # reserva minima opcional por wallet
REBALANCE_TRANSFER_BUFFER_USDT = _env_float('REBALANCE_TRANSFER_BUFFER_USDT', 0.10)  # colchon para evitar -5013 por saldo libre exacto
REBALANCE_ALIGNMENT_TOLERANCE_USDT = _env_float('REBALANCE_ALIGNMENT_TOLERANCE_USDT', 0.20)  # tolerancia para reconciliar estado pendiente
_LAST_FUTURES_CAPITAL_DETAILS = {}


def _rebalance_log(message, level=None):
    line = f'REBALANCE {message}'
    try:
        log_level = (level or 'WARNING').upper()
        if log_level == 'ERROR':
            logging.error(line)
        elif log_level == 'INFO':
            logging.info(line)
        else:
            logging.warning(line)
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass
    try:
        upper = str(message).upper()
        if upper.startswith('ERROR'):
            level, event = 'ERROR', 'rebalance_error'
        elif upper.startswith('SKIP'):
            level, event = 'INFO', 'rebalance_skip'
        elif upper.startswith('TRANSFER'):
            level, event = 'INFO', 'rebalance_transfer'
        else:
            level, event = 'INFO', 'rebalance_check'
        decision_timeline.record_rebalance_event(event, line, level=level, details={'raw': message})
    except Exception:
        pass


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _safe_float(value):
    try:
        return round(float(value), 8)
    except (TypeError, ValueError):
        return None


def _futures_position_margin(account):
    if not isinstance(account, dict):
        return 0.0
    value = _safe_float(account.get('totalPositionInitialMargin'))
    if value is not None:
        return value
    positions = account.get('positions') if isinstance(account.get('positions'), list) else []
    total = 0.0
    for position in positions:
        if not isinstance(position, dict):
            continue
        amount = _safe_float(position.get('positionAmt'))
        if amount is None or abs(amount) <= 0:
            continue
        total += (
            _safe_float(position.get('positionInitialMargin')) or
            _safe_float(position.get('initialMargin')) or
            0.0
        )
    return round(total, 8)


def _active_futures_positions_count(account):
    positions = account.get('positions') if isinstance(account, dict) and isinstance(account.get('positions'), list) else []
    count = 0
    for position in positions:
        if not isinstance(position, dict):
            continue
        amount = _safe_float(position.get('positionAmt'))
        if amount is not None and abs(amount) > 0:
            count += 1
    return count


def _direction_arrow(direction):
    labels = {
        'SPOT_TO_FUTURES': 'Spot -> Futures',
        'FUTURES_TO_SPOT': 'Futures -> Spot',
    }
    return labels.get(str(direction or '').upper(), str(direction or 'UNKNOWN'))


def read_rebalance_status():
    try:
        with open(REBALANCE_STATUS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logging.warning('REBALANCE STATUS read failed path=%s error=%s', REBALANCE_STATUS_FILE, exc)
        return {}


def _write_rebalance_status(payload):
    try:
        os.makedirs(os.path.dirname(REBALANCE_STATUS_FILE), exist_ok=True)
        tmp = f'{REBALANCE_STATUS_FILE}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, REBALANCE_STATUS_FILE)
        try:
            os.chmod(REBALANCE_STATUS_FILE, 0o600)
        except Exception:
            pass
        return True
    except Exception as exc:
        logging.warning('REBALANCE STATUS write failed path=%s error=%s', REBALANCE_STATUS_FILE, exc)
        return False


def clear_rebalance_status(recovery=None):
    payload = {
        'pending': False,
        'direction': None,
        'amount': None,
        'first_failure': None,
        'last_attempt': _now_iso(),
        'attempts': 0,
        'last_http_status': None,
        'last_binance_code': None,
        'last_message': None,
        'last_raw_body': None,
    }
    if isinstance(recovery, dict):
        payload.update({
            'recovered': True,
            'recovered_direction': recovery.get('direction'),
            'recovered_attempt': recovery.get('attempt'),
            'original_amount': _safe_float(recovery.get('original_amount')),
            'final_amount': _safe_float(recovery.get('final_amount')),
            'buffer_applied': _safe_float(recovery.get('buffer_applied')),
        })
    _write_rebalance_status(payload)
    return payload


def _failure_attempts(previous, direction):
    if previous.get('pending') and previous.get('direction') == direction:
        try:
            return int(previous.get('attempts') or 0) + 1
        except (TypeError, ValueError):
            return 1
    return 1


def _existing_attempts(previous, direction):
    if previous.get('pending') and previous.get('direction') == direction:
        try:
            return int(previous.get('attempts') or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _record_rebalance_pending_check(direction, amount, pending_reason, blocked_reason=None, context=None, status='PENDING', only_existing=False):
    previous = read_rebalance_status()
    if only_existing and not previous.get('pending'):
        return None
    now = _now_iso()
    effective_direction = previous.get('direction') if previous.get('pending') and direction in (None, 'NONE') else direction
    effective_amount = previous.get('amount') if previous.get('pending') and amount is None else amount
    same_direction = previous.get('pending') and previous.get('direction') == effective_direction
    attempts = _existing_attempts(previous, effective_direction)
    payload = {
        'pending': True,
        'direction': effective_direction,
        'amount': _safe_float(effective_amount),
        'pending_reason': pending_reason,
        'blocked_reason': blocked_reason,
        'last_check': now,
        'last_attempt': previous.get('last_attempt') if same_direction else None,
        'attempts': attempts,
        'first_failure': previous.get('first_failure') if same_direction else None,
        'last_http_status': previous.get('last_http_status') if same_direction else None,
        'last_binance_code': previous.get('last_binance_code') if same_direction else None,
        'last_message': previous.get('last_message') if same_direction else pending_reason,
        'last_error': previous.get('last_error') if same_direction else None,
        'last_raw_body': previous.get('last_raw_body') if same_direction else None,
    }
    if isinstance(context, dict):
        payload.update({key: _safe_float(value) if isinstance(value, (int, float)) else value for key, value in context.items()})
    _write_rebalance_status(payload)
    log_fn = logging.warning if blocked_reason or str(status or '').upper() == 'BLOCKED' else logging.info
    log_fn(
        'REBALANCE PENDING CHECK status=%s direction=%s amount=%s attempts=%s pending_reason=%s blocked_reason=%s context=%s',
        status, _direction_arrow(effective_direction), effective_amount, attempts, pending_reason, blocked_reason, context or {},
    )
    position_margin = _safe_float((context or {}).get('position_margin') or (context or {}).get('futures_position_margin'))
    event_name = 'rebalance_blocked' if blocked_reason else 'rebalance_pending_created'
    if blocked_reason and position_margin and position_margin > 0 and str(blocked_reason).startswith(('active_shorts', 'insufficient_futures_free')):
        event_name = 'rebalance_blocked_margin'
    if previous.get('pending') and not blocked_reason:
        event_name = 'rebalance_pending_check'
    try:
        decision_timeline.record_rebalance_event(
            event_name,
            (
                f'{_direction_arrow(effective_direction)} pendiente: {pending_reason}'
                + (f' | bloqueo={blocked_reason}' if blocked_reason else '')
            ),
            level='WARNING' if blocked_reason else 'INFO',
            details={
                'direction': effective_direction,
                'amount': _safe_float(effective_amount),
                'attempts': attempts,
                'reason': pending_reason,
                'pending_reason': pending_reason,
                'blocked_reason': blocked_reason,
                'last_check': now,
                **(context or {}),
            },
        )
    except Exception:
        pass
    return payload


def _record_rebalance_attempt(direction, amount, attempt, context=None):
    logging.info(
        'REBALANCE ATTEMPT direction=%s amount=%s attempt=%s context=%s',
        _direction_arrow(direction), amount, attempt, context or {},
    )
    try:
        decision_timeline.record_rebalance_event(
            'rebalance_attempt',
            f'{_direction_arrow(direction)} intento #{attempt}: {float(amount):.2f} USDT',
            level='INFO',
            details={
                'direction': direction,
                'amount': _safe_float(amount),
                'attempts': attempt,
                **(context or {}),
            },
        )
    except Exception:
        pass


def _record_rebalance_failure(direction, amount, error, payload, extra=None):
    details = utils.extract_http_error_details(error)
    details['endpoint'] = details.get('endpoint') or '/sapi/v1/asset/transfer'
    details['method'] = details.get('method') or 'POST'
    details['payload'] = utils.safe_order_context(payload or details.get('payload') or {})
    previous = read_rebalance_status()
    now = _now_iso()
    attempts = _failure_attempts(previous, direction)
    status = {
        'pending': True,
        'direction': direction,
        'amount': _safe_float(amount),
        'first_failure': previous.get('first_failure') if previous.get('pending') and previous.get('direction') == direction else now,
        'last_check': now,
        'pending_reason': 'Transferencia rechazada por Binance',
        'blocked_reason': None,
        'last_attempt': now,
        'attempts': attempts,
        'last_http_status': details.get('status'),
        'last_binance_code': details.get('code'),
        'last_message': details.get('msg') or str(error),
        'last_error': details.get('msg') or str(error),
        'last_raw_body': details.get('raw_body'),
        'endpoint': details.get('endpoint'),
        'method': details.get('method'),
        'payload': details.get('payload'),
    }
    if isinstance(extra, dict):
        status.update(extra)
    _write_rebalance_status(status)
    logging.error(
        'REBALANCE HTTP ERROR direction=%s amount=%s endpoint=%s method=%s status=%s code=%s msg=%s payload=%s raw_body=%s',
        _direction_arrow(direction),
        amount,
        details.get('endpoint'),
        details.get('method'),
        details.get('status'),
        details.get('code'),
        details.get('msg'),
        details.get('payload'),
        details.get('raw_body'),
    )
    try:
        decision_timeline.record_rebalance_event(
            'rebalance_error',
            (
                f'{_direction_arrow(direction)} {float(amount):.2f} USDT '
                f'intento #{attempts}: HTTP {details.get("status")} '
                f'{details.get("msg") or str(error)}'
            ),
            level='ERROR',
            details={
                'direction': direction,
                'amount': _safe_float(amount),
                'attempts': attempts,
                'reason': details.get('msg') or str(error),
                'http_status': details.get('status'),
                'binance_code': details.get('code'),
                'binance_msg': details.get('msg'),
                'raw_body': details.get('raw_body'),
                'endpoint': details.get('endpoint'),
                'method': details.get('method'),
                'payload': details.get('payload'),
            },
        )
    except Exception:
        pass
    return status, details


def _alignment_tolerance(tolerance=None):
    value = REBALANCE_ALIGNMENT_TOLERANCE_USDT if tolerance is None else tolerance
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def reconcile_rebalance_status_if_aligned(spot_actual, fut_actual, target_spot, target_fut, tolerance=None):
    status = read_rebalance_status()
    if not isinstance(status, dict) or not status.get('pending'):
        return None
    tol = _alignment_tolerance(tolerance)
    diff_spot = abs(float(spot_actual or 0) - float(target_spot or 0))
    diff_fut = abs(float(fut_actual or 0) - float(target_fut or 0))
    aligned = diff_spot <= tol and diff_fut <= tol
    log_fn = logging.info if aligned else logging.warning
    log_fn(
        'REBALANCE RECONCILE CHECK target_spot=%s target_futures=%s real_spot=%s real_futures=%s diff_spot=%s diff_futures=%s tolerance=%s aligned=%s',
        target_spot, target_fut, spot_actual, fut_actual, diff_spot, diff_fut, tol, aligned,
    )
    if not aligned:
        _record_rebalance_pending_check(
            status.get('direction'),
            status.get('amount'),
            'Capital aun fuera de tolerancia de alineacion',
            context={
                'spot_real': _safe_float(spot_actual),
                'futures_real': _safe_float(fut_actual),
                'target_spot': _safe_float(target_spot),
                'target_futures': _safe_float(target_fut),
                'diff_spot': _safe_float(diff_spot),
                'diff_futures': _safe_float(diff_fut),
                'tolerance': _safe_float(tol),
            },
            only_existing=True,
        )
        return None

    now = _now_iso()
    resolved = {
        'pending': False,
        'direction': None,
        'amount': None,
        'first_failure': status.get('first_failure'),
        'last_attempt': status.get('last_attempt'),
        'attempts': 0,
        'last_http_status': None,
        'last_binance_code': None,
        'last_message': None,
        'last_raw_body': None,
        'last_resolved_at': now,
        'resolved_reason': 'capital_already_aligned',
        'last_direction': status.get('direction'),
        'last_amount': status.get('amount'),
        'last_attempts': status.get('attempts'),
        'reconciled': True,
        'reconciled_at': now,
        'diff_spot': _safe_float(diff_spot),
        'diff_futures': _safe_float(diff_fut),
        'tolerance': _safe_float(tol),
        'spot_actual': _safe_float(spot_actual),
        'futures_actual': _safe_float(fut_actual),
        'target_spot': _safe_float(target_spot),
        'target_futures': _safe_float(target_fut),
    }
    _write_rebalance_status(resolved)
    logging.info(
        'REBALANCE RECONCILED reason=capital_already_aligned target_spot=%s target_futures=%s real_spot=%s real_futures=%s diff_spot=%s diff_futures=%s tolerance=%s',
        target_spot, target_fut, spot_actual, fut_actual, diff_spot, diff_fut, tol,
    )
    try:
        decision_timeline.record_rebalance_event(
            'rebalance_reconciled',
            'Rebalance reconciliado: capital ya alineado.',
            level='INFO',
            details={
                'reason': 'capital_already_aligned',
                'diff_spot': _safe_float(diff_spot),
                'diff_futures': _safe_float(diff_fut),
                'tolerance': _safe_float(tol),
                'spot_actual': _safe_float(spot_actual),
                'futures_actual': _safe_float(fut_actual),
                'target_spot': _safe_float(target_spot),
                'target_futures': _safe_float(target_fut),
            },
        )
    except Exception:
        pass
    return resolved


def _format_transfer_error(direction, details, error):
    label = _direction_arrow(direction)
    if details.get('status') or details.get('code') is not None or details.get('msg'):
        return (
            f'Error al transferir {label}: HTTP {details.get("status")} '
            f'code={details.get("code")} msg={details.get("msg") or str(error)}'
        )
    return f'Error al transferir {label}: {error}'


def _transferable_amount(required_amount, source_free, wallet_min=None):
    reserve = REBALANCE_MIN_WALLET if wallet_min is None else float(wallet_min or 0)
    reserve = max(0.0, reserve)
    return round(min(float(required_amount or 0), float(source_free or 0) - reserve), 2)


def _transfer_buffer(buffer=None):
    value = REBALANCE_TRANSFER_BUFFER_USDT if buffer is None else buffer
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _apply_transfer_buffer(amount, buffer=None):
    return round(max(0.0, float(amount or 0) - _transfer_buffer(buffer)), 2)


def _is_insufficient_transfer(details):
    msg = str((details or {}).get('msg') or '').lower()
    return (details or {}).get('code') == -5013 and 'insufficient balance' in msg


def _transfer_type(direction):
    return 'MAIN_UMFUTURE' if direction == 'SPOT_TO_FUTURES' else 'UMFUTURE_MAIN'


def _record_rebalance_recovered(direction, original_amount, final_amount, buffer, attempt):
    try:
        decision_timeline.record_rebalance_event(
            'rebalance_recovered',
            (
                f'Rebalance recuperado automaticamente {_direction_arrow(direction)} '
                f'intento {attempt}: original={float(original_amount):.2f} '
                f'final={float(final_amount):.2f} buffer={float(buffer):.2f}'
            ),
            level='INFO',
            details={
                'direction': direction,
                'attempt': attempt,
                'original_amount': _safe_float(original_amount),
                'final_amount': _safe_float(final_amount),
                'buffer_applied': _safe_float(buffer),
            },
        )
    except Exception:
        pass


def _transfer_with_recovery(direction, calculated_amount, context=None):
    buffer = _transfer_buffer()
    attempt_1 = round(float(calculated_amount or 0), 2)
    if attempt_1 <= 0:
        return False, None, {'attempts': 0, 'final_amount': 0.0, 'buffer_applied': buffer}
    transfer_type = _transfer_type(direction)
    payload = {'type': transfer_type, 'asset': 'USDT', 'amount': str(attempt_1)}
    logging.info(
        'REBALANCE TRANSFER attempt=1 direction=%s calculated_amount=%s buffer=%s attempt_1=%s',
        _direction_arrow(direction), calculated_amount, buffer, attempt_1,
    )
    _record_rebalance_attempt(direction, attempt_1, 1, context=context)
    try:
        BINANCE.spot_signed('POST', '/sapi/v1/asset/transfer', payload)
        clear_rebalance_status()
        logging.info(
            'REBALANCE TRANSFER result=success direction=%s calculated_amount=%s buffer=%s attempt_1=%s final_amount=%s',
            _direction_arrow(direction), calculated_amount, buffer, attempt_1, attempt_1,
        )
        return True, attempt_1, {'attempts': 1, 'final_amount': attempt_1, 'buffer_applied': buffer}
    except Exception as exc:
        first_status, first_details = _record_rebalance_failure(
            direction,
            attempt_1,
            exc,
            payload,
            extra={
                'buffer_applied': _safe_float(buffer),
                'requested_amount': _safe_float(attempt_1),
                **(context or {}),
            },
        )
        if not _is_insufficient_transfer(first_details):
            logging.warning(
                'REBALANCE TRANSFER result=failed direction=%s calculated_amount=%s buffer=%s attempt_1=%s final_amount=%s code=%s msg=%s',
                _direction_arrow(direction), calculated_amount, buffer, attempt_1, attempt_1,
                first_details.get('code'), first_details.get('msg'),
            )
            return False, _format_transfer_error(direction, first_details, exc), {'attempts': 1, 'final_amount': attempt_1, 'buffer_applied': buffer, 'status': first_status}

        attempt_2 = _apply_transfer_buffer(attempt_1, buffer)
        if attempt_2 <= 0:
            logging.warning(
                'REBALANCE TRANSFER result=failed_no_retry direction=%s calculated_amount=%s buffer=%s attempt_1=%s attempt_2=%s',
                _direction_arrow(direction), calculated_amount, buffer, attempt_1, attempt_2,
            )
            return False, _format_transfer_error(direction, first_details, exc), {'attempts': 1, 'final_amount': attempt_1, 'buffer_applied': buffer, 'status': first_status}

        payload_2 = {'type': transfer_type, 'asset': 'USDT', 'amount': str(attempt_2)}
        logging.info(
            'REBALANCE TRANSFER attempt=2 direction=%s calculated_amount=%s buffer=%s attempt_1=%s attempt_2=%s',
            _direction_arrow(direction), calculated_amount, buffer, attempt_1, attempt_2,
        )
        _record_rebalance_attempt(direction, attempt_2, 2, context=context)
        try:
            BINANCE.spot_signed('POST', '/sapi/v1/asset/transfer', payload_2)
            clear_rebalance_status({
                'direction': direction,
                'attempt': 2,
                'original_amount': calculated_amount,
                'final_amount': attempt_2,
                'buffer_applied': buffer,
            })
            _record_rebalance_recovered(direction, calculated_amount, attempt_2, buffer, 2)
            logging.info(
                'REBALANCE TRANSFER result=recovered direction=%s calculated_amount=%s buffer=%s attempt_1=%s attempt_2=%s final_amount=%s',
                _direction_arrow(direction), calculated_amount, buffer, attempt_1, attempt_2, attempt_2,
            )
            return True, attempt_2, {'attempts': 2, 'final_amount': attempt_2, 'buffer_applied': buffer, 'recovered': True}
        except Exception as retry_exc:
            retry_status, retry_details = _record_rebalance_failure(
                direction,
                attempt_2,
                retry_exc,
                payload_2,
                extra={
                    'buffer_applied': _safe_float(buffer),
                    'requested_amount': _safe_float(attempt_1),
                    'retried_amount': _safe_float(attempt_2),
                    **(context or {}),
                },
            )
            logging.warning(
                'REBALANCE TRANSFER result=failed direction=%s calculated_amount=%s buffer=%s attempt_1=%s attempt_2=%s final_amount=%s code=%s msg=%s',
                _direction_arrow(direction), calculated_amount, buffer, attempt_1, attempt_2, attempt_2,
                retry_details.get('code'), retry_details.get('msg'),
            )
            try:
                decision_timeline.record_rebalance_event(
                    'rebalance_pending',
                    (
                        f'Rebalance pendiente {_direction_arrow(direction)}: '
                        f'motivo={retry_details.get("msg") or retry_exc} '
                        f'solicitado={attempt_1:.2f} reintentado={attempt_2:.2f}'
                    ),
                    level='ERROR',
                    details={
                        'direction': direction,
                        'requested_amount': _safe_float(attempt_1),
                        'retried_amount': _safe_float(attempt_2),
                        'buffer_applied': _safe_float(buffer),
                        'attempts': 2,
                        'http_status': retry_details.get('status'),
                        'binance_code': retry_details.get('code'),
                        'binance_msg': retry_details.get('msg'),
                    },
                )
            except Exception:
                pass
            return False, _format_transfer_error(direction, retry_details, retry_exc), {'attempts': 2, 'final_amount': attempt_2, 'buffer_applied': buffer, 'status': retry_status}


def _capital_total(state):
    """Capital total = libre + comprometido en posiciones (ambas wallets)."""
    import urllib.error, time, logging
    global _LAST_FUTURES_CAPITAL_DETAILS
    
    spot_free   = BINANCE.get_usdt_spot()
    
    # Futures: walletBalance + uPnL (balance real, igual al resumen diario)
    # Con reintentos ante errores transitorios de API
    account = None
    last_err = None
    for _attempt in range(3):
        try:
            account = BINANCE.fut_signed('GET', '/fapi/v2/account', {})
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if _attempt < 2:
                _delay = 5 * (_attempt + 1)  # 5s, 10s
                logging.warning(f'Rebalance: intento {_attempt+1} fallido ({e}), reintentando en {_delay}s')
                time.sleep(_delay)
    if account is None:
        raise last_err
    
    fut_wallet  = float(account.get('totalWalletBalance', 0))
    fut_upnl    = float(account.get('totalUnrealizedProfit', 0))
    fut_total   = fut_wallet + fut_upnl
    spot_in_pos = sum(
        p['entry_price'] * p['quantity']
        for p in state.get('positions', []) if p['direction'] == 'long'
    )
    # Para rebalanceo, fut_free = disponible para nuevas posiciones
    fut_free    = float(account.get('availableBalance', 0))
    position_margin = _futures_position_margin(account)
    _LAST_FUTURES_CAPITAL_DETAILS = {
        'wallet_balance': _safe_float(fut_wallet),
        'available_balance': _safe_float(fut_free),
        'position_margin': _safe_float(position_margin),
        'total_initial_margin': _safe_float(account.get('totalInitialMargin')),
        'open_positions': _active_futures_positions_count(account),
    }
    return spot_free, fut_free, spot_in_pos, fut_total


def _trend_changed(state, new_trend):
    """Devuelve True si la tendencia actual es distinta a la del último rebalanceo."""
    last = state.get('last_rebalance_trend', 'neutral')
    return last != new_trend


def _count_bearish_days():
    """
    Cuenta cuántos días consecutivos lleva BTC en tendencia bajista
    (precio < EMA20_4h Y precio < EMA50_4h en velas 4h consecutivas desde ahora).
    """
    try:
        k4h = BINANCE.get_klines('BTCUSDT', interval='4h', limit=60)
        closes = [float(k[4]) for k in k4h]
        ema20  = utils.ema(closes, 20)
        ema50  = utils.ema(closes, 50)
        bearish_candles = 0
        for i in range(len(closes) - 1, -1, -1):
            if closes[i] < ema20[i] and closes[i] < ema50[i]:
                bearish_candles += 1
            else:
                break
        return bearish_candles * 4 / 24
    except Exception:
        return 0.0


def _count_bullish_days():
    """
    Cuenta cuántos días consecutivos lleva BTC en tendencia alcista
    (precio > EMA20_4h Y precio > EMA50_4h en velas 4h consecutivas desde ahora).
    """
    try:
        k4h = BINANCE.get_klines('BTCUSDT', interval='4h', limit=60)
        closes = [float(k[4]) for k in k4h]
        ema20  = utils.ema(closes, 20)
        ema50  = utils.ema(closes, 50)
        bullish_candles = 0
        for i in range(len(closes) - 1, -1, -1):
            if closes[i] > ema20[i] and closes[i] > ema50[i]:
                bullish_candles += 1
            else:
                break
        return bullish_candles * 4 / 24
    except Exception:
        return 0.0


def rebalance(state, btc_ctx=None):
    """
    Evalúa si corresponde rebalancear y ejecuta la transferencia si es necesario.
    Retorna (transferido: bool, mensaje: str)
    """
    if btc_ctx is None:
        btc_ctx = market.get_btc_context()

    trend = btc_ctx.get('trend', 'neutral')

    spot_free, fut_free, spot_in_pos, fut_actual = _capital_total(state)

    # Capital total real (libre + atrapado en posiciones)
    spot_actual   = spot_free + spot_in_pos
    total_capital = spot_actual + fut_actual

    # Capital libre para operar
    total_free = spot_free + fut_free

    if REBALANCE_MIN_WALLET > 0 and total_free < REBALANCE_MIN_WALLET * 2:
        _record_rebalance_pending_check(
            'NONE',
            None,
            f'Capital libre insuficiente para rebalancear (${total_free:.2f})',
            blocked_reason='free_capital_below_wallet_minimum',
            context={'trend': trend, 'total_free': total_free, 'spot_free': spot_free, 'fut_free': fut_free},
            status='BLOCKED',
            only_existing=True,
        )
        _rebalance_log(
            f'SKIP: reason=free capital below wallet minimum regime={trend} '
            f'total_free={total_free:.2f} spot_free={spot_free:.2f} fut_free={fut_free:.2f}'
        )
        return False, f'Capital libre insuficiente para rebalancear (${total_free:.2f})'

    # ── Targets calculados sobre capital TOTAL ────────────────────────────────
    # Modo direccional: concentrar 100% en la wallet de la tendencia
    if config.DIRECTIONAL_MODE and trend == 'bearish':
        ratio_fut = 1.0  # 100% futures
        label = f'direccional bearish → {ratio_fut*100:.0f}% futures'
        target_fut  = total_capital * ratio_fut
        target_spot = total_capital * (1 - ratio_fut)
    elif config.DIRECTIONAL_MODE and trend == 'bullish':
        ratio_spot = 1.0  # 100% spot
        label = f'direccional bullish → {ratio_spot*100:.0f}% spot'
        target_spot = total_capital * ratio_spot
        target_fut  = total_capital * (1 - ratio_spot)
    elif trend == 'bearish':
        # Detectar cuántos días consecutivos lleva bajista
        bearish_days = _count_bearish_days()
        if bearish_days >= VERY_BEARISH_DAYS:
            ratio_fut = RATIO_VERY_BEARISH_FUTURES
            label = f'muy bajista ({bearish_days:.1f} días) → {ratio_fut*100:.0f}% futures'
        else:
            ratio_fut = RATIO_BEARISH_FUTURES
            label = f'bearish ({bearish_days:.1f} días) → {ratio_fut*100:.0f}% futures'
        target_fut  = total_capital * ratio_fut
        target_spot = total_capital * (1 - ratio_fut)
    elif trend == 'bullish':
        if config.DIRECTIONAL_MODE:
            ratio_spot = 1.0  # 100% spot
            label = f'direccional bullish → {ratio_spot*100:.0f}% spot'
            target_spot = total_capital * ratio_spot
            target_fut  = total_capital * (1 - ratio_spot)
        else:
            bullish_days = _count_bullish_days()
            if bullish_days >= VERY_BULLISH_DAYS:
                ratio_spot = RATIO_VERY_BULLISH_SPOT
                label = f'muy alcista ({bullish_days:.1f} días) → {ratio_spot*100:.0f}% spot'
            else:
                ratio_spot = RATIO_BULLISH_SPOT
                label = f'bullish ({bullish_days:.1f} días) → {ratio_spot*100:.0f}% spot'
            target_spot = total_capital * ratio_spot
            target_fut  = total_capital * (1 - ratio_spot)
    else:
        target_spot = total_capital * 0.5
        target_fut  = total_capital * 0.5
        label = 'neutral → 50/50'

    # Capital actual real (libre + en posiciones) por wallet
    # spot_actual y fut_actual ya calculados arriba desde _capital_total
    diff_fut = target_fut - fut_actual   # positivo = futures tiene menos de lo que debería
    diff_spot = target_spot - spot_actual
    pending_to_futures = max(0.0, diff_fut)
    pending_to_spot = max(0.0, -diff_fut)
    futures_details = dict(_LAST_FUTURES_CAPITAL_DETAILS)
    _rebalance_log(
        f'CHECK: regime={trend} total={total_capital:.2f} spot_free={spot_free:.2f} '
        f'spot_actual={spot_actual:.2f} fut_actual={fut_actual:.2f} fut_free={fut_free:.2f} '
        f'target_spot={target_spot:.2f} target_fut={target_fut:.2f} '
        f'diff_spot={diff_spot:.2f} diff_fut={diff_fut:.2f}',
        level='INFO',
    )
    reconcile_rebalance_status_if_aligned(
        spot_actual=spot_actual,
        fut_actual=fut_actual,
        target_spot=target_spot,
        target_fut=target_fut,
    )

    if abs(diff_fut) < REBALANCE_MIN_USDT:
        _rebalance_log(
            f'SKIP: reason=balances aligned regime={trend} spot_actual={spot_actual:.2f} '
            f'fut_actual={fut_actual:.2f} diff_fut={diff_fut:.2f}',
            level='INFO',
        )
        return False, f'Balances ya alineados ({trend}): spot=${spot_actual:.2f} fut=${fut_actual:.2f}'

    # ── Cambio de tendencia: rebalanceo PACIENTE ──────────────────────────────
    # Si hay posiciones de la dirección "vieja" abiertas, no forzar nada.
    # Solo transferir el capital que ya está libre, progresivamente.
    trend_flipped = _trend_changed(state, trend)
    shorts_open = [p for p in state.get('positions', []) if p['direction'] == 'short']
    longs_open  = [p for p in state.get('positions', []) if p['direction'] == 'long']

    if diff_fut > 0:
        # Necesitamos más capital en futures (bearish) — mover Spot → Futures
        calculated_amount = _transferable_amount(diff_fut, spot_free)
        _rebalance_log(
            f'CHECK: direction=Spot->Futures calculated_amount={calculated_amount:.2f} '
            f'buffer={_transfer_buffer():.2f}',
            level='INFO',
        )
        try:
            capped_amount = capital_manager.cap_transfer_amount('FUTURES', fut_actual, calculated_amount)
        except Exception as e:
            _record_rebalance_pending_check(
                'SPOT_TO_FUTURES',
                calculated_amount,
                'Capital manager bloqueo rebalance hacia Futures',
                blocked_reason=str(e),
                context={'trend': trend, 'spot_free': spot_free, 'fut_actual': fut_actual, 'target_fut': target_fut},
                status='BLOCKED',
            )
            _rebalance_log(f'SKIP: reason=capital_manager error direction=Spot->Futures error={e}')
            return False, f'Capital limit: rebalanceo Spot->Futures bloqueado ({e})'
        if capped_amount < calculated_amount:
            _rebalance_log(f'CHECK: capital_manager capped Spot->Futures requested={calculated_amount:.2f} capped={capped_amount:.2f}')
            calculated_amount = round(capped_amount, 2)
            if calculated_amount < REBALANCE_MIN_USDT:
                _record_rebalance_pending_check(
                    'SPOT_TO_FUTURES',
                    calculated_amount,
                    'Capital manager dejo el monto bajo el minimo transferible',
                    blocked_reason='destination_wallet_limit_reached',
                    context={'trend': trend, 'requested_amount': capped_amount, 'threshold': REBALANCE_MIN_USDT},
                    status='BLOCKED',
                )
                _rebalance_log('SKIP: reason=capital_manager cap_transfer_amount returned 0')
                return False, 'Capital limit: no se transfiere a Futures porque la wallet destino ya alcanzo el limite configurado'
        amount = _apply_transfer_buffer(calculated_amount)
        _rebalance_log(
            f'CHECK: direction=Spot->Futures calculated_amount={calculated_amount:.2f} '
            f'buffer={_transfer_buffer():.2f} final_amount={amount:.2f}',
            level='INFO',
        )
        if amount < REBALANCE_MIN_USDT:
            if trend_flipped and longs_open:
                # Cambio a bearish pero hay longs viejos: esperar que cierren
                _record_rebalance_pending_check(
                    'SPOT_TO_FUTURES',
                    amount,
                    'Pendiente, pero no transferido porque hay longs activos reteniendo Spot',
                    blocked_reason='active_longs',
                    context={'trend': trend, 'active_longs': len(longs_open), 'spot_free': spot_free, 'calculated_amount': calculated_amount},
                    status='BLOCKED',
                )
                _rebalance_log(
                    f'SKIP: reason=trend flipped with active longs count={len(longs_open)} '
                    f'spot_free={spot_free:.2f} amount={amount:.2f}'
                )
                return False, (
                    f'⏳ Tendencia viró a BEARISH — esperando cierre de {len(longs_open)} long(s) ' 
                    f'para liberar capital spot. Rebalanceo progresivo en curso.'
                )
            _record_rebalance_pending_check(
                'SPOT_TO_FUTURES',
                amount,
                'Pendiente, pero no transferido por USDT libre insuficiente en Spot',
                blocked_reason='insufficient_spot_free',
                context={'trend': trend, 'spot_free': spot_free, 'calculated_amount': calculated_amount, 'threshold': REBALANCE_MIN_USDT},
                status='BLOCKED',
            )
            _rebalance_log(f'SKIP: reason=insufficient spot free spot_free={spot_free:.2f} amount={amount:.2f}')
            return False, f'No hay suficiente USDT libre en spot para transferir (${spot_free:.2f})'

        if longs_open and spot_free - amount < REBALANCE_MIN_WALLET:
            _record_rebalance_pending_check(
                'SPOT_TO_FUTURES',
                amount,
                'Pendiente, pero no transferido porque reduciria Spot bajo la reserva con longs activos',
                blocked_reason='active_longs_wallet_reserve',
                context={'trend': trend, 'active_longs': len(longs_open), 'spot_free': spot_free, 'wallet_min': REBALANCE_MIN_WALLET},
                status='BLOCKED',
            )
            _rebalance_log(f'SKIP: reason=active longs count={len(longs_open)} spot_free={spot_free:.2f} amount={amount:.2f}')
            return False, f'No se puede reducir spot: hay {len(longs_open)} long(s) activo(s)'

        try:
            _rebalance_log(f'TRANSFER: {amount:.2f} Spot -> Futures', level='INFO')
            transfer_ok, transfer_result, transfer_meta = _transfer_with_recovery('SPOT_TO_FUTURES', amount, context={
                'spot_real': _safe_float(spot_actual),
                'futures_real': _safe_float(fut_actual),
                'target_spot': _safe_float(target_spot),
                'target_futures': _safe_float(target_fut),
                'diff_spot': _safe_float(diff_spot),
                'diff_futures': _safe_float(diff_fut),
                'reason': 'rebalance_transfer_required',
            })
            if not transfer_ok:
                return False, transfer_result
            amount = transfer_result
            state['last_rebalance_trend'] = trend
            if trend_flipped:
                msg = (f'🔄 Rebalanceo parcial (viraje a BEARISH): ${amount:.2f} Spot → Futures ' 
                       f'| Quedan {len(longs_open)} longs — capital liberará al cerrar')
            else:
                msg = f'🔄 Rebalanceo ({label}): ${amount:.2f} USDT Spot → Futures'
            return True, msg
        except Exception as e:
            return False, f'Error al transferir Spot→Futures: {e}'

    else:
        # Necesitamos más capital en spot (bullish) — mover Futures → Spot
        calculated_amount = _transferable_amount(-diff_fut, fut_free)
        _rebalance_log(
            f'CHECK: direction=Futures->Spot calculated_amount={calculated_amount:.2f} '
            f'buffer={_transfer_buffer():.2f}',
            level='INFO',
        )
        try:
            capped_amount = capital_manager.cap_transfer_amount('SPOT', spot_actual, calculated_amount)
        except Exception as e:
            _record_rebalance_pending_check(
                'FUTURES_TO_SPOT',
                calculated_amount,
                'Capital manager bloqueo rebalance hacia Spot',
                blocked_reason=str(e),
                context={'trend': trend, 'fut_free': fut_free, 'spot_actual': spot_actual, 'target_spot': target_spot},
                status='BLOCKED',
            )
            _rebalance_log(f'SKIP: reason=capital_manager error direction=Futures->Spot error={e}')
            return False, f'Capital limit: rebalanceo Futures->Spot bloqueado ({e})'
        if capped_amount < calculated_amount:
            _rebalance_log(f'CHECK: capital_manager capped Futures->Spot requested={calculated_amount:.2f} capped={capped_amount:.2f}')
            calculated_amount = round(capped_amount, 2)
            if calculated_amount < REBALANCE_MIN_USDT:
                _record_rebalance_pending_check(
                    'FUTURES_TO_SPOT',
                    calculated_amount,
                    'Capital manager dejo el monto bajo el minimo transferible',
                    blocked_reason='destination_wallet_limit_reached',
                    context={'trend': trend, 'requested_amount': capped_amount, 'threshold': REBALANCE_MIN_USDT},
                    status='BLOCKED',
                )
                _rebalance_log('SKIP: reason=capital_manager cap_transfer_amount returned 0')
                return False, 'Capital limit: no se transfiere a Spot porque la wallet destino ya alcanzo el limite configurado'
        amount = _apply_transfer_buffer(calculated_amount)
        _rebalance_log(
            f'CHECK: direction=Futures->Spot calculated_amount={calculated_amount:.2f} '
            f'buffer={_transfer_buffer():.2f} final_amount={amount:.2f}',
            level='INFO',
        )
        if amount < REBALANCE_MIN_USDT:
            if trend_flipped and shorts_open:
                # Cambio a bullish pero hay shorts viejos: esperar que cierren
                _record_rebalance_pending_check(
                    'FUTURES_TO_SPOT',
                    pending_to_spot,
                    'Pendiente, pero no transferido porque hay shorts activos reteniendo Futures',
                    blocked_reason='active_shorts',
                    context={
                        'trend': trend,
                        'active_shorts': len(shorts_open),
                        'fut_free': fut_free,
                        'pending_amount': pending_to_spot,
                        'transferable_amount': amount,
                        'calculated_amount': calculated_amount,
                        'available_balance': futures_details.get('available_balance', fut_free),
                        'position_margin': futures_details.get('position_margin'),
                        'wallet_balance': futures_details.get('wallet_balance'),
                    },
                    status='BLOCKED',
                )
                _rebalance_log(
                    f'SKIP: reason=trend flipped with active shorts count={len(shorts_open)} '
                    f'fut_free={fut_free:.2f} amount={amount:.2f}'
                )
                return False, (
                    f'⏳ Tendencia viró a BULLISH — esperando cierre de {len(shorts_open)} short(s) '
                    f'para liberar capital futures. Rebalanceo progresivo en curso.'
                )
            _record_rebalance_pending_check(
                'FUTURES_TO_SPOT',
                pending_to_spot,
                'Pendiente, pero no transferido por USDT libre insuficiente en Futures',
                blocked_reason='insufficient_futures_free',
                context={
                    'trend': trend,
                    'fut_free': fut_free,
                    'pending_amount': pending_to_spot,
                    'transferable_amount': amount,
                    'calculated_amount': calculated_amount,
                    'threshold': REBALANCE_MIN_USDT,
                    'available_balance': futures_details.get('available_balance', fut_free),
                    'position_margin': futures_details.get('position_margin'),
                    'wallet_balance': futures_details.get('wallet_balance'),
                },
                status='BLOCKED',
            )
            _rebalance_log(f'SKIP: reason=insufficient futures free fut_free={fut_free:.2f} amount={amount:.2f}')
            return False, f'No hay suficiente USDT libre en futures para transferir (${fut_free:.2f})'

        if shorts_open and fut_free - amount < REBALANCE_MIN_WALLET:
            _record_rebalance_pending_check(
                'FUTURES_TO_SPOT',
                pending_to_spot,
                'Pendiente, pero no transferido porque Futures esta ocupado por shorts activos',
                blocked_reason='active_shorts_wallet_reserve',
                context={
                    'trend': trend,
                    'active_shorts': len(shorts_open),
                    'fut_free': fut_free,
                    'pending_amount': pending_to_spot,
                    'transferable_amount': amount,
                    'wallet_min': REBALANCE_MIN_WALLET,
                    'available_balance': futures_details.get('available_balance', fut_free),
                    'position_margin': futures_details.get('position_margin'),
                    'wallet_balance': futures_details.get('wallet_balance'),
                },
                status='BLOCKED',
            )
            _rebalance_log(f'SKIP: reason=active shorts count={len(shorts_open)} fut_free={fut_free:.2f} amount={amount:.2f}')
            return False, (
                f'⏳ Futures ocupado ({len(shorts_open)} short(s) con margen). '
                f'Transferiré ${amount:.2f} cuando cierren posiciones.'
            )

        try:
            _rebalance_log(f'TRANSFER: {amount:.2f} Futures -> Spot', level='INFO')
            transfer_ok, transfer_result, transfer_meta = _transfer_with_recovery('FUTURES_TO_SPOT', amount, context={
                'spot_real': _safe_float(spot_actual),
                'futures_real': _safe_float(fut_actual),
                'target_spot': _safe_float(target_spot),
                'target_futures': _safe_float(target_fut),
                'diff_spot': _safe_float(diff_spot),
                'diff_futures': _safe_float(diff_fut),
                'reason': 'rebalance_transfer_required',
            })
            if not transfer_ok:
                return False, transfer_result
            amount = transfer_result
            state['last_rebalance_trend'] = trend
            if trend_flipped:
                msg = (f'🔄 Rebalanceo parcial (viraje a BULLISH): ${amount:.2f} Futures → Spot '
                       f'| Quedan {len(shorts_open)} shorts — capital liberará al cerrar')
            else:
                msg = f'🔄 Rebalanceo ({label}): ${amount:.2f} USDT Futures → Spot'
            return True, msg
        except Exception as e:
            return False, f'Error al transferir Futures→Spot: {e}'


if __name__ == '__main__':
    import json
    with open(config.STATE_FILE, encoding='utf-8') as f:
        state = json.load(f)
    ok, msg = rebalance(state)
    print(msg)
