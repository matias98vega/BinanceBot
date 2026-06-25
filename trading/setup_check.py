#!/usr/bin/env python3
"""Deployment readiness check for BinanceBot."""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from config_loader import ENV_FILES, ConfigError, load_config, validate_environment


REQUIRED_PYTHON = (3, 10)
REQUIRED_MODULES = [
    'argparse',
    'csv',
    'dataclasses',
    'hashlib',
    'hmac',
    'json',
    'os',
    'subprocess',
    'urllib.request',
]


def _status(ok):
    return 'OK' if ok else 'ERROR'


def _check_python():
    return sys.version_info >= REQUIRED_PYTHON, f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'


def _check_dependencies():
    missing = []
    for module in REQUIRED_MODULES:
        try:
            __import__(module)
        except Exception:
            missing.append(module)
    return not missing, ', '.join(missing)


def _check_environment():
    env_present = any(os.path.exists(path) for path in ENV_FILES)
    try:
        config = validate_environment(require_api=True)
    except ConfigError as exc:
        return False, str(exc), None, env_present
    return True, '', config, env_present


def _ensure_parent_writable(path):
    parent = os.path.dirname(path) or '.'
    if not os.path.exists(parent):
        return False
    return os.access(parent, os.W_OK)


def _check_files(config):
    paths = [
        config.state_file,
        config.analytics_file,
        config.decision_snapshots_file,
    ]
    missing = [path for path in paths if not os.path.exists(path)]
    unwritable = [path for path in paths if not _ensure_parent_writable(path)]
    return not missing and not unwritable, missing, unwritable


def _request_json(req_or_url, timeout=10):
    with urllib.request.urlopen(req_or_url, timeout=timeout) as response:
        body = response.read().decode('utf-8')
    return json.loads(body) if body else {}


def _check_ping(config):
    try:
        _request_json(f'{config.spot_base}/api/v3/ping', timeout=10)
        return True, ''
    except Exception as exc:
        return False, str(exc)


def _server_time(config):
    try:
        data = _request_json(f'{config.spot_base}/api/v3/time', timeout=10)
        return int(data['serverTime'])
    except Exception:
        return int(time.time() * 1000)


def _check_api_auth(config):
    params = {
        'timestamp': _server_time(config),
        'recvWindow': 10000,
    }
    query = urllib.parse.urlencode(params)
    signature = hmac.new(config.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f'{config.spot_base}/api/v3/account?{query}&signature={signature}'
    req = urllib.request.Request(url, headers={'X-MBX-APIKEY': config.api_key})
    try:
        _request_json(req, timeout=10)
        return True, ''
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode('utf-8')
        except Exception:
            body = str(exc)
        return False, f'HTTP {exc.code}: {body}'
    except Exception as exc:
        return False, str(exc)


def main():
    checks = {}

    python_ok, python_version = _check_python()
    checks['Python'] = python_ok

    deps_ok, deps_detail = _check_dependencies()
    checks['Dependencies'] = deps_ok

    env_ok, env_error, config, env_present = _check_environment()
    checks['Environment'] = env_ok and env_present

    if config is None:
        config = load_config(require_api=False)

    files_ok, missing_files, unwritable_files = _check_files(config)
    checks['Files'] = files_ok

    ping_ok, ping_error = _check_ping(config)
    checks['Binance Ping'] = ping_ok

    api_ok = False
    api_error = ''
    if env_ok:
        api_ok, api_error = _check_api_auth(config)
    else:
        api_error = env_error
    checks['API Authentication'] = api_ok

    ready = all(checks.values())

    print('SETUP CHECK')
    print(f'Python ............ {_status(checks["Python"])}')
    print(f'Dependencies ...... {_status(checks["Dependencies"])}')
    print(f'Environment ....... {_status(checks["Environment"])}')
    print(f'Files ............. {_status(checks["Files"])}')
    print(f'Binance Ping ...... {_status(checks["Binance Ping"])}')
    print(f'API Authentication. {_status(checks["API Authentication"])}')
    print(f'Final Status ...... {"READY" if ready else "NOT READY"}')

    details = []
    if not python_ok:
        details.append(f'Python version is {python_version}; required >= {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}')
    if not deps_ok:
        details.append(f'Missing dependencies: {deps_detail}')
    if not env_present:
        details.append('Missing .env file')
    if env_error:
        details.append(env_error)
    if missing_files:
        details.append('Missing files: ' + ', '.join(missing_files))
    if unwritable_files:
        details.append('Unwritable file directories: ' + ', '.join(unwritable_files))
    if ping_error:
        details.append('Binance ping error: ' + ping_error)
    if api_error:
        details.append('API authentication error: ' + api_error)
    if details:
        print('')
        print('Details:')
        for detail in details:
            print(f'- {detail}')

    return 0 if ready else 1


if __name__ == '__main__':
    raise SystemExit(main())
