#!/usr/bin/env python3
"""Central guard for external notifications during tests."""
import logging
import os
import sys


TEST_MODE_VARS = (
    'BINANCEBOT_TEST_MODE',
    'BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS',
)


def _truthy(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def argv_indicates_test():
    text = ' '.join(str(arg).lower() for arg in sys.argv)
    return 'unittest' in text or 'discover' in text or 'pytest' in text


def external_notifications_disabled():
    if any(_truthy(os.environ.get(name)) for name in TEST_MODE_VARS):
        return True
    return argv_indicates_test()


def suppression_reason():
    if any(_truthy(os.environ.get(name)) for name in TEST_MODE_VARS):
        return 'test mode env'
    if argv_indicates_test():
        return 'test runner argv'
    return None


def log_suppressed(channel='external'):
    reason = suppression_reason() or 'unknown'
    logging.info('external notification suppressed in test mode channel=%s reason=%s', channel, reason)
