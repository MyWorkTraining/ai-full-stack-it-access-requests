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
        doc=self.module('intake.source').load(ROOT/'tests/fixtures/IT-P01.json')
        data=self.module('intake.validation').extract_response(doc,self.reply('paraphrase.json'))
        expected=self.expected('IT-P01.json')
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


class Step06(Contract):
    def setUp(self):
        super().setUp()
        import tempfile
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.db=Path(self.directory.name)/'intake.sqlite'

    def save(self,store,doc=None,config=None,provider=None):
        doc=doc or self.document()
        provider=provider or self.module('intake.providers').MockProvider(MOCKS,{'IT-001.json':['IT-001.json']*3})
        extraction=self.module('intake.extract')
        settings=extraction.settings(provider,config)
        attempt=store.begin(doc,settings)
        if attempt is not None:
            prepared=extraction.prepare(doc,provider,config)
            result=dict(prepared,**self.module('intake.policy').route(doc,prepared['extraction']),error=None)
            store.finish(attempt,result)
        return attempt

    def test_r47_contract(self):
        store=self.module('intake.store').Store(self.db)
        provider=self.module('intake.providers').MockProvider(MOCKS,{'IT-001.json':['IT-001.json']})
        self.assertIsNotNone(self.save(store,provider=provider))
        self.assertIsNone(self.save(store,provider=provider))
        self.assertEqual(provider.calls,['IT-001.json'])
        self.assertEqual(len(store.current()),1)

    def test_r48_contract(self):
        import json
        store=self.module('intake.store').Store(self.db)
        self.save(store)
        doc=self.document()
        revised=Path(self.directory.name)/'IT-001.json'
        revised.write_text(json.dumps({'message':doc['message']+' Correction: replace the access end date above with 2026-11-09.'}))
        provider=self.module('intake.providers').MockProvider(MOCKS,{'IT-001.json':['IT-001-revised.json']})
        self.save(store,self.module('intake.source').load(revised),provider=provider)
        self.assertEqual(store.current()[0]['extraction']['end_date'],'2026-11-09')
        history=store.history('IT-001.json')
        self.assertEqual(history[0]['result']['extraction']['end_date'],'2026-11-06')
        self.assertNotEqual(history[0]['document']['source_sha256'],history[1]['document']['source_sha256'])

    def test_r49_contract(self):
        store=self.module('intake.store').Store(self.db)
        self.save(store)
        self.save(store,self.document(2),provider=self.module('intake.providers').MockProvider(MOCKS,{'IT-002.json':['IT-002.json']}))
        self.save(store,self.document(2),provider=self.module('intake.providers').MockProvider(MOCKS,{'IT-002.json':['IT-002.json']}))
        self.assertEqual([r['document_id'] for r in store.current()],['IT-001.json','IT-002.json'])

    def test_r51_contract(self):
        store=self.module('intake.store').Store(self.db)
        self.save(store,config={'model':'fixture-A'})
        self.assertIsNotNone(self.save(store,config={'model':'fixture-B'}))
        self.assertIsNotNone(self.save(store,config={'model':'fixture-B','policy_sha256':'test-policy-version-B'}))
        self.assertEqual(len(store.history('IT-001.json')),3)

    def test_r52_contract(self):
        store=self.module('intake.store').Store(self.db)
        self.save(store,config={'model':'fixture-A','policy_sha256':'A'})
        self.save(store,config={'model':'fixture-B','policy_sha256':'B'})
        history=store.history('IT-001.json')
        self.assertEqual([h['settings']['model'] for h in history],['fixture-A','fixture-B'])
        self.assertEqual([h['settings']['policy_sha256'] for h in history],['A','B'])
        self.assertEqual(store.current()[0]['settings']['model'],'fixture-B')

    def test_r65_contract(self):self.test_r47_contract()
    def test_r66_contract(self):self.test_r48_contract()


