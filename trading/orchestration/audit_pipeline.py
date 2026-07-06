#!/usr/bin/env python3
"""Audit and reconciliation helpers for passive state-vs-wallet checks."""

import logging
import time

import config
import residuals
import utils


def audit_orphans(state, binance, out_fn, safe_log_open_fn):
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

                if residuals.handle_unprotectable_spot_residual(
                    sym,
                    asset,
                    free,
                    cur,
                    filters,
                    out_fn=out_fn,
                    limit_price=tp,
                    stop_price=sl,
                    stop_limit_price=sl_limit,
                ):
                    continue

                if tp <= cur or sl >= cur or qty <= 0:
                    raise ValueError(f'precios inválidos: sl={sl} cur={cur} tp={tp} qty={qty}')

                utils.send_alert(msg)
                oco_params = {
                    'symbol': sym,
                    'side': 'SELL',
                    'quantity': str(qty),
                    'price': str(tp),
                    'stopPrice': str(sl),
                    'stopLimitPrice': str(sl_limit),
                    'stopLimitTimeInForce': 'GTC',
                }
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
