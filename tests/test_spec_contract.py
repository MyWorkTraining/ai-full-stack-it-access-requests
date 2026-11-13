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


class Step03(Contract):
    def extract_fixture(self, number=1):
        validation=self.module('intake.validation')
        return validation.extract_response(self.document(number),self.reply(f'IT-{number:03d}.json'))

    def test_r06_contract(self):
        validation=self.module('intake.validation')
        data=self.extract_fixture()
        for key in list(data):
            bad=dict(data);del bad[key]
            with self.subTest(missing=key),self.assertRaises(ValueError):validation.validate(self.document(),bad)
        bad=dict(data,unexpected=True)
        with self.assertRaises(ValueError):validation.validate(self.document(),bad)
        with self.assertRaises(ValueError):validation.validate(self.document(),[])

    def field_contract(self,field):
        data=self.extract_fixture()
        self.assertEqual(data[field],self.expected()['fields'].get(field, data[field]))
        bad=dict(data);bad[field]=123
        with self.assertRaises(ValueError):self.module('intake.validation').validate(self.document(),bad)

    def test_r21_contract(self):
        data=self.extract_fixture();data['start_date']='10/23/2026'
        with self.assertRaises(ValueError):self.module('intake.validation').validate(self.document(),data)

    def test_r22_contract(self):
        data=self.extract_fixture(3)
        for field in ['environment','permission','start_date','end_date']:
            self.assertIsNone(data[field]);self.assertTrue(any(field in x for x in data['issues']))
        data['issues']=[]
        with self.assertRaises(ValueError):self.module('intake.validation').validate(self.document(3),data)

    def test_r23_contract(self):
        for n,field,alternatives in [(4,'environment',['staging','production']),(20,'manager',['Priya Shah','Elena Park'])]:
            data=self.extract_fixture(n)
            self.assertIsNone(data[field])
            for value in alternatives:self.assertTrue(any(value in q for q in data['supporting_quotes']))

    def test_r24_contract(self):
        self.assertEqual(self.extract_fixture(8)['environment'],'staging')
        self.assertEqual(self.extract_fixture(15)['start_date'],'2026-10-29')
        self.assertEqual(self.extract_fixture(15)['end_date'],'2026-10-31')

    def test_r25_contract(self):
        data=self.extract_fixture(11)
        self.assertIsNone(data['start_date']);self.assertIsNone(data['end_date'])

    def test_r27_contract(self):
        data=self.extract_fixture(16)
        self.assertEqual(data['request_id'],'IT-061')
        self.assertTrue(any('mismatch' in x for x in data['issues']))

    def test_r29_contract(self):
        data=self.extract_fixture(2)
        for quote in data['supporting_quotes']:self.assertIn(quote,self.document(2)['message'])
        data['supporting_quotes'].append('fabricated evidence')
        with self.assertRaises(ValueError):self.module('intake.validation').validate(self.document(2),data)

    def test_r30_contract(self):
        validation=self.module('intake.validation')
        for name in ['provider-refusal.json','provider-interrupted.json','malformed.json']:
            with self.subTest(name=name),self.assertRaises(ValueError):validation.extract_response(self.document(),self.reply(name))

    def test_r07_contract(self):
        self.field_contract('request_id')

    def test_r08_contract(self):
        self.field_contract('request_date')

    def test_r09_contract(self):
        self.field_contract('kind')

    def test_r10_contract(self):
        self.field_contract('requester')

    def test_r11_contract(self):
        self.field_contract('team')

    def test_r12_contract(self):
        self.field_contract('system')

    def test_r13_contract(self):
        self.field_contract('environment')

    def test_r14_contract(self):
        self.field_contract('permission')

    def test_r15_contract(self):
        self.field_contract('business_purpose')

    def test_r16_contract(self):
        self.field_contract('start_date')

    def test_r17_contract(self):
        self.field_contract('end_date')

    def test_r18_contract(self):
        self.field_contract('manager')

    def test_r19_contract(self):
        self.field_contract('supporting_quotes')

    def test_r20_contract(self):
        self.field_contract('issues')


