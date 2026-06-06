#!/usr/bin/env python3
"""
Resumen diario de trading — corre a las 00:00 UTC via cron.
Cada sección separada por \n\n porque Jarvis colapsa \n simple a espacio.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
import utils, config


def uy_time(utc_str):
    try:
        parts = utc_str.replace(' UTC', '').split(' ')
        h, m  = map(int, parts[1].split(':'))
        return f"{(h - 3) % 24:02d}:{m:02d} UY"
    except Exception:
        return utc_str


def resultado_emoji(resultado):
    r = resultado.upper()
    if 'PARCIAL' in r and 'TP' in r: return 'PARCIAL TP 💰'
    if 'TP'      in r:               return 'TP ✅'
    if 'SL'      in r:               return 'SL 🔴'
    if 'STALE'   in r:               return 'STALE ⏱️'
    if 'MANUAL'  in r:               return 'MANUAL ⏱️'
    return resultado


def main():
    state       = utils.load_state()
    ayer        = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 86400))
    spot        = utils.get_usdt_spot()
    fut_total, fut_avail, fut_margin = utils.get_futures_summary()
    total       = spot + fut_total
    positions   = state.get('positions', [])
    daily_pnl   = state.get('daily_pnl_usdt', 0)
    daily_start = state.get('daily_start_capital', 0)
    daily_pct   = (daily_pnl / daily_start * 100) if daily_start > 0 else 0
    total_pnl   = state.get('total_pnl_usdt', 0)
    trade_count = state.get('trade_count', 0)
    bot_status  = state.get('status', 'active').upper()

    # uPnL de posiciones abiertas
    upnl_total = 0.0
    pos_data   = []
    for p in positions:
        sym = p['symbol']
        try:
            if p['direction'] == 'short':
                price = utils.get_fut_price(sym)
                upnl  = (p['entry_price'] - price) * p['quantity']
            else:
                price = utils.get_spot_price(sym)
                upnl  = (price - p['entry_price']) * p['quantity']
        except Exception:
            upnl = 0.0
        upnl_total += upnl
        parcial = ' (parcial TP)' if p.get('partial_taken') else ''
        signo   = '+' if upnl >= 0 else ''
        pos_data.append(
            f"{'LONG' if p['direction']=='long' else 'SHORT'} {sym}: "
            f"{signo}${upnl:.4f}{parcial}"
        )

    # Trades del día
    trades_dia = []
    try:
        with open(config.TRADES_LOG) as f:
            for line in f:
                if line.startswith('#') or '|' not in line:
                    continue
                if ayer not in line:
                    continue
                parts = [p.strip() for p in line.strip().split('|')]
                if len(parts) >= 6:
                    try:
                        trades_dia.append({
                            'par':       parts[1],
                            'resultado': parts[2],
                            'pnl':       float(parts[3].replace('$','').replace('+','')),
                            'hora':      uy_time(parts[5]),
                        })
                    except Exception:
                        pass
    except FileNotFoundError:
        pass

    n_tot = len(trades_dia)
    n_tp  = sum(1 for t in trades_dia if t['pnl'] > 0)
    n_sl  = sum(1 for t in trades_dia if t['pnl'] < 0)

    pnl_emoji = '🟢' if daily_pnl >= 0 else '🔴'
    sd = '+' if daily_pnl  >= 0 else ''
    st = '+' if total_pnl  >= 0 else ''
    su = '+' if upnl_total >= 0 else ''

    # ── Armar partes (cada una separada por \n\n al unir) ────────────────────
    P = []

    P.append(f"📊 Resumen diario — {ayer}")

    P.append(f"💰 Balance: ${total:.2f} USDT")
    P.append(f"Spot: ${spot:.2f}   Futures: ${fut_total:.2f}")
    if positions:
        P.append(f"uPnL abierto: {su}${upnl_total:.4f}")
    P.append(f"{pnl_emoji} PnL del dia: {sd}${daily_pnl:.4f} ({sd}{daily_pct:.2f}%)")
    P.append(f"📈 PnL acumulado: {st}${total_pnl:.4f}")
    P.append(f"🤖 Bot: {bot_status}   {trade_count} trades totales")

    P.append("─" * 28)

    if trades_dia:
        P.append(f"Trades cerrados ({n_tot}):")
        for t in trades_dia:
            sp = '+' if t['pnl'] >= 0 else ''
            P.append(f"• {t['par']}   {resultado_emoji(t['resultado'])}   {sp}${t['pnl']:.4f}   {t['hora']}")
    else:
        P.append("Sin trades cerrados.")

    if pos_data:
        P.append("─" * 28)
        P.append(f"Posiciones abiertas ({len(positions)}):")
        for pd in pos_data:
            P.append(pd)

    P.append("─" * 28)

    if n_tot == 0:
        P.append("Sin actividad. Bot en modo scanning.")
    elif daily_pnl > 0:
        P.append(f"Buen dia — {n_tp} TP / {n_sl} SL. Ganancia neta: +${daily_pnl:.4f}.")
    elif daily_pnl < 0 and n_sl == n_tot:
        P.append(f"Dia dificil — {n_tot} trades, todos en negativo.")
    else:
        P.append(f"{n_tp} TP / {n_sl} SL — dia mixto. PnL neto: {sd}${daily_pnl:.4f}.")

    msg = "\n\n".join(P)
    print(msg)


if __name__ == '__main__':
    main()
