"""Disk-scripted providers and injectable HTTP transport boundaries."""
from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import threading


class ProviderError(ValueError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class MockTransport:
    def __init__(self, root, responses, barriers=None):
        self.root = Path(root)
        self.responses = deque(responses)
        self.requests = []
        self.barriers = barriers or {}
        self.lock = threading.Lock()

    def __call__(self, request):
        with self.lock:
            self.requests.append(deepcopy(request))
            if not self.responses:
                raise ProviderError('unscripted transport call')
            name = self.responses.popleft()
        response = json.loads((self.root / name).read_text(encoding='utf-8'))
        if response.get('event') == 'file-block':
            gates = request.get('config', {}).get('mock_gates')
            if not gates:
                raise ProviderError('file-block fixture requires --mock-gates')
            FileEvent(Path(gates) / response['entered']).set()
            if not FileEvent(Path(gates) / response['barrier']).wait(request.get('config', {}).get('timeout', 30)):
                raise ProviderError('provider response timeout', True)
            response = json.loads((self.root / response['response']).read_text())
        if response.get('event') == 'block':
            if response.get('entered') in self.barriers:
                self.barriers[response['entered']].set()
            timeout = request.get('timeout', request.get('config', {}).get('timeout', 30))
            if not self.barriers[response['barrier']].wait(timeout):
                raise ProviderError('provider response timeout', True)
            response = json.loads((self.root / response['response']).read_text())
        if response.get('event') == 'error':
            raise ProviderError(response['error'], response.get('retryable', False))
        return response


class MockProvider:
    response_source = 'mock'

    def __init__(self, root, scripts=None, barriers=None):
        self.root = Path(root)
        if scripts is None:
            scripts = json.loads((self.root / 'manifest.json').read_text())
        self.scripts = {key: MockTransport(root, values, barriers) for key, values in scripts.items()}
        self.calls = []
        self.lock = threading.Lock()

    def complete(self, document, config=None):
        name = document['document_id']
        with self.lock:
            self.calls.append(name)
        if name not in self.scripts:
            raise ProviderError(f'unscripted document: {name}')
        return self.scripts[name]({'message': document.get('message', ''), 'config': config or {}})


class FileEvent:
    """Development-only cross-process barrier used by saved mock scenarios."""
    def __init__(self, path):
        self.path = Path(path)

    def set(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def wait(self, timeout):
        import time
        deadline = time.monotonic() + timeout
        while not self.path.exists():
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        return True