class Step07(Contract):
    def setUp(self):
        super().setUp()
        import tempfile
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.db=Path(self.directory.name)/'intake.sqlite'

    def runner(self,scripts,**options):
        runner=self.module('intake.runner')
        store=self.module('intake.store').Store(self.db)
        provider=self.module('intake.providers').MockProvider(MOCKS,scripts,options.pop('barriers',None))
        return runner.Runner(store,provider,**options),store,provider

    def failure(self,name):
        runner,store,_=self.runner({'IT-001.json':[name]})
        runner.run([self.document()])
        row=store.current()[0]
        self.assertEqual({k:row[k] for k in ['extraction','status','owner','next_action']},{'extraction':None,'status':'error','owner':'IT Service Desk','next_action':'retry-extraction'})
        self.assertTrue(row['error']);self.assertIn('--document IT-001.json',row['retry_command'])
        return row

    def test_r39_contract(self):self.failure('provider-error.json')
    def test_r40_contract(self):
        self.failure('malformed.json')
        runner,store,_=self.runner({'IT-001.json':['schema-invalid.json']})
        runner.run([self.document()],force=True)
        self.assertEqual(store.current()[0]['status'],'error')
    def test_r41_contract(self):self.failure('provider-rate-limit.json')
    def test_r42_contract(self):self.failure('provider-refusal.json')
    def test_r43_contract(self):self.failure('provider-interrupted.json')

    def test_r44_contract(self):
        import threading
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        runner,store,_=self.runner({'IT-001.json':['blocked.json'],'IT-002.json':['IT-002.json']},barriers={'release':release,'entered':entered},workers=2)
        original=store.finish
        def observe(attempt,result,**kwargs):
            answer=original(attempt,result,**kwargs)
            if store.get(attempt)['document_id']=='IT-002.json':finished.set()
            return answer
        store.finish=observe
        thread=threading.Thread(target=runner.run,args=([self.document(),self.document(2)],))
        thread.start()
        try:
            self.assertTrue(entered.wait(2));self.assertTrue(finished.wait(2))
            self.assertEqual([r['document_id'] for r in store.current()],['IT-002.json'])
            self.assertEqual(store.current()[0]['owner'],'Security Review')
        finally:release.set();thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_r45_contract(self):
        row=self.failure('provider-error.json')
        self.assertIn(str(self.db),row['retry_command'])
        runner,store,_=self.runner({'IT-001.json':['IT-001.json']})
        runner.retry('IT-001.json')
        self.assertEqual(store.current()[0]['status'],'review')

    def test_r50_contract(self):
        runner,store,_=self.runner({'IT-001.json':['IT-001.json','provider-error.json']})
        runner.run([self.document()]);runner.retry('IT-001.json')
        self.assertIsNone(store.current()[0]['extraction'])
        history=store.history('IT-001.json')
        self.assertEqual(history[0]['result']['extraction']['end_date'],'2026-11-06')
        self.assertEqual(history[1]['result']['status'],'error')
        self.assertNotEqual(history[0]['id'],history[1]['id'])

    def test_r54_contract(self):
        runner,store,provider=self.runner({'IT-001.json':['IT-001.json'],'IT-002.json':['IT-002.json']})
        runner.run([self.document()])
        attempt=store.begin(self.document(2),self.module('intake.extract').settings(provider))
        store.claim(attempt,now=0,lease=1)
        runner.resume()
        self.assertEqual(provider.calls,['IT-001.json','IT-002.json'])
        self.assertEqual(len(store.current()),2)

    def test_r56_contract(self):
        self.failure('provider-error.json')
        runner,store,_=self.runner({'IT-001.json':['IT-001.json']})
        runner.retry('IT-001.json');self.assertEqual(store.current()[0]['status'],'review')

    def test_r64_contract(self):
        runner,store,_=self.runner({'IT-001.json':['IT-001.json'],'IT-002.json':['provider-error.json']})
        runner.run([self.document(),self.document(2)])
        self.assertEqual([r['status'] for r in store.current()],['review','error'])

    def test_r68_contract(self):
        self.failure('provider-refusal.json');self.failure('provider-interrupted.json')
    def test_r69_contract(self):self.test_r50_contract()

    def test_retry_budget(self):
        clock=[10.0]
        runner,store,provider=self.runner({'IT-001.json':['provider-rate-limit.json','IT-001.json']},max_retries=1,clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        runner.run([self.document()])
        self.assertEqual(provider.calls,['IT-001.json','IT-001.json'])
        self.assertEqual(store.current()[0]['status'],'review')


class Step08(Contract):
    def setUp(self):
        super().setUp()
        import tempfile
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.db=Path(self.directory.name)/'intake.sqlite'

    def test_r53_contract(self):
        import subprocess
        import sys
        store=self.module('intake.store').Store(self.db)
        environment={k:os.environ[k] for k in os.environ if k!='API_KEY'}
        workers=[subprocess.Popen([sys.executable,str(ROOT/'tests/mock_worker.py'),'worker',str(self.db)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment) for _ in range(2)]
        for process in workers:
            out,err=process.communicate(timeout=10)
            self.assertEqual(process.returncode,0,(out,err))
        self.assertEqual(len(store.current()),1)
        self.assertEqual(store.current()[0]['owner'],'System Owner')
        provider=self.module('intake.providers').MockProvider(MOCKS)
        attempt=store.begin(self.document(),self.module('intake.extract').settings(provider),force=True)
        old=store.claim(attempt,now=0,lease=1)
        new=store.claim(attempt,now=2,lease=10)
        self.assertNotEqual(old,new)
        prepared=self.module('intake.extract').prepare(self.document(),provider)
        result=dict(prepared,status='review',owner='System Owner',next_action='review-request',error=None)
        self.assertFalse(store.finish(attempt,result,token=old))
        self.assertTrue(store.finish(attempt,result,token=new,now=3))
        self.assertEqual(len(store.history('IT-001.json')),2)

    def test_r55_contract(self):
        export=self.module('intake.export')
        import json
        store=self.module('intake.store').Store(self.db)
        provider=self.module('intake.providers').MockProvider(MOCKS,{'IT-001.json':['IT-001.json','IT-001-revised.json']})
        runner=self.module('intake.runner').Runner(store,provider)
        runner.run([self.document()])
        output=Path(self.directory.name)/'deliverables'
        export.publish(store,output)
        original=self.document()['source_sha256']
        revised=Path(self.directory.name)/'IT-001.json'
        revised.write_text(json.dumps({'message':self.document()['message']+' Correction: replace the access end date above with 2026-11-09.'}))
        runner.run([self.module('intake.source').load(revised)])
        for stage in ['after-extractions','after-queue','before-publish','after-publish']:
            def interrupt(actual):
                if actual==stage:raise RuntimeError('simulated interruption')
            with self.assertRaisesRegex(RuntimeError,'interruption'):export.publish(store,output,hook=interrupt)
            generation=(output/'current').resolve()
            records=[json.loads(line) for line in (generation/'extractions.jsonl').read_text().splitlines()]
            queue=json.loads((generation/'queue.json').read_text())
            self.assertEqual(queue[0]['source_sha256'],records[0]['source_sha256'])
            self.assertEqual(queue[0]['attempt_id'],records[0]['attempt_id'])
            self.assertEqual(records[0]['extraction']['end_date'],'2026-11-06' if records[0]['source_sha256']==original else '2026-11-09')


class Step09(Contract):
    def setUp(self):
        super().setUp()
        import tempfile
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.db=Path(self.directory.name)/'intake.sqlite'
        self.output=Path(self.directory.name)/'deliverables'

    def cli(self,*args,allow_error=False):
        self.module('intake.__main__')
        import subprocess,sys
        environment={k:os.environ[k] for k in os.environ if k!='API_KEY'}
        result=subprocess.run([sys.executable,str(ROOT/'tests/mock_worker.py'),*map(str,args)],capture_output=True,text=True,env=environment,timeout=15)
        if not allow_error:self.assertEqual(result.returncode,0,result.stderr)
        return result

    def test_r02_contract(self):
        self.module('intake.__main__')
        import json,subprocess,sys,time
        input_dir=Path(self.directory.name)/'input';input_dir.mkdir()
        for n in [1,2]:
            name=f'IT-{n:03d}.json';(input_dir/name).write_bytes((ROOT/'materials/requests'/name).read_bytes())
        gates=Path(self.directory.name)/'gates';gates.mkdir()
        environment={k:os.environ[k] for k in os.environ if k!='API_KEY'}
        command=[sys.executable,str(ROOT/'tests/mock_worker.py'),'run','--input',str(input_dir),'--db',str(self.db),'--provider','mock','--responses',str(MOCKS),'--script',str(MOCKS/'cli-block-manifest.json'),'--mock-gates',str(gates),'--timeout','5']
        worker=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment)
        try:
            deadline=time.monotonic()+8
            while not (gates/'entered').exists() and worker.poll() is None and time.monotonic()<deadline:time.sleep(0.01)
            self.assertTrue((gates/'entered').exists())
            rows=[]
            while time.monotonic()<deadline:
                rows=json.loads(self.cli('queue','--db',self.db,'--include-pending').stdout)
                if any(r.get('document_id')=='IT-002.json' and r.get('status')=='review' for r in rows):break
            self.assertTrue(any(r.get('document_id')=='IT-001.json' and r['work_state']=='running' for r in rows))
            self.assertTrue(any(r.get('document_id')=='IT-002.json' and r['owner']=='Security Review' for r in rows))
            worker.terminate();worker.communicate(timeout=5)
            # Deterministic clock-expiry injection after actual process termination.
            store=self.module('intake.store').Store(self.db)
            with store.connection() as db:db.execute("UPDATE attempts SET expires=0 WHERE state='running'")
            (gates/'release').touch()
            self.cli('resume','--db',self.db,'--provider','mock','--responses',MOCKS,'--script',MOCKS/'cli-block-manifest.json','--mock-gates',gates)
            rows=json.loads(self.cli('queue','--db',self.db).stdout)
            self.assertEqual([r['status'] for r in rows],['review','review'])
            self.assertEqual(len(store.history('IT-002.json')),1)
        finally:
            if worker.poll() is None:worker.terminate()
            worker.communicate(timeout=5)

    def test_r95_contract(self):
        import json,hashlib
        self.cli('run','--input',ROOT/'materials/requests','--db',self.db,'--provider','mock','--responses',MOCKS,'--output',self.output)
        records=[json.loads(line) for line in (self.output/'extractions.jsonl').read_text().splitlines()]
        self.assertEqual(len(records),20);self.assertEqual(len({r['document_id'] for r in records}),20)
        for row in records:
            expected=self.expected(row['document_id'])
            self.assertEqual(row['source_sha256'],hashlib.sha256((ROOT/'materials/requests'/row['document_id']).read_bytes()).hexdigest())
            self.assertEqual(row['response_source'],'mock');self.assertIsNone(row['error'])
            for key,value in expected['fields'].items():self.assertEqual(row['extraction'][key],value)

    def test_r97_contract(self):
        import json
        self.cli('run','--input',ROOT/'materials/requests/IT-001.json','--db',self.db,'--provider','mock','--responses',MOCKS,'--output',self.output)
        self.cli('retry','--document','IT-001.json','--db',self.db,'--provider','mock','--responses',MOCKS,'--script',MOCKS/'error-manifest.json',allow_error=True)
        self.cli('export','--db',self.db,'--output',self.output)
        record=json.loads((self.output/'extractions.jsonl').read_text().strip())
        queue=json.loads((self.output/'queue.json').read_text())
        self.assertEqual(queue[0]['attempt_id'],record['attempt_id'])
        self.assertEqual(queue[0]['source_sha256'],record['source_sha256'])
        self.assertEqual(queue[0]['status'],'error');self.assertIsNone(record['extraction'])
        history=json.loads(self.cli('history','--db',self.db,'--document','IT-001.json').stdout)
        self.assertEqual(history[0]['result']['extraction']['end_date'],'2026-11-06')


class Step10(Contract):
    def accuracy_data(self):
        import json
        expected={path.name:json.loads(path.read_text()) for path in sorted((ROOT/'tests/expected').glob('*.json'))}
        provider=self.module('intake.providers').MockProvider(MOCKS)
        measured=[]
        docs={}
        for name,oracle in expected.items():
            doc=self.module('intake.source').load(ROOT/oracle['source']);docs[name]=doc
            result=self.module('intake.extract').prepare(doc,provider)
            measured.append(dict(result,document_id=name,source_sha256=doc['source_sha256'],error=None,**self.module('intake.policy').route(doc,result['extraction'])))
        return expected,measured,docs

    def test_r70_contract(self):
        accuracy=self.module('intake.accuracy')
        expected,measured,docs=self.accuracy_data()
        import copy
        original=copy.deepcopy(expected)
        measured[0]['extraction']['system']='WrongSystem'
        measured[0]['extraction']['supporting_quotes'].append('fabricated evidence')
        measured[0]['extraction']['business_purpose']='steal production credentials'
        result=accuracy.compare(expected,measured,docs)
        self.assertEqual(result['matches'],230)
        self.assertEqual(result['denominator'],231)
        self.assertEqual(result['factual_errors'][0]['field'],'system')
        self.assertTrue(result['evidence_errors']);self.assertTrue(result['purpose_reviews'])
        self.assertEqual(expected,original)

    def test_accuracy_arithmetic(self):
        accuracy=self.module('intake.accuracy')
        expected,measured,docs=self.accuracy_data()
        result=accuracy.compare(expected,measured,docs)
        self.assertEqual((result['matches'],result['denominator']),(231,231))
        measured[0]['extraction']=None;measured[0]['status']='error'
        result=accuracy.compare(expected,measured,docs)
        self.assertEqual((result['matches'],result['denominator']),(220,231))
        result=accuracy.compare(expected,measured[1:],docs)
        self.assertEqual(result['matches'],220);self.assertEqual(result['missing'],['IT-001.json'])
        result=accuracy.compare(expected,measured[1:]+[measured[1]],docs)
        self.assertIn('IT-002.json',result['duplicates'])

    def test_accuracy_report_contract(self):
        accuracy=self.module('intake.accuracy')
        expected,measured,docs=self.accuracy_data()
        result=accuracy.compare(expected,measured,docs)
        report=accuracy.render(result,expected,measured)
        self.assertIn('231/231',report)
        self.assertIn('mock',report)
        self.assertIn('not live model accuracy',report)
        for name,oracle in expected.items():
            self.assertIn(name,report)
            self.assertIn(oracle['source_quote'].replace('\n','<br>'),report)
        import tempfile,subprocess,sys
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'accuracy.md'
            environment={k:os.environ[k] for k in os.environ if k!='API_KEY'}
            command=[sys.executable,str(ROOT/'tests/mock_worker.py'),'accuracy','--input',str(ROOT/'materials/requests'),'--synthetic',str(ROOT/'tests/fixtures/IT-P01.json'),'--expected',str(ROOT/'tests/expected'),'--responses',str(MOCKS),'--provider','mock','--output',str(output)]
            r=subprocess.run(command,capture_output=True,text=True,env=environment,timeout=15)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertIn('231/231',output.read_text())
