"""Independent bounded workers with durable attempts and explicit recovery."""
from concurrent.futures import ThreadPoolExecutor
import shlex
import time
from .extract import prepare, settings
from .policy import route
from .providers import ProviderError


class Runner:
    def __init__(self, store, provider, workers=4, timeout=30, max_retries=0,
                 retry_delay=1, clock=time.time, sleep=time.sleep, config=None):
        if workers < 1 or timeout <= 0 or max_retries < 0 or retry_delay < 0:
            raise ValueError('invalid worker, timeout or retry configuration')
        self.store, self.provider = store, provider
        self.workers, self.timeout = workers, timeout
        self.max_retries, self.retry_delay = max_retries, retry_delay
        self.clock, self.sleep = clock, sleep
        self.config = settings(provider, config)
        self.config['timeout'] = timeout
        if hasattr(provider, 'root'):
            self.config['mock_responses'] = str(provider.root.resolve())

    def run(self, documents, force=False):
        attempts = [self.store.begin(doc, self.config, force) for doc in documents]
        self._execute([a for a in attempts if a is not None])
        return self.store.current()

    def _execute(self, attempts):
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(self._work, dict.fromkeys(attempts)))

    def retry_command(self, document_id):
        args = ['python3', '-m', 'intake', 'retry', '--db', self.store.path, '--document', document_id]
        if self.provider.response_source == 'mock':
            args += ['--provider', 'mock', '--responses', str(self.provider.root.resolve())]
        return shlex.join(args)

    def _work(self, attempt):
        row = self.store.get(attempt)
        if not row:
            return
        delay = row['not_before'] - self.clock()
        if delay > 0:
            self.sleep(delay)
        while True:
            token = self.store.claim(attempt, now=self.clock(), lease=self.timeout + 5)
            if not token:
                return
            row = self.store.get(attempt)
            doc = row['document']
            try:
                # Use the persisted producing settings when resuming recorded work.
                prepared = prepare(doc, self.provider, row['settings'])
                result = dict(prepared, **route(doc, prepared['extraction']), error=None)
                result['settings'] = row['settings']
            except Exception as exc:
                if isinstance(exc, ProviderError) and exc.retryable and row['tries'] <= self.max_retries:
                    delay = self.retry_delay * (2 ** (row['tries'] - 1))
                    if not self.store.defer(attempt, token, self.clock() + delay):
                        return
                    self.sleep(delay)
                    continue
                message = str(exc).lower()
                category = 'provider-error' if isinstance(exc, ProviderError) else 'invalid-response'
                for word in ['refusal', 'incomplete', 'malformed', 'structure', 'quote']:
                    if word in message:
                        category = word
                        break
                result = {'extraction': None, 'status': 'error', 'owner': 'IT Service Desk',
                          'next_action': 'retry-extraction', 'error': f'{category}: extraction failed; inspect source and recorded provider settings, then retry',
                          'error_type': category, 'retry_command': self.retry_command(doc['document_id']),
                          'response_source': self.provider.response_source, 'settings': row['settings']}
            self.store.finish(attempt, result, token=token, now=self.clock())
            return

    def resume(self):
        self._execute([row['id'] for row in self.store.pending()])
        return self.store.current()

    def retry(self, document_id):
        history = self.store.history(document_id)
        if not history:
            raise ValueError(f'unknown document: {document_id}')
        return self.run([history[-1]['document']], force=True)
