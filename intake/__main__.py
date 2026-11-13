"""Documented operator CLI. Mock mode does not touch credential configuration."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from .export import publish
from .extract import ResponsesProvider
from .providers import MockProvider
from .runner import Runner
from .source import load
from .store import Store


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    for command in ['run', 'queue', 'resume', 'retry', 'history', 'export']:
        sub = commands.add_parser(command)
        sub.add_argument('--db', default='var/intake.sqlite')
        if command in ('run', 'resume', 'retry'):
            sub.add_argument('--provider', choices=['mock', 'openai'])
            sub.add_argument('--responses')
            sub.add_argument('--script')
            sub.add_argument('--mock-gates')
            sub.add_argument('--model')
            sub.add_argument('--endpoint')
            sub.add_argument('--timeout', type=float)
            sub.add_argument('--max-output-tokens', type=int)
            sub.add_argument('--workers', type=int, default=4)
            sub.add_argument('--max-retries', type=int, default=0)
            sub.add_argument('--retry-delay', type=float, default=1)
            sub.add_argument('--output')
        if command == 'run':
            sub.add_argument('--input', required=True)
            sub.add_argument('--refresh', action='store_true')
        if command in ('history', 'retry'):
            sub.add_argument('--document', required=True)
        if command == 'queue':
            sub.add_argument('--include-pending', action='store_true')
        if command == 'export':
            sub.add_argument('--output', required=True)
    return result


def make_runner(args, store):
    prior = {}
    if args.command == 'retry':
        history = store.history(args.document)
        if history:
            prior = history[-1]['settings']
    elif args.command == 'resume' and store.pending():
        prior = store.pending()[0]['settings']
    provider_name = args.provider or ('openai' if prior.get('provider', '').startswith('openai/') else 'mock')
    timeout = args.timeout if args.timeout is not None else prior.get('timeout', 30)
    config = {}
    if provider_name == 'mock':
        root = Path(args.responses or prior.get('mock_responses', 'tests/mock_responses')).resolve()
        script = Path(args.script or prior.get('mock_script', root / 'manifest.json')).resolve()
        provider = MockProvider(root, json.loads(script.read_text()))
        config.update(mock_script=str(script), mock_script_sha256=hashlib.sha256(script.read_bytes()).hexdigest())
        gates = args.mock_gates or prior.get('mock_gates')
        if gates:
            config['mock_gates'] = str(Path(gates).resolve())
    else:
        model = args.model or prior.get('model')
        if not model:
            raise ValueError('--model is required for provider=openai')
        provider = ResponsesProvider(model=model, endpoint=args.endpoint or prior.get('endpoint') or 'https://api.openai.com/v1/responses',
                                     timeout=timeout, max_output_tokens=args.max_output_tokens or prior.get('max_output_tokens', 4096))
    return Runner(store, provider, workers=args.workers, timeout=timeout, max_retries=args.max_retries,
                  retry_delay=args.retry_delay, config=config)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        store = Store(args.db)
        if args.command == 'queue':
            print(json.dumps(store.current(args.include_pending), indent=2)); return 0
        if args.command == 'history':
            print(json.dumps(store.history(args.document), indent=2)); return 0
        if args.command == 'export':
            print(json.dumps({'generation': str(publish(store, args.output))})); return 0
        runner = make_runner(args, store)
        if args.command == 'run':
            source = Path(args.input)
            paths = [source] if source.is_file() else sorted(source.glob('*.json'))
            if not paths:
                raise ValueError('input contains no JSON exports')
            runner.run([load(path) for path in paths], force=args.refresh)
        elif args.command == 'resume':
            runner.resume()
        else:
            runner.retry(args.document)
        if args.output:
            publish(store, args.output)
        rows = store.current(include_pending=True)
        errors = sum(row.get('status') == 'error' for row in rows)
        print(json.dumps({'documents': len(rows), 'errors': errors,
                          'pending': sum(row['work_state'] in ('pending', 'running') for row in rows)}))
        return 1 if errors else 0
    except (OSError, ValueError) as exc:
        # Do not print untrusted provider bodies, extracted values or credentials.
        print(f'{type(exc).__name__}: command failed; inspect paths, source format and documented configuration', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
