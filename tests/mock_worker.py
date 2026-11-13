"""Subprocess test harness: mock-only application operations with credential/network guards."""
import os
from pathlib import Path
import socket
import ssl  # Define SSLSocket before replacing the socket constructor.
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
original_getitem = os._Environ.__getitem__

def guarded_getitem(environment, key):
    if key == 'API_KEY':
        raise AssertionError('subprocess must not read API_KEY')
    return original_getitem(environment, key)

def forbidden_socket(*args, **kwargs):
    raise AssertionError('subprocess network forbidden')

os._Environ.__getitem__ = guarded_getitem
socket.socket = forbidden_socket

if sys.argv[1] == 'worker':
    from intake.providers import MockProvider
    from intake.runner import Runner
    from intake.source import load
    from intake.store import Store
    Runner(Store(sys.argv[2]), MockProvider(ROOT / 'tests/mock_responses')).run([load(ROOT / 'materials/requests/IT-001.json')])
else:
    import runpy
    sys.argv = ['intake', *sys.argv[1:]]
    runpy.run_module('intake', run_name='__main__')
