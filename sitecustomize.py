"""Process-level safety defaults for local test runs.

Python imports this module automatically when the repository root is on
sys.path. It only sets notification safety flags for test runners.
"""
import os
import sys


def _argv_indicates_test():
    text = ' '.join(str(arg).lower() for arg in sys.argv)
    return 'unittest' in text or 'discover' in text or 'pytest' in text


if _argv_indicates_test():
    os.environ.setdefault('BINANCEBOT_TEST_MODE', 'true')
    os.environ.setdefault('BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS', 'true')
