#!/usr/bin/env python3
"""Run local observability checks before a real bot cycle."""
import os
import subprocess
import sys
from telegram_alerts import send_telegram_alert


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CHECKS = [
    ('healthcheck', 'healthcheck.py', True),
    ('observability', 'validate_observability.py', True),
    ('analyze_trades', 'analyze_trades.py', False),
    ('analyze_decisions', 'analyze_decisions.py', False),
]


def _run_script(script_name):
    path = os.path.join(BASE_DIR, script_name)
    proc = subprocess.run(
        [sys.executable, path],
        cwd=BASE_DIR,
        text=True,
        capture_output=True,
        check=False,
    )
    output = (proc.stdout or '') + (proc.stderr or '')
    return proc.returncode, output


def _extract_final_status(output):
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.lower() == 'final status:' and index + 1 < len(lines):
            status = lines[index + 1].upper()
            if status in {'OK', 'WARNING', 'ERROR'}:
                return status
    for line in reversed(lines):
        upper = line.upper()
        if upper in {'OK', 'WARNING', 'ERROR'}:
            return upper
    return 'ERROR'


def _final_status(statuses):
    if statuses.get('healthcheck') == 'ERROR' or statuses.get('observability') == 'ERROR':
        return 'ERROR'
    if any(status == 'ERROR' for status in statuses.values()):
        return 'ERROR'
    if any(status == 'WARNING' for status in statuses.values()):
        return 'WARNING'
    return 'OK'


def main():
    statuses = {}
    for name, script, has_final_status in CHECKS:
        returncode, output = _run_script(script)
        if has_final_status:
            status = _extract_final_status(output)
        else:
            status = 'OK' if returncode == 0 else 'ERROR'
        if returncode != 0:
            status = 'ERROR'
        statuses[name] = status

    print('PREFLIGHT CHECK')
    print(f'- healthcheck: {statuses["healthcheck"]}')
    print(f'- observability: {statuses["observability"]}')
    print(f'- analyze_trades: {statuses["analyze_trades"]}')
    print(f'- analyze_decisions: {statuses["analyze_decisions"]}')
    final_status = _final_status(statuses)
    print(f'- final_status: {final_status}')
    if final_status in {'WARNING', 'ERROR'}:
        message = '\n'.join(f'{name}: {status}' for name, status in statuses.items())
        send_telegram_alert(final_status, f'Preflight {final_status}', message)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
