"""Plan-owned contracts: disk response fixtures, no network or credential reads."""
import importlib
import importlib.util
import os
from pathlib import Path
import socket
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / 'tests' / 'mock_responses'


class Contract(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')).start()
        original_getitem = os._Environ.__getitem__

        def guarded_getitem(environment, key):
            if key == 'API_KEY':
                raise AssertionError('tests must not read API_KEY')
            return original_getitem(environment, key)

        mock.patch.object(os._Environ, '__getitem__', guarded_getitem).start()

    def providers(self):
        self.assertIsNotNone(
            importlib.util.find_spec('intake.providers'),
            'Step 1 mock provider behavior has not been implemented',
        )
        return importlib.import_module('intake.providers')

    def module(self, name):
        self.assertIsNotNone(importlib.util.find_spec(name), f'{name} behavior has not been implemented')
        return importlib.import_module(name)

    def reply(self, name):
        import json
        return json.loads((MOCKS / name).read_text())

    def document(self, number=1):
        return self.module('intake.source').load(ROOT / f'materials/requests/IT-{number:03d}.json')

    def expected(self, name='IT-001.json'):
        import json
        return json.loads((ROOT / 'tests/expected' / name).read_text())


class Step01(Contract):
    def test_mock_provider_contract(self):
        """TP-01: ordered saved success/error responses and explicit unscripted errors."""
        providers = self.providers()
        provider = providers.MockProvider(
            MOCKS,
            {'one.json': ['provider-complete.json', 'provider-error.json']},
        )
        self.assertEqual(provider.complete({'document_id': 'one.json'})['status'], 'completed')
        with self.assertRaisesRegex(providers.ProviderError, 'temporarily unavailable'):
            provider.complete({'document_id': 'one.json'})
        self.assertEqual(provider.calls, ['one.json', 'one.json'])
        with self.assertRaisesRegex(providers.ProviderError, 'unscripted'):
            provider.complete({'document_id': 'unknown.json'})

    def test_mock_transport_contract(self):
        """TP-01: saved refusal/interruption responses and exact request recording."""
        providers = self.providers()
        transport = providers.MockTransport(
            MOCKS, ['provider-refusal.json', 'provider-interrupted.json'],
        )
        refusal = transport({'model': 'fixture-model'})
        self.assertEqual(refusal['status'], 'completed')
        self.assertEqual(refusal['output'][0]['content'][0]['type'], 'refusal')
        self.assertEqual(transport({'model': 'fixture-model'})['status'], 'incomplete')
        self.assertEqual(transport.requests, [{'model': 'fixture-model'}, {'model': 'fixture-model'}])


class Step02(Contract):
    def test_r03_contract(self):
        import json
        import tempfile
        source = self.module('intake.source')
        for path in sorted((ROOT / 'materials/requests').glob('*.json')):
            self.assertIsInstance(source.load(path)['message'], str)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fresh.json'
            path.write_text(json.dumps({'message': ' '.join(['word'] * 200)}))
            self.assertEqual(len(source.load(path)['message'].split()), 200)

    def test_r04_contract(self):
        import json
        import tempfile
        source = self.module('intake.source')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'metadata.json'
            path.write_text(json.dumps({'message':'No explicit request date.', 'exported_at':'2026-10-22', 'subject':'production admin'}))
            doc=source.load(path)
            self.assertEqual(doc['message'], 'No explicit request date.')
            self.assertEqual(doc['metadata']['exported_at'], '2026-10-22')
            self.assertNotIn('request_date',doc)

    def test_r05_contract(self):
        doc=self.document(16)
        self.assertEqual(doc['document_id'],'IT-016.json')
        self.assertIn('IT-061',doc['message'])

    def test_r28_contract(self):
        import tempfile
        source=self.module('intake.source')
        with tempfile.TemporaryDirectory() as directory:
            docs=[]
            for name in ['one.json','two.json']:
                path=Path(directory)/name
                path.write_bytes((ROOT/'materials/requests/IT-001.json').read_bytes())
                docs.append(source.load(path))
            self.assertEqual([d['document_id'] for d in docs],['one.json','two.json'])
            self.assertEqual(docs[0]['message'],docs[1]['message'])

    def test_r46_contract(self):
        import tempfile
        import hashlib
        source=self.module('intake.source')
        path=ROOT/'materials/requests/IT-001.json'
        original=source.load(path)
        self.assertEqual(original['source_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            changed=Path(directory)/path.name
            changed.write_bytes(path.read_bytes()+b'\n')
            self.assertNotEqual(source.load(changed)['source_sha256'],original['source_sha256'])
