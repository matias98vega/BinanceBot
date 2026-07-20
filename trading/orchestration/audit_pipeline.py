#!/usr/bin/env python3
"""Audit and reconciliation helpers for passive state-vs-wallet checks."""

import logging
import time

import config
import decision_timeline
import residuals
import utils


SPOT_RECONCILIATION_SOURCE = 'automatic_spot_position_reconciliation'


def _spot_number(value):
    try:
        value = float(value)
        return value if value == value else None
    except (TypeError, ValueError):
        return None


def evaluate_spot_position_reconciliation(position, account, trades, open_orders, filters, price):
    """Classify a managed Spot position from read-only exchange evidence."""
    symbol = str(position.get('symbol') or '').upper()
    asset = symbol[:-4] if symbol.endswith('USDT') else symbol
    managed = _spot_number(position.get('quantity'))
    entry_time = _spot_number(position.get('entry_time'))
    step = _spot_number((filters or {}).get('step_size'))
    min_qty = _spot_number((filters or {}).get('min_qty'))
    min_notional = _spot_number((filters or {}).get('min_notional'))
    mark = _spot_number(price)
    if not symbol or managed is None or entry_time is None or step is None or min_qty is None or min_notional is None or mark is None:
        return {'classification': 'INSUFFICIENT_EVIDENCE', 'reconcile': False, 'reason': 'missing_position_or_filter_data'}
    balances = account.get('balances', []) if isinstance(account, dict) else []
    balance = next((row for row in balances if str(row.get('asset') or '').upper() == asset), {})
    free = _spot_number(balance.get('free'))
    locked = _spot_number(balance.get('locked'))
    if free is None or locked is None or not isinstance(trades, list) or not isinstance(open_orders, list):
        return {'classification': 'INSUFFICIENT_EVIDENCE', 'reconcile': False, 'reason': 'missing_exchange_evidence'}
    relevant = [row for row in trades if _spot_number(row.get('time')) is not None and float(row['time']) >= entry_time * 1000 - 1000]
    bought = sum(float(row.get('qty') or 0) for row in relevant if row.get('isBuyer') is True)
    sold = sum(float(row.get('qty') or 0) for row in relevant if row.get('isBuyer') is False)
    fees_asset = sum(float(row.get('commission') or 0) for row in relevant if str(row.get('commissionAsset') or '').upper() == asset)
    expected = bought - sold - fees_asset
    observed = free + locked
    residual_notional = observed * mark
    tolerance = max(step, 1e-12)
    evidence = {
        'total_bought': round(bought, 8), 'total_sold': round(sold, 8),
        'quantity_paid_as_fee': round(fees_asset, 8),
        'quantity_expected': round(expected, 8), 'quantity_observed': round(observed, 8),
        'free_balance': free, 'locked_balance': locked,
        'difference': round(observed - managed, 8), 'step_size': step,
        'min_qty': min_qty, 'min_notional': min_notional,
        'residual_notional': round(residual_notional, 8),
        'open_orders_count': len(open_orders),
        'trade_ids': [row.get('id') for row in relevant],
        'order_ids': [row.get('orderId') for row in relevant],
    }
    if open_orders:
        classification, reconcile, reason = 'POSITION_STILL_OPEN', False, 'open_orders_exist'
    elif abs(observed - managed) <= tolerance and sold <= tolerance:
        classification, reconcile, reason = 'POSITION_STILL_OPEN', False, 'managed_and_exchange_aligned'
    elif observed >= min_qty and residual_notional >= min_notional:
        classification, reconcile, reason = 'PARTIALLY_CLOSED', False, 'operable_residual_remains'
    elif sold + tolerance >= managed and observed < min_qty and residual_notional < min_notional:
        classification, reconcile, reason = 'CLOSED_ON_EXCHANGE_OPEN_IN_STATE', True, 'exchange_sell_fill_and_non_operable_residual'
    elif observed <= tolerance and sold > tolerance:
        classification, reconcile, reason = 'CLOSED_ON_EXCHANGE_OPEN_IN_STATE', True, 'exchange_closed_state_open'
    elif abs(expected - observed) <= tolerance and abs(observed - managed) > tolerance:
        classification, reconcile, reason = 'STATE_QUANTITY_STALE', True, 'non_operable_exchange_quantity_differs_from_state'
    else:
        classification, reconcile, reason = 'INSUFFICIENT_EVIDENCE', False, 'exchange_history_does_not_explain_difference'
    return {'classification': classification, 'reconcile': reconcile, 'reason': reason, 'evidence': evidence}


