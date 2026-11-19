"""Documented operator CLI. Mock mode does not touch credential configuration."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from .cli_text import history_text, printable, queue_text
from .export import publish
from .extract import ResponsesProvider
from .providers import MockProvider
from .runner import Runner
from .source import load
from .store import Store


def add_output_format(command):
    command.add_argument('--format', choices=('json', 'text'), default='json',
                         help='Choose JSON or labelled plain text (default: JSON).')


def parser():
    result = argparse.ArgumentParser(description='Process access requests and inspect the human-review queue.')
    commands = result.add_subparsers(dest='command', required=True)
    descriptions = {
        'run': 'Start intake for one export or a directory of exports.',
        'queue': 'Read the current review queue.',
        'resume': 'Continue recorded unfinished work.',
        'retry': 'Run a new attempt for one document.',
        'history': 'Read previous attempts and their source and settings.',
        'export': 'Publish the current queue and extraction files.',
    }
    for command, description in descriptions.items():
        sub = commands.add_parser(command, help=description, description=description)
        sub.add_argument('--db', default='var/intake.sqlite')
        add_output_format(sub)
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
            sub.add_argument('--document', required=True, help='Input filename, including .json.')
        if command == 'queue':
            sub.add_argument('--include-pending', action='store_true', help='Include recorded unfinished work.')
        if command == 'export':
            sub.add_argument('--output', required=True)
    accuracy = commands.add_parser('accuracy', help='Compare mock or provider results with source expectations.')
    add_output_format(accuracy)
    accuracy.add_argument('--input', required=True)
    accuracy.add_argument('--synthetic')
    accuracy.add_argument('--expected', required=True)
    accuracy.add_argument('--responses', default='tests/mock_responses')
    accuracy.add_argument('--provider', choices=['mock', 'openai'], default='mock')
    accuracy.add_argument('--model')
    accuracy.add_argument('--output', required=True)
    return result


def make_runner(args, store, prior_override=None):
    prior = {}
    if args.command == 'retry':
        history = store.history(args.document)
        if history:
            prior = history[-1]['settings']
    elif args.command == 'resume' and store.pending():
        prior = store.pending()[0]['settings']
    if prior_override is not None:
        prior = prior_override
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
    text_output = args.format == 'text'
    try:
        if args.command == 'accuracy':
            from .accuracy import run_accuracy
            if args.provider == 'openai' and not args.model:
                raise ValueError('--model is required for provider=openai')
            provider = MockProvider(args.responses) if args.provider == 'mock' else ResponsesProvider(args.model)
            if text_output:
                print('Progress: Checking accuracy.', flush=True)
            comparison = run_accuracy(args.input, args.synthetic, args.expected, provider, args.output)
            summary = {'matches': comparison['matches'], 'denominator': comparison['denominator']}
            if text_output:
                print(f"Exact scalar matches: {summary['matches']} of {summary['denominator']}")
                print(f'Accuracy report: {printable(args.output)}')
            else:
                print(json.dumps(summary))
            return 0 if comparison['matches'] == comparison['denominator'] and not comparison['duplicates'] and not comparison['unexpected'] and not comparison['evidence_errors'] and not comparison['routing_errors'] else 1
        store = Store(args.db)
        if args.command == 'queue':
            records = store.current(args.include_pending)
            print(queue_text(records) if text_output else json.dumps(records, indent=2))
            return 0
        if args.command == 'history':
            attempts = store.history(args.document)
            print(history_text(args.document, attempts) if text_output else json.dumps(attempts, indent=2))
            return 0
        if args.command == 'export':
            generation = publish(store, args.output)
            print(f'Published generation: {printable(generation)}' if text_output
                  else json.dumps({'generation': str(generation)}))
            return 0
        runner = make_runner(args, store)
        if args.command == 'run':
            source = Path(args.input)
            paths = [source] if source.is_file() else sorted(source.glob('*.json'))
            if not paths:
                if text_output:
                    print(f'Error: No JSON exports found at {printable(source)}.', file=sys.stderr)
                    print('Next action: Use --input with a readable JSON file or a directory containing .json exports.', file=sys.stderr)
                    return 2
                raise ValueError('input contains no JSON exports')
            documents = []
            for path in paths:
                try:
                    documents.append(load(path))
                except (OSError, ValueError) as exc:
                    if not text_output:
                        raise
                    if isinstance(exc, OSError):
                        print(f'Error: Could not read JSON export: {printable(path.name)}.', file=sys.stderr)
                        print('Next action: Check that the export is readable, then rerun intake.', file=sys.stderr)
                    else:
                        print(f'Error: Invalid JSON message export: {printable(path.name)}.', file=sys.stderr)
                        print('Next action: Provide UTF-8 JSON with a message string, then rerun intake.', file=sys.stderr)
                    return 2
            if text_output:
                print(f'Progress: Starting intake for {len(paths)} input files.', flush=True)
            runner.run(documents, force=args.refresh)
        elif args.command == 'resume':
            if text_output:
                print('Progress: Resuming unfinished requests.', flush=True)
            # Reconstruct producing settings per document rather than borrowing the
            # first pending document's model, endpoint or mock script for all work.
            from concurrent.futures import ThreadPoolExecutor
            def resume_one(pending):
                make_runner(args, store, pending['settings']).resume(attempts=[pending['id']])
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                list(pool.map(resume_one, store.pending()))
        else:
            if text_output:
                print(f'Progress: Retrying {printable(args.document)}.', flush=True)
            runner.retry(args.document)
        if args.output:
            publish(store, args.output)
        rows = store.current(include_pending=True)
        errors = sum(row.get('status') == 'error' for row in rows)
        pending = sum(row['work_state'] in ('pending', 'running') for row in rows)
        if text_output:
            print(f'Current requests: {len(rows)}\nFailed requests: {errors}\nUnfinished requests: {pending}')
            if errors:
                print('Current queue failures:')
                print(queue_text([row for row in rows if row.get('status') == 'error']))
        else:
            print(json.dumps({'documents': len(rows), 'errors': errors, 'pending': pending}))
        return 1 if errors else 0
    except (OSError, ValueError) as exc:
        # Do not print untrusted provider bodies, extracted values or credentials.
        if text_output:
            print(f'Error: The {args.command} command failed ({type(exc).__name__}).', file=sys.stderr)
            print(f'Next action: Check paths and source format, then run intake {args.command} --help.', file=sys.stderr)
        else:
            print(f'{type(exc).__name__}: command failed; inspect paths, source format and documented configuration', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
