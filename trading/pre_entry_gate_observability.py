"""Caller-owned observability for the pure pre-entry safety gate."""
import decision_timeline
import pre_entry_safety_gate


def record_result(result, state, cycle_id=None):
    summary = {
        'status': result.get('status'), 'safe_to_enter': result.get('safe_to_enter'),
        'mode': result.get('mode'), 'symbol': result.get('symbol'), 'side': result.get('side'),
        'observed_at': result.get('observed_at'), 'blocking_reasons': result.get('blocking_reasons') or [],
        'freshness': result.get('freshness') or {}, 'duration_ms': result.get('duration_ms'),
    }
    state['_last_pre_entry_gate'] = summary
    level = 'INFO' if result.get('safe_to_enter') or result.get('status') == 'BLOCKED_CAPACITY' else 'WARNING'
    try:
        decision_timeline.record_event(
            'pre_entry_safety_gate',
            f'Pre-entry {result.get("side")} {result.get("symbol")}: {result.get("status")}',
            level=level, category='RISK', cycle_id=cycle_id,
            details={**summary, 'checks': {name: {'passed': item.get('passed'), 'status_code': item.get('status_code')}
                                          for name, item in (result.get('checks') or {}).items()}},
        )
    except Exception:
        pass
    return summary


def evaluate_and_record(*, local_state, cycle_id=None, **kwargs):
    result = pre_entry_safety_gate.evaluate_pre_entry_safety(local_state=local_state, **kwargs)
    record_result(result, local_state, cycle_id=cycle_id)
    return result