def reconcile_stale_spot_positions(state, binance, out_fn=lambda _message: None,
                                   save_state_fn=utils.save_state, timeline_path=None):
    """Remove only unequivocally closed/stale managed Spot entries; never trade."""
    results = []
    for position in list(state.get('positions', [])):
        if str(position.get('direction') or '').lower() != 'long':
            continue
        symbol = str(position.get('symbol') or '').upper()
        try:
            account = binance.get_spot_account()
            trades = binance.my_trades({'symbol': symbol, 'limit': 1000})
            if hasattr(binance, 'spot_open_orders'):
                orders = binance.spot_open_orders({'symbol': symbol})
            else:
                orders = binance.spot_signed('GET', '/api/v3/openOrders', {'symbol': symbol})
            filters = binance.get_spot_filters(symbol)
            price = binance.get_spot_price(symbol)
            result = evaluate_spot_position_reconciliation(position, account, trades, orders, filters, price)
        except Exception as exc:
            result = {'classification': 'BINANCE_OBSERVATION_ERROR', 'reconcile': False, 'reason': str(exc)}
        result.update({'symbol': symbol, 'trade_id': position.get('id') or position.get('trade_id')})
        results.append(result)
        if not result.get('reconcile'):
            continue
        trade_id = str(position.get('id') or position.get('trade_id') or '')
        current = state.get('positions', [])
        if not any(str(item.get('id') or item.get('trade_id') or '') == trade_id for item in current):
            continue
        state['positions'] = [item for item in current if str(item.get('id') or item.get('trade_id') or '') != trade_id]
        state.setdefault('spot_position_reconciliations', {})[trade_id] = {
            'source': SPOT_RECONCILIATION_SOURCE,
            'reason': result['reason'],
            'classification': result['classification'],
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        save_state_fn(state)
        ev = result.get('evidence') or {}
        details = {
            'source': SPOT_RECONCILIATION_SOURCE, 'reason': result['reason'],
            'classification': result['classification'],
            'quantity_managed_before': position.get('quantity'),
            'quantity_expected': ev.get('quantity_expected'),
            'quantity_observed': ev.get('quantity_observed'),
            'residual_quantity': ev.get('quantity_observed'),
            'residual_notional': ev.get('residual_notional'),
            'evidence': ev,
        }
        kwargs = {'path': timeline_path} if timeline_path else {}
        decision_timeline.record_event(
            'spot_position_state_reconciled',
            f'{symbol} managed position reconciled from read-only exchange evidence',
            level='WARNING', category='CAPITAL', symbol=symbol, direction='LONG',
            details=details, related_trade_id=trade_id, **kwargs)
        out_fn(f'WARNING: {symbol} stale managed position reconciled ({result["classification"]})')
    return results


def audit_orphans(state, binance, out_fn, safe_log_open_fn):
    reconcile_stale_spot_positions(state, binance, out_fn=out_fn)
    """
    Detecta activos spot con valor > $5 que no tienen posicion registrada en el state.
    Si encuentra uno: intenta colocar un OCO de proteccion y lo agrega al state.
    """
    try:
        active_syms = {p['symbol'] for p in state.get('positions', []) if p['direction'] == 'long'}
        cooldown_syms = utils.get_active_cooldowns(state)
        dust_in_progress = state.get('dust_in_progress', False)

        all_prices = {}
        try:
            for p in binance.spot_ticker_prices():
                all_prices[p['symbol']] = float(p['price'])
        except Exception:
            pass

        acc = binance.get_spot_account()
        try:
            residuals.reconcile_status_file_with_spot_balances(
                balances=acc.get('balances', []),
                state=state,
            )
        except Exception as exc:
            logging.warning('SPOT RESIDUAL stale reconcile failed: %s', exc)
        for b in acc.get('balances', []):
            asset = b['asset']
            free = float(b['free'])
            locked = float(b['locked'])
            total = free + locked
            if asset in config.DUST_PROTECTED or total < 0.001:
                continue
            if locked > 0:
                continue
            sym = asset + 'USDT'
            price = all_prices.get(sym, 0)
            if price == 0 or total * price < 5.0:
                continue
            if sym in active_syms:
                continue

            if sym in cooldown_syms:
                cd_info = state.get('cooldown_symbols', {})
                expiry = cd_info.get(sym, 0) if isinstance(cd_info, dict) else 0
                rem_h = max(0, (expiry - int(time.time())) / 3600) if expiry else 0
                out_fn(f'ℹ️ {asset} en cooldown ({rem_h:.1f}h restantes), no se coloca OCO automático')
                continue

            if dust_in_progress and total * price < 15.0:
                continue

            msg = f'⚠️ Activo huérfano detectado: {asset} ({total:.4f} = ${total*price:.2f})'
            out_fn(msg)

            try:
                trades = binance.spot_signed('GET', '/api/v3/myTrades', {'symbol': sym, 'limit': 5})
                buys = [t for t in trades if t['isBuyer']]
                entry = float(buys[-1]['price']) if buys else price
            except Exception:
                entry = price

            try:
                k1h = binance.get_klines(sym, '1h', 50)
                closes = [float(k[4]) for k in k1h]
                highs = [float(k[2]) for k in k1h]
                lows = [float(k[3]) for k in k1h]
                trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
                atr = sum(trs[-14:]) / 14
                cur = closes[-1]
            except Exception:
                atr = price * 0.015
                cur = price

            try:
                filters = binance.get_spot_filters(sym)
                tick = filters.get('tick_size', 0.0001)
                step = filters.get('step_size', 0.1)
                qty = utils.round_step(free, step)

                sl = utils.round_tick(cur - config.SL_ATR_MULT * atr, tick)
                tp = utils.round_tick(entry + config.TP_ATR_MULT * atr, tick)
                sl = max(sl, cur * (1 - config.SL_MIN_DIST_PCT / 100 * 1.05))
                sl = utils.round_tick(sl, tick)
                sl_limit = utils.round_tick(sl * 0.9985, tick)
                oco_params = {
                    'symbol': sym,
                    'side': 'SELL',
                    'quantity': str(qty),
                    'price': str(tp),
                    'stopPrice': str(sl),
                    'stopLimitPrice': str(sl_limit),
                    'stopLimitTimeInForce': 'GTC',
                }

                if residuals.handle_unprotectable_spot_residual(
                    sym,
                    asset,
                    free,
                    cur,
                    filters,
                    out_fn=out_fn,
                    oco_payload=oco_params,
                ):
                    continue

                if tp <= cur or sl >= cur or qty <= 0:
                    raise ValueError(f'precios inválidos: sl={sl} cur={cur} tp={tp} qty={qty}')

                utils.send_alert(msg)
                oco = binance.spot_signed('POST', '/api/v3/order/oco', oco_params)
                oco_id = str(oco.get('orderListId', ''))

                state['positions'].append({
                    'id': f'long_{sym}_recovered_{int(time.time())}',
                    'direction': 'long',
                    'symbol': sym,
                    'entry_price': entry,
                    'quantity': qty,
                    'sl': sl,
                    'tp': tp,
                    'atr': atr,
                    'oco_order_list_id': oco_id,
                    'entry_time': int(time.time()),
                    'partial_taken': False,
                    'trail_peak': cur,
                })
                safe_log_open_fn(state['positions'][-1], None, None, None)
                utils.save_state(state)
                ok_msg = f'✅ {asset} recuperado: OCO colocado (SL=${sl:.4f} TP=${tp:.4f})'
                out_fn(ok_msg)
                utils.send_alert(ok_msg)

            except Exception as e:
                details = {}
                if hasattr(e, 'code') or hasattr(e, 'status'):
                    residuals.log_spot_oco_payload_notional(
                        sym,
                        locals().get('oco_params', {}),
                        locals().get('filters', {}),
                        context='orphan spot OCO recovery rejected',
                    )
                    details = utils.log_binance_http_error(
                        'orphan spot OCO recovery',
                        sym,
                        'SELL',
                        'OCO',
                        locals().get('oco_params', {}),
                        e,
                    )
                reason = utils.format_binance_error_details(details, include_raw_body=True) or str(e)
                out_fn(f'❌ No se pudo proteger {asset}: {reason}')
                utils.send_alert(f'🚨 {asset} huérfano sin OCO: {reason}. Requiere intervención manual.')

    except Exception as e:
        out_fn(f'⚠️ Auditoría falló: {e}')


def maybe_clean_dust(state, binance, out_fn):
    import time as _time
    now = int(_time.time())
    last = state.get('last_dust_clean', 0)
    weekday = _time.gmtime(now).tm_wday

    dust_in_progress = state.get('dust_in_progress', False)
    nueva_semana = (weekday == config.DUST_CLEAN_DAY and now - last >= 604800)

    if not dust_in_progress and not nueva_semana:
        return

    last_conv = state.get('last_dust_conversion', 0)
    if now - last_conv < 3660:
        return

    if not getattr(config, 'AUTO_CLEAN_DUST', False):
        logging.warning('DUST CLEAN SKIP: disabled by config')
        if nueva_semana:
            out_fn('Limpieza de polvo omitida: AUTO_CLEAN_DUST=False')
        return

    dust_dry_run = getattr(config, 'DUST_CLEAN_DRY_RUN', True)
    if dust_dry_run:
        logging.warning('DUST CLEAN DRY RUN')
    else:
        logging.warning('DUST CLEAN EXECUTE')

    assets, msg = binance.clean_dust(dry_run=dust_dry_run)
    if assets:
        logging.warning('DUST CLEAN RESULT assets=%s dry_run=%s message=%s', assets, dust_dry_run, msg)
        out_fn(f'🧹 Polvo: {msg}')
        if not dust_dry_run:
            utils.send_alert(f'Polvo convertido: {msg}')
        state['last_dust_conversion'] = now
        state['dust_in_progress'] = True
    elif 'Rate limit' in msg:
        pass
    elif 'Sin polvo' in msg or 'insuficiente' in msg:
        state['last_dust_clean'] = now
        state['dust_in_progress'] = False
        if nueva_semana:
            out_fn(f'🧹 Limpieza de polvo completada')
    else:
        state['last_dust_conversion'] = now
