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


class Step01(unittest.TestCase):
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
