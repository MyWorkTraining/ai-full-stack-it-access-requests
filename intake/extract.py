"""Provider integration with a lazy credential boundary and an injectable transport."""
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from .providers import ProviderError
from .validation import SCHEMA, extract_response

PROMPT = Path(__file__).with_name('prompt.txt').read_text()


def read_api_key():
    """Called only by the real provider, never by mock execution or tests."""
    return os.environ["API_KEY"]


def redact(value, secrets=()):
    for secret in secrets:
        if secret:
            value = value.replace(secret, '[REDACTED]')
    return value


def http_transport(request):
    if not request['url'].startswith('https://'):
        raise ProviderError('provider endpoint must use HTTPS')
    req = urllib.request.Request(request['url'], data=json.dumps(request['body']).encode(),
                                 headers=request['headers'], method='POST')
    try:
        with urllib.request.urlopen(req, timeout=request['timeout']) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not persist provider error bodies or request headers: either can contain secrets.
        raise ProviderError(f'provider HTTP {exc.code}', exc.code in (408, 429) or exc.code >= 500) from None
    except (OSError, ValueError):
        raise ProviderError('provider transport failed or returned invalid JSON', True) from None


class ResponsesProvider:
    def __init__(self, model, endpoint='https://api.openai.com/v1/responses', timeout=30,
                 max_output_tokens=4096, transport=None, credential=None):
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.transport = transport or http_transport
        self.credential = credential or read_api_key
        self.response_source = f'openai/{model}'
        self.evidence = None

    def complete(self, document, config=None):
        body = {'model': self.model, 'instructions': PROMPT,
                'input': [{'role': 'user', 'content': json.dumps({'document_id': document['document_id'], 'message': document['message']})}],
                'store': False, 'max_output_tokens': self.max_output_tokens,
                'text': {'format': {'type': 'json_schema', 'name': 'access_request', 'strict': True, 'schema': SCHEMA}}}
        try:
            secret = self.credential()
        except (KeyError, ValueError):
            raise ProviderError('missing provider credential: configure API_KEY') from None
        if not secret:
            raise ProviderError('empty provider credential')
        request = {'url': self.endpoint, 'timeout': self.timeout, 'body': body,
                   'headers': {'Content-Type': 'application/json', 'Authorization': f'Bearer {secret}'}}
        safe = dict(request, headers={'Content-Type': 'application/json', 'Authorization': '[REDACTED]'})
        self.evidence = {'request': safe, 'response_handling': 'completed required; refusal, incomplete, malformed JSON and schema errors rejected',
                         'observation': 'intercepted transport; mock response' if self.transport is not http_transport else 'constructed provider request; response not yet observed'}
        try:
            response = self.transport(request)
        except Exception as exc:
            # Keep explicit retryability, but never persist untrusted provider text.
            raise ProviderError('provider call failed; inspect configuration and retry', getattr(exc, 'retryable', False)) from None
        return response


def settings(provider, extra=None):
    value = {'provider': getattr(provider, 'response_source', 'mock'),
             'model': getattr(provider, 'model', None), 'endpoint': getattr(provider, 'endpoint', None),
             'timeout': getattr(provider, 'timeout', 30), 'max_output_tokens': getattr(provider, 'max_output_tokens', 4096),
             'prompt_sha256': hashlib.sha256(PROMPT.encode()).hexdigest(),
             'schema_sha256': hashlib.sha256(json.dumps(SCHEMA, sort_keys=True).encode()).hexdigest(),
             'policy_sha256': hashlib.sha256((Path(__file__).resolve().parents[1] / 'materials/office-policy.md').read_bytes()).hexdigest()}
    value.update(extra or {})
    return value


def prepare(document, provider, config=None):
    return {'extraction': extract_response(document, provider.complete(document, config)),
            'response_source': provider.response_source, 'settings': settings(provider, config)}
