#!/usr/bin/env python3
"""Position close, partial take-profit and recovery helpers extracted from bot.py."""

import time

import config
import decision_timeline
import futures_residuals
import rebalance
import residuals
import shorts
import utils


def _state_contains_position(state, pos):
    trade_id = str(pos.get('id') or pos.get('trade_id') or '')
    symbol = str(pos.get('symbol') or '').upper()
    direction = str(pos.get('direction') or '').lower()
    for current in state.get('positions', []) if isinstance(state, dict) else []:
        current_id = str(current.get('id') or current.get('trade_id') or '')
        current_symbol = str(current.get('symbol') or '').upper()
        current_direction = str(current.get('direction') or '').lower()
        if trade_id and current_id == trade_id:
            return True
        if symbol and current_symbol == symbol and (not direction or current_direction == direction):
            return True
    return False


def _first_futures_position(positions, symbol):
    if isinstance(positions, dict):
        positions = [positions]
    for item in positions or []:
        if not isinstance(item, dict):
            continue
        if str(item.get('symbol') or '').upper() == str(symbol).upper():
            return item
    return {}


def _futures_position_amount(binance, symbol):
    if hasattr(binance, 'futures_position_risk'):
        positions = binance.futures_position_risk({'symbol': symbol})
    else:
        positions = binance.fut_signed('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
    item = _first_futures_position(positions, symbol)
    return float(item.get('positionAmt') or 0), item


def _futures_open_orders(binance, symbol):
    if hasattr(binance, 'futures_open_orders'):
        orders = binance.futures_open_orders({'symbol': symbol})
    else:
        orders = binance.fut_signed('GET', '/fapi/v1/openOrders', {'symbol': symbol})
    return orders if isinstance(orders, list) else []


def _spot_free_balance(binance, symbol):
    account = binance.get_spot_account()
    asset = str(symbol).replace('USDT', '')
    balances = account.get('balances', []) if isinstance(account, dict) else []
    return next((float(item.get('free') or 0) for item in balances if item.get('asset') == asset), 0.0)


def _spot_open_orders(binance, symbol):
    if hasattr(binance, 'spot_open_orders'):
        orders = binance.spot_open_orders({'symbol': symbol})
    else:
        orders = binance.spot_signed('GET', '/api/v3/openOrders', {'symbol': symbol})
    return orders if isinstance(orders, list) else []


def _error_details(error):
    try:
        details = utils.extract_http_error_details(error)
    except Exception:
        details = {'error': str(error)}
    details.setdefault('error', str(error))
    return details


def _partial_failure_message(direction, symbol, resolution):
    if resolution == 'position_already_closed':
        return f'Parcial {direction} {symbol} fallo, pero la posicion ya figura cerrada / sin exposicion. Sin accion requerida.'
    if resolution == 'still_open_protected':
        return f'Parcial {direction} {symbol} fallo, pero la posicion sigue abierta con proteccion. Sin accion critica requerida.'
    if resolution == 'still_open_unprotected':
        return f'Parcial {direction} {symbol} fallo y la posicion sigue abierta sin proteccion.'
    return f'Parcial {direction or "UNKNOWN"} {symbol} fallo y no se pudo verificar estado real.'


def classify_partial_failure_after_exchange_check(pos, state, binance, error, side=None, attempted_quantity=None, order_type='MARKET'):
    symbol = pos.get('symbol')
    direction = str(side or pos.get('direction') or '').upper()
    trade_id = pos.get('id') or pos.get('trade_id')
    state_quantity = pos.get('quantity')
    details = {
        'symbol': symbol,
        'side': direction,
        'trade_id': trade_id,
        'attempted_quantity': attempted_quantity,
        'state_quantity': state_quantity,
        'order_type': order_type,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'error': _error_details(error),
        'in_state_after_check': _state_contains_position(state, pos),
    }

    try:
        if direction == 'SHORT':
            position_amt, position = _futures_position_amount(binance, symbol)
            orders = _futures_open_orders(binance, symbol)
            details.update({
                'position_amt_after_check': position_amt,
                'open_orders_count_after_check': len(orders),
                'position_after_check': position,
                'reduce_only_orders_after_check': sum(1 for order in orders if str(order.get('reduceOnly')).lower() == 'true'),
            })
            if abs(position_amt) <= 1e-12:
                resolution = 'position_already_closed'
                severity = 'INFO'
                event = 'partial_failed_but_position_closed'
            elif orders:
                resolution = 'still_open_protected'
                severity = 'INFO'
                event = 'partial_failed_but_position_still_protected'
            else:
                resolution = 'still_open_unprotected'
                severity = 'ERROR'
                event = 'partial_failed_position_unprotected'
        else:
            free_balance = _spot_free_balance(binance, symbol)
            orders = _spot_open_orders(binance, symbol)
            state_qty = float(state_quantity or 0)
            details.update({
                'spot_free_balance_after_check': free_balance,
                'open_orders_count_after_check': len(orders),
            })
            if free_balance + 1e-12 < state_qty and not details['in_state_after_check']:
                resolution = 'position_already_closed'
                severity = 'INFO'
                event = 'partial_failed_but_position_closed'
            elif orders:
                resolution = 'still_open_protected'
                severity = 'INFO'
                event = 'partial_failed_but_position_still_protected'
            else:
                resolution = 'still_open_unprotected'
                severity = 'ERROR'
                event = 'partial_failed_position_unprotected'
    except Exception as check_error:
        details.update({
            'exchange_check_error': _error_details(check_error),
            'position_amt_after_check': None,
            'open_orders_count_after_check': None,
        })
        resolution = 'state_unknown'
        severity = 'WARNING'
        event = 'partial_failed_state_unknown'

    message = _partial_failure_message(direction, symbol, resolution)
    details['resolution'] = resolution
    details['severity'] = severity
    return {
        'resolution': resolution,
        'severity': severity,
        'event': event,
        'message': message,
        'details': details,
        'risk_alert': resolution in {'still_open_unprotected', 'state_unknown'},
    }


def _record_partial_failure_classification(result):
    details = result.get('details') or {}
    try:
        decision_timeline.record_order_event(
            'PARTIAL_CLOSE_FAILED',
            details.get('symbol'),
            details.get('side'),
            result.get('message'),
            level=result.get('severity', 'WARNING'),
            details=details,
            related_trade_id=details.get('trade_id'),
        )
    except Exception:
        pass


def recolocar_oco_long(pos, sym, qty_total, step, price, tp, entry, binance, out_fn):
    import urllib.error as _ue
    try:
        filters = binance.get_spot_filters(sym)
        tick = filters.get('tick_size', 0.0001)
        qty = utils.round_step(qty_total, step)
        new_sl = utils.round_tick(entry * (1 - config.SL_MIN_DIST_PCT / 100), tick)
        new_sl_l = utils.round_tick(new_sl * 0.999, tick)
        new_tp = utils.round_tick(tp, tick)
        oco_params = {
            'symbol': sym, 'side': 'SELL', 'quantity': str(qty),
            'price': str(new_tp), 'stopPrice': str(new_sl),
            'stopLimitPrice': str(new_sl_l), 'stopLimitTimeInForce': 'GTC',
        }
        if residuals.handle_unprotectable_spot_residual(
            sym,
            str(sym).replace('USDT', ''),
            qty_total,
            price,
            filters,
            out_fn=out_fn,
            oco_payload=oco_params,
        ):
            return
        if qty * price < 5.0:
            utils.send_alert(f'🚨 {sym}: no pude recolocar OCO (qty insuficiente). Revisión manual requerida.')
            return
        oco = binance.spot_signed('POST', '/api/v3/order/oco', oco_params)
        pos['oco_order_list_id'] = str(oco.get('orderListId', ''))
        pos['oco_order_ids'] = [str(o['orderId']) for o in oco.get('orders', [])]
        pos['quantity'] = qty
        out_fn(f'✅ OCO recolocado para {sym} tras fallo de parcial')
    except _ue.HTTPError as e:
        residuals.log_spot_oco_payload_notional(
            sym,
            locals().get('oco_params', {}),
            locals().get('filters', {}),
            context='partial spot OCO recovery rejected',
        )
        err = utils._binance_error_msg(e)
        utils.send_alert(f'🚨 {sym}: no pude recolocar OCO ({err}). Revisión manual requerida.')
    except Exception as e:
        utils.send_alert(f'🚨 {sym}: no pude recolocar OCO ({e}). Revisión manual requerida.')


def handle_close(state, pos, action, price_close, pnl, btc_ctx, binance, out_fn, safe_log_close_fn):
    sym = pos['symbol']
    direction = pos['direction']

    state['trade_count'] = state.get('trade_count', 0) + 1
    state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl, 4)
    state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl, 4)
    spot_free_now = binance.get_usdt_spot()
    spot_in_pos_now = sum(
        p['entry_price'] * p['quantity']
        for p in state.get('positions', []) if p['direction'] == 'long'
    )
    capital_now = spot_free_now + spot_in_pos_now + binance.get_total_futures()

    label = {'closed_tp': 'TP ✅', 'closed_sl': 'SL 🔴', 'closed_manual': 'STALE ⏱️ (sin movimiento)'}[action]
    dir_emoji = '📈' if direction == 'long' else '📉'
    if action == 'closed_sl' and not pos.get('partial_taken'):
        msg = (
            f'{dir_emoji} {direction.upper()} {sym} cerrado: {label}\n'
            f'PnL: {pnl:+.4f} USDT | Acumulado: {state["total_pnl_usdt"]:+.4f} USDT'
        )
    elif action == 'closed_sl' and pos.get('partial_taken'):
        ppnl = pos.get('partial_pnl')
        ppnl_str = f'+${ppnl:.4f}' if ppnl else 'ver log'
        msg = (
            f'{dir_emoji} {direction.upper()} {sym} cerrado: {label} (breakeven - parcial TP cobrado: {ppnl_str})\n'
            f'PnL esta mitad: {pnl:+.4f} USDT | Acumulado: {state["total_pnl_usdt"]:+.4f} USDT'
        )
    else:
        msg = (
            f'{dir_emoji} {direction.upper()} {sym} cerrado: {label}\n'
            f'PnL: {pnl:+.4f} USDT | Acumulado: {state["total_pnl_usdt"]:+.4f} USDT'
        )
    out_fn(msg)
    reason = {'closed_tp': 'TP', 'closed_sl': 'SL', 'closed_manual': 'STALE_EXIT'}[action]
    utils.send_alert(utils.format_trade_close_alert(pos, price_close, reason, pnl))
    utils.log_trade(state['trade_count'], sym, direction, label, pnl, capital_now)
    safe_log_close_fn(pos, price_close, reason, pnl)

    if action == 'closed_sl':
        had_partial = pos.get('partial_taken', False)

        if not had_partial:
            state['consec_sl'] = state.get('consec_sl', 0) + 1
            state['last_sl_time'] = int(time.time())
            state['skip_next_cycles'] = 2

        if config.COOLDOWN_AFTER_SL:
            utils.add_cooldown(state, sym)

        if not had_partial:
            sl_history = state.get('sl_history_by_symbol', {})
            if sym not in sl_history:
                sl_history[sym] = []
            sl_history[sym].append(int(time.time()))
            now = int(time.time())
            cutoff = now - 432000
            sl_history[sym] = [ts for ts in sl_history[sym] if ts > cutoff]
            state['sl_history_by_symbol'] = sl_history
            if len(sl_history[sym]) >= 3:
                if sym not in config.BLACKLIST_SYMBOLS:
                    config.BLACKLIST_SYMBOLS.add(sym)
                    out_fn(f'⛔ {sym} auto-blacklisted: 3 SLs reales en 5 días')
                    utils.send_alert(f'⛔ {sym} agregado a BLACKLIST automática: 3 SLs reales en 5 días')
    else:
        state['consec_sl'] = 0
        utils.remove_cooldown(state, sym)

    daily_start = state.get('daily_start_capital', capital_now)
    if daily_start > 0:
        daily_loss_pct = (state['daily_pnl_usdt'] / daily_start) * 100
        if daily_loss_pct <= -config.DAILY_LOSS_LIMIT_PCT:
            state['status'] = 'paused'
            out_fn(f'⛔ Límite diario alcanzado ({daily_loss_pct:.2f}%). Bot pausado hasta mañana.')
            utils.send_alert(f'⛔ Bot pausado por límite diario: {daily_loss_pct:.2f}%')

    try:
        rb_ok, rb_msg = rebalance.rebalance(state, btc_ctx)
        if rb_ok:
            out_fn(rb_msg)
            utils.send_alert(utils.format_rebalance_alert(rb_msg))
    except Exception:
        pass


