#!/usr/bin/env python3
"""
Orquestador principal del bot de trading.
Corre cada 10 min via cron. Gestiona longs (spot) y shorts (futures) simultÃ¡neamente.
"""
import sys, os, time, json
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
sys.path.insert(0, os.path.dirname(__file__))
import config, utils, bot_state, binance_client
from orchestration import audit_pipeline, cycle_runner, persistence_pipeline, position_lifecycle
from analytics import AnalyticsLogger, DecisionSnapshotLogger
from telegram_alerts import send_telegram_alert

OUTPUT = []
ANALYTICS = AnalyticsLogger()
DECISIONS = DecisionSnapshotLogger()
BINANCE = binance_client.get_default_client()

def out(msg):
    print(msg)
    OUTPUT.append(msg)


def _safe_log_open(pos, candidate, btc_ctx, capital_at_entry):
    return persistence_pipeline.safe_log_open(pos, candidate, btc_ctx, capital_at_entry, ANALYTICS)


def _safe_log_close(pos, exit_price, exit_reason, pnl):
    return persistence_pipeline.safe_log_close(pos, exit_price, exit_reason, pnl, ANALYTICS)


def _safe_log_decision_snapshot(btc_ctx, spot_total_capital, spot_balance, futures_balance):
    return persistence_pipeline.safe_log_decision_snapshot(
        btc_ctx, spot_total_capital, spot_balance, futures_balance, DECISIONS, BINANCE
    )


def _safe_persist_bot_state(state, btc_ctx=None, spot_real=None, futures_real=None,
                            max_longs=None, max_shorts=None, system_health='OK'):
    return persistence_pipeline.safe_persist_bot_state(
        state,
        btc_ctx=btc_ctx,
        spot_real=spot_real,
        futures_real=futures_real,
        max_longs=max_longs,
        max_shorts=max_shorts,
        system_health=system_health,
        out_fn=out,
    )


def main():
    lock = utils.acquire_lock()
    if not lock:
        print('âš ï¸ Ya hay una instancia corriendo. Saliendo.')
        sys.exit(0)
    try:
        _run()
    except Exception as e:
        err = str(e)
        if '503' in err or '502' in err or '504' in err:
            print(f'\u26a0\ufe0f Binance no disponible temporalmente ({e}). El pr\u00f3ximo ciclo reintenta.')
        elif '429' in err or '418' in err:
            print(f'\u26a0\ufe0f Rate limit de Binance alcanzado. El pr\u00f3ximo ciclo reintenta.')
        else:
            print(f'\u274c Error inesperado: {e}')
            send_telegram_alert('CRITICAL', 'Bot error inesperado', str(e))
            try:
                state = utils.load_state()
                bot_state.safe_persist_bot_state(state=state, system_health='ERROR', bot_status='UNKNOWN')
            except Exception:
                pass
            import traceback; traceback.print_exc()
    finally:
        utils.release_lock(lock)


def _run():
    runner = cycle_runner.CycleRunner(
        out_fn=out,
        analytics=ANALYTICS,
        binance=BINANCE,
        safe_log_open_fn=_safe_log_open,
        safe_log_close_fn=_safe_log_close,
        safe_log_decision_snapshot_fn=_safe_log_decision_snapshot,
        safe_persist_bot_state_fn=_safe_persist_bot_state,
        audit_orphans_fn=_audit_orphans,
        maybe_clean_dust_fn=_maybe_clean_dust,
        check_partial_long_fn=_check_partial_long,
        check_partial_short_fn=_check_partial_short,
        handle_close_fn=_handle_close,
    )
    return runner.run()


# Helpers internos

def _recolocar_oco_long(pos, sym, qty_total, step, price, tp, entry):
    return position_lifecycle.recolocar_oco_long(pos, sym, qty_total, step, price, tp, entry, BINANCE, out)


def _handle_close(state, pos, action, price_close, pnl, btc_ctx=None):
    return position_lifecycle.handle_close(state, pos, action, price_close, pnl, btc_ctx, BINANCE, out, _safe_log_close)


def _check_partial_long(pos, state):
    return position_lifecycle.check_partial_long(pos, state, BINANCE, out, ANALYTICS, _recolocar_oco_long)


def _check_partial_short(pos, state):
    return position_lifecycle.check_partial_short(pos, state, BINANCE, out, ANALYTICS)


def _audit_orphans(state):
    return audit_pipeline.audit_orphans(state, BINANCE, out, _safe_log_open)


def _maybe_clean_dust(state):
    return audit_pipeline.maybe_clean_dust(state, BINANCE, out)


if __name__ == '__main__':
    main()