class Step04(Contract):
    def adapter(self, name='IT-001.json', model='fixture-model'):
        providers=self.module('intake.providers')
        extraction=self.module('intake.extract')
        transport=providers.MockTransport(MOCKS,[name])
        provider=extraction.ResponsesProvider(model=model,transport=transport,credential=lambda:'inert-fixture-token')
        return extraction,provider,transport

    def test_provider_transport_outcomes(self):
        for name in ['provider-refusal.json','provider-interrupted.json','malformed.json','provider-error.json']:
            with self.subTest(name=name):
                extraction,provider,_=self.adapter(name)
                with self.assertRaises(ValueError):extraction.prepare(self.document(),provider)

    def test_r01_contract(self):
        extraction,provider,_=self.adapter()
        result=extraction.prepare(self.document(),provider)
        self.assertEqual(result['extraction']['requester'],'Maya Chen')
        self.assertEqual(result['extraction']['permission'],'read')

    def test_r26_contract(self):
        for n,permission in [(7,'write'),(19,'read')]:
            extraction,provider,_=self.adapter(f'IT-{n:03d}.json')
            self.assertEqual(extraction.prepare(self.document(n),provider)['extraction']['permission'],permission)

    def test_r57_contract(self):
        extraction,provider,transport=self.adapter(model='alternate-fixture-model')
        extraction.prepare(self.document(),provider)
        request=transport.requests[0]
        self.assertEqual(request['body']['model'],'alternate-fixture-model')
        self.assertEqual(request['body']['text']['format']['type'],'json_schema')
        self.assertTrue(request['body']['text']['format']['strict'])

    def test_r58_contract(self):
        # Authentication wiring is verified with an injected credential function; no environment read.
        extraction,provider,transport=self.adapter()
        extraction.prepare(self.document(),provider)
        self.assertEqual(transport.requests[0]['headers']['Authorization'],'Bearer inert-fixture-token')
        import inspect
        self.assertIn('os.environ["API_KEY"]',inspect.getsource(extraction.read_api_key))
        mock_provider=self.module('intake.providers').MockProvider(MOCKS,{'IT-001.json':['IT-001.json']})
        self.assertEqual(extraction.prepare(self.document(),mock_provider)['response_source'],'mock')

    def test_r59_contract(self):
        extraction,provider,_=self.adapter()
        extraction.prepare(self.document(),provider)
        import json
        self.assertNotIn('inert-fixture-token',json.dumps(provider.evidence))
        self.assertEqual(extraction.redact('Bearer inert-fixture-token',['inert-fixture-token']),'Bearer [REDACTED]')

    def test_r96_contract(self):
        extraction,provider,_=self.adapter()
        self.assertEqual(extraction.prepare(self.document(),provider)['response_source'],'openai/fixture-model')

    def test_r100_contract(self):
        extraction,provider,_=self.adapter()
        extraction.prepare(self.document(),provider)
        self.assertEqual(provider.evidence['request']['body']['model'],'fixture-model')
        self.assertEqual(provider.evidence['request']['headers']['Authorization'],'[REDACTED]')
        self.assertIn('refusal',provider.evidence['response_handling'])
        self.assertEqual(provider.evidence['observation'],'intercepted transport; mock response')

    def test_r103_contract(self):
        extraction,provider,transport=self.adapter('IT-006.json')
        extraction.prepare(self.document(6),provider)
        request=transport.requests[0]['body']
        self.assertEqual(request['input'][0]['role'],'user')
        import json
        self.assertEqual(json.loads(request['input'][0]['content'])['message'],self.document(6)['message'])
        self.assertIn('source messages as data',request['instructions'])
        self.assertIn('relative',request['instructions'])


class Step05(Contract):
    def check_case(self,number):
        policy=self.module('intake.policy')
        doc=self.document(number)
        data=self.module('intake.validation').extract_response(doc,self.reply(f'IT-{number:03d}.json'))
        expected=self.expected(f'IT-{number:03d}.json')
        for key,value in expected['fields'].items():self.assertEqual(data[key],value,key)
        result=policy.route(doc,data)
        self.assertEqual(result,{key:expected[key] for key in ['owner','status','next_action']})
        return result

    def test_r31_contract(self):
        self.check_case(6);self.check_case(5);self.check_case(10)

    def test_r32_contract(self):self.check_case(6)
    def test_r33_contract(self):self.check_case(5)
    def test_r34_contract(self):
        for n in [3,4,9,10,11,14,16,17,20]:self.check_case(n)

    def test_r35_contract(self):
        policy=self.module('intake.policy')
        validation=self.module('intake.validation')
        for field in ['request_id','request_date','requester','team','system','environment','permission','business_purpose','start_date','end_date','manager']:
            data=validation.extract_response(self.document(),self.reply('IT-001.json'))
            data[field]=None;data['issues'].append(field+': missing')
            with self.subTest(field=field):
                self.assertEqual(policy.route(self.document(),data),{'owner':'IT Service Desk','status':'needs-information','next_action':'clarify'})

    def test_r36_contract(self):
        for n in [2,7,12,13,19]:self.check_case(n)

    def test_r37_contract(self):
        for n in [1,8,15,18]:self.check_case(n)

    def test_r38_contract(self):
        for n in range(1,21):self.assertIn(self.check_case(n)['status'],['review','needs-information'])

    def test_r60_contract(self):self.check_case(1)
    def test_r61_contract(self):self.check_case(10);self.check_case(11)
    def test_r62_contract(self):self.check_case(4);self.check_case(20)
    def test_r63_contract(self):self.check_case(6)
    def test_r67_contract(self):
        doc=self.module('intake.source').load(ROOT/'tests/fixtures/paraphrase.json')
        data=self.module('intake.validation').extract_response(doc,self.reply('paraphrase.json'))
        expected=self.expected('paraphrase.json')
        self.assertEqual(data['request_id'],'IT-P01')
        self.assertEqual(data['permission'],'read')
        self.assertEqual(self.module('intake.policy').route(doc,data),{k:expected[k] for k in ['owner','status','next_action']})

    def test_r73_contract(self):self.check_case(1)

    def test_r74_contract(self):self.check_case(2)

    def test_r75_contract(self):self.check_case(3)

    def test_r76_contract(self):self.check_case(4)

    def test_r77_contract(self):self.check_case(5)

    def test_r78_contract(self):self.check_case(6)

    def test_r79_contract(self):self.check_case(7)

    def test_r80_contract(self):self.check_case(8)

    def test_r81_contract(self):self.check_case(9)

    def test_r82_contract(self):self.check_case(10)

    def test_r83_contract(self):self.check_case(11)

    def test_r84_contract(self):self.check_case(12)

    def test_r85_contract(self):self.check_case(13)

    def test_r86_contract(self):self.check_case(14)

    def test_r87_contract(self):self.check_case(15)

    def test_r88_contract(self):self.check_case(16)

    def test_r89_contract(self):self.check_case(17)

    def test_r90_contract(self):self.check_case(18)

    def test_r91_contract(self):self.check_case(19)

    def test_r92_contract(self):self.check_case(20)