def check_partial_long(pos, state, binance, out_fn, analytics, recolocar_oco_long_fn):
    if pos.get('partial_taken'):
        return

    entry = pos['entry_price']
    tp = pos['tp']
    sym = pos['symbol']

    try:
        price = binance.get_spot_price(sym)
    except Exception:
        return

    mid = entry + (tp - entry) * config.PARTIAL_TAKE_PCT
    if price < mid:
        return

    oco_id = pos.get('oco_order_list_id', '')
    qty_half = utils.round_step(pos['quantity'] * 0.5,
                                binance.get_spot_filters(sym).get('step_size', 0.001))
    qty_rest = utils.round_step(pos['quantity'] * 0.5,
                                binance.get_spot_filters(sym).get('step_size', 0.001))

    if qty_half * price < 5.0:
        return

    import urllib.error as _ue
    try:
        oco_cancelled = False
        if oco_id:
            try:
                binance.spot_signed('DELETE', '/api/v3/orderList', {'symbol': sym, 'orderListId': int(oco_id)})
                oco_cancelled = True
            except _ue.HTTPError as e:
                err = utils._binance_error_msg(e)
                if '-2011' in err or '-1013' in err:
                    pos['partial_taken'] = True
                    out_fn(f'⚠️ Parcial LONG {sym}: OCO ya ejecutado ({err}), marcando partial_taken')
                    return
                else:
                    out_fn(f'⚠️ Parcial LONG {sym}: error al cancelar OCO ({err}), abortando parcial')
                    return

        try:
            acct = binance.get_spot_account()
            base_asset = sym.replace('USDT', '')
            free_base = next((float(b['free']) for b in acct.get('balances', []) if b['asset'] == base_asset), 0)
            step = binance.get_spot_filters(sym).get('step_size', 0.001)
            qty_half_real = utils.round_step(min(qty_half, free_base * 0.5), step)
            qty_rest_real = utils.round_step(free_base - qty_half_real, step)
            if qty_half_real * price < 5.0 or qty_rest_real * price < 5.0:
                out_fn(f'⚠️ Parcial LONG {sym}: qty insuficiente (free={free_base:.4f}), abortando')
                if oco_cancelled:
                    recolocar_oco_long_fn(pos, sym, free_base, step, price, tp, entry)
                return
        except Exception as e:
            out_fn(f'⚠️ Parcial LONG {sym}: no pude verificar balance ({e}), abortando')
            return

        try:
            binance.spot_signed('POST', '/api/v3/order', {
                'symbol': sym, 'side': 'SELL', 'type': 'MARKET', 'quantity': str(qty_half_real)
            })
        except _ue.HTTPError as e:
            err = utils._binance_error_msg(e)
            classification = classify_partial_failure_after_exchange_check(
                pos,
                state,
                binance,
                e,
                side='LONG',
                attempted_quantity=qty_half_real,
                order_type='MARKET',
            )
            _record_partial_failure_classification(classification)
            out_fn(f"WARNING: {classification['message']} Error: {err}")
            if classification.get('risk_alert'):
                utils.send_alert(f"{classification['message']} Error: {err}")
            if oco_cancelled:
                recolocar_oco_long_fn(pos, sym, qty_half_real + qty_rest_real, step, price, tp, entry)
            return

        pnl_partial = (price - entry) * qty_half_real
        tick = binance.get_spot_filters(sym).get('tick_size', 0.0001)

        new_sl = utils.round_tick(entry * 1.003, tick)
        new_sl_limit = utils.round_tick(new_sl * 0.999, tick)
        new_tp = utils.round_tick(tp, tick)

        try:
            oco = binance.spot_signed('POST', '/api/v3/order/oco', {
                'symbol': sym,
                'side': 'SELL',
                'quantity': str(qty_rest_real),
                'price': str(new_tp),
                'stopPrice': str(new_sl),
                'stopLimitPrice': str(new_sl_limit),
                'stopLimitTimeInForce': 'GTC',
            })
        except _ue.HTTPError as e:
            err = utils._binance_error_msg(e)
            utils.send_alert(f'🚨 Parcial LONG {sym}: vendido pero OCO fallido ({err}). Intervención requerida.')
            out_fn(f'🚨 Parcial LONG {sym}: vendido 50% pero no pude colocar nuevo OCO ({err})')
            pos['partial_taken'] = True
            return

        pos['quantity'] = qty_rest_real
        pos['sl'] = new_sl
        pos['oco_order_list_id'] = str(oco.get('orderListId', ''))
        pos['oco_order_ids'] = [str(o['orderId']) for o in oco.get('orders', [])]
        pos['partial_taken'] = True
        pos['partial_pnl'] = round(pnl_partial, 4)

        msg = (
            f'💰 PARCIAL LONG {sym}: vendí 50% @ ${price:.4f}\n'
            f'PnL parcial: +${pnl_partial:.4f} | SL movido a breakeven ${new_sl:.4f}'
        )
        out_fn(msg)
        utils.send_alert(utils.format_trade_close_alert(pos, price, 'PARTIAL_TP', pnl_partial))
        state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl_partial, 4)
        state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl_partial, 4)
        try:
            analytics.log_trade_close(
                trade_id=f'{pos.get("id")}:partial',
                symbol=sym,
                side='LONG',
                entry_time=pos.get('entry_time'),
                entry_price=entry,
                exit_price=price,
                exit_reason='PARTIAL_TP',
                pnl_usdt=pnl_partial,
            )
        except Exception:
            pass

    except Exception as e:
        out_fn(f'⚠️ Parcial LONG {sym} error inesperado: {e}')


