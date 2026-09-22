"""Canonical lifecycle lock for unresolved managed Spot LONG recovery."""


def is_spot_long_recovery_pending(pos):
    return bool(
        isinstance(pos, dict)
        and str(pos.get('direction') or '').lower() == 'long'
        and pos.get('recovery_pending') is True
    )


def spot_long_recovery_kind(pos):
    """Select a reconciler only from explicit, non-conflicting evidence."""
    if not is_spot_long_recovery_pending(pos):
        return 'NONE'
    partial = pos.get('partial_spot_recovery')
    entry = pos.get('entry_spot_recovery')
    if partial is not None and entry is not None:
        return 'UNKNOWN'
    if isinstance(partial, dict) and partial.get('kind') == 'partial_long_spot_v1':
        return 'PARTIAL_EXIT'
    if isinstance(entry, dict) and entry.get('kind') == 'entry_protection_v1':
        if entry.get('sell_attempted') and entry.get('status') not in {
            'ENTRY_EXIT_ZERO_CONFIRMED', 'ENTRY_OCO_CREATE_UNCONFIRMED',
            'ENTRY_OCO_AWAITING_CONFIRMATION',
        }:
            return 'ENTRY_EMERGENCY_EXIT'
        return 'ENTRY_PROTECTION'
    if pos.get('preventive_close_status'):
        return 'PREVENTIVE_EXIT_UNKNOWN'
    return 'UNKNOWN'