def check_partial_short(pos, state, binance, out_fn, analytics):
    if pos.get('partial_taken'):
        return

    entry = pos['entry_price']
    tp = pos['tp']
    sym = pos['symbol']

    try:
        price = binance.get_fut_price(sym)
    except Exception:
        return

    mid = entry - (entry - tp) * config.PARTIAL_TAKE_PCT
    if price > mid:
        return

    qty = pos['quantity']
    step = binance.get_futures_filters(sym).get('step_size', 0.01)
    qty_half = utils.round_step(qty * 0.5, step)
    qty_rest = utils.round_step(qty * 0.5, step)

    if qty_half < binance.get_futures_filters(sym).get('min_qty', 0.01):
        return

    try:
        order = binance.fut_signed('POST', '/fapi/v1/order', {
            'symbol': sym, 'side': 'BUY', 'type': 'MARKET',
            'quantity': str(qty_half), 'reduceOnly': 'true',
        })
        time.sleep(1)
        d = binance.fut_signed('GET', '/fapi/v1/order', {
            'symbol': sym, 'orderId': order['orderId']
        })
        fill = float(d.get('avgPrice', price))
        if fill == 0:
            fill = price

        pnl_partial = (entry - fill) * qty_half

        tp_id = pos.get('tp_order_id', '')
        if tp_id:
            try:
                binance.fut_signed('DELETE', '/fapi/v1/order', {'symbol': sym, 'orderId': int(tp_id)})
            except Exception:
                pass

        tick = binance.get_futures_filters(sym).get('tick_size', 0.001)
        new_sl = utils.round_tick(entry * 1.003, tick)
        new_tp = utils.round_tick(tp, tick)

        tp_order = binance.fut_signed('POST', '/fapi/v1/order', {
            'symbol': sym, 'side': 'BUY', 'type': 'LIMIT',
            'price': str(new_tp), 'quantity': str(qty_rest),
            'reduceOnly': 'true', 'timeInForce': 'GTC',
        })
        new_tp_order_id = str(tp_order.get('orderId', ''))

        old_sl_id = pos.get('sl_order_id', '')
        new_sl_order_id = ''
        if config.NATIVE_SL_ENABLED:
            if old_sl_id:
                try:
                    binance.fut_signed('DELETE', '/fapi/v1/order', {
                        'symbol': sym, 'orderId': int(old_sl_id)
                    })
                except Exception:
                    pass
            try:
                price_now = binance.get_fut_price(sym)
                if new_sl > price_now * 1.0005:
                    max_stop_dist_pct = 4.5
                    max_allowed_sl = price_now * (1 + max_stop_dist_pct / 100)
                    if new_sl > max_allowed_sl:
                        new_sl = utils.round_tick(max_allowed_sl, tick)

                    sl_order = shorts._place_stop_market(sym, 'BUY', new_sl, qty_rest)
                    new_sl_order_id = str(sl_order.get('orderId', '') or sl_order.get('strategyId', '')) if sl_order else ''
                else:
                    pass
            except Exception as e:
                import logging
                error_msg = str(e)
                logging.error(f'SL breakeven {sym}: stopPrice={new_sl}, qty={qty_rest}, price={price_now}, error={error_msg}')
                utils.send_alert(f'⚠️ SL nativo breakeven {sym} no se pudo colocar: {error_msg}. Guardian software activo.')

        pos['quantity'] = qty_rest
        pos['sl'] = new_sl
        pos['tp_order_id'] = new_tp_order_id
        pos['sl_order_id'] = new_sl_order_id
        pos['partial_taken'] = True
        pos['partial_pnl'] = round(pnl_partial, 4)

        msg = (
            f'💰 PARCIAL SHORT {sym}: cerré 50% @ ${fill:.4f}\n'
            f'PnL parcial: +${pnl_partial:.4f} | SL movido a breakeven ${new_sl:.4f}'
        )
        out_fn(msg)
        utils.send_alert(utils.format_trade_close_alert(pos, fill, 'PARTIAL_TP', pnl_partial))
        state['trade_count'] = state.get('trade_count', 0) + 1
        state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl_partial, 4)
        state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl_partial, 4)
        spot_free_now = binance.get_usdt_spot()
        capital_now = spot_free_now + binance.get_total_futures()
        try:
            analytics.log_trade_close(
                trade_id=f'{pos.get("id")}:partial',
                symbol=sym,
                side='SHORT',
                entry_time=pos.get('entry_time'),
                entry_price=entry,
                exit_price=fill,
                exit_reason='PARTIAL_TP',
                pnl_usdt=pnl_partial,
            )
        except Exception:
            pass
        utils.log_trade(state['trade_count'], sym, 'short', 'PARCIAL TP 💰 (50%)', pnl_partial, capital_now)
        try:
            residual_result = futures_residuals.handle_after_partial_short(
                pos,
                state,
                binance,
                out_fn=out_fn,
                alert_fn=utils.send_alert,
            )
            if residual_result.get('status') in ('closed', 'already_closed'):
                pos['closed_by_residual_cleanup'] = True
        except Exception as residual_error:
            out_fn(f'⚠️ Futures residual check {sym} falló: {residual_error}')

    except Exception as e:
        classification = classify_partial_failure_after_exchange_check(
            pos,
            state,
            binance,
            e,
            side='SHORT',
            attempted_quantity=locals().get('qty_half'),
            order_type='MARKET',
        )
        _record_partial_failure_classification(classification)
        out_fn(f"WARNING: {classification['message']} Error: {e}")
        if classification.get('risk_alert'):
            utils.send_alert(f"{classification['message']} Error: {e}")
