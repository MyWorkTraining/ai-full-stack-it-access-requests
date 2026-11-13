"""Source-oracle comparison, deliberately separate from mock provider replies."""
from collections import Counter
import json
from pathlib import Path
from .extract import prepare
from .policy import route
from .source import load
from .validation import SCHEMA, validate

FIELDS = [key for key in SCHEMA['required'] if key not in ('business_purpose', 'supporting_quotes', 'issues')]


def compare(expected, measured, documents):
    counts = Counter(row['document_id'] for row in measured)
    indexed = {row['document_id']: row for row in measured}
    result = {'matches': 0, 'denominator': len(expected) * len(FIELDS), 'factual_errors': [],
              'missing': sorted(set(expected) - set(counts)),
              'duplicates': sorted(name for name, count in counts.items() if count != 1),
              'unexpected': sorted(set(counts) - set(expected)), 'evidence_errors': [], 'purpose_reviews': [],
              'routing_errors': [], 'by_field': {key: {'matches': 0, 'denominator': len(expected)} for key in FIELDS}, 'by_case': []}
    for name, oracle in sorted(expected.items()):
        row = indexed.get(name)
        actual = row.get('extraction') if row and counts[name] == 1 else None
        actual = actual if isinstance(actual, dict) and not row.get('error') else None
        for field in ['owner', 'status', 'next_action']:
            if row is None or row.get(field) != oracle[field]:
                result['routing_errors'].append({'document_id': name, 'field': field, 'expected': oracle[field], 'actual': row.get(field) if row else None})
        matched = 0
        for field in FIELDS:
            wanted = oracle['fields'][field]
            if actual is not None and field in actual and actual[field] == wanted:
                matched += 1
                result['by_field'][field]['matches'] += 1
            else:
                result['factual_errors'].append({'document_id': name, 'field': field, 'expected': wanted,
                                                  'actual': actual.get(field) if actual else None,
                                                  'reason': 'different or unavailable extraction'})
        result['matches'] += matched
        result['by_case'].append({'document_id': name, 'matches': matched, 'denominator': len(FIELDS)})
        if actual is not None:
            try:
                validate(documents[name], actual)
            except ValueError:
                result['evidence_errors'].append({'document_id': name, 'error': 'schema, issue or exact-quote/date-grounding check failed'})
            purpose = actual.get('business_purpose')
            if purpose and purpose not in documents[name]['message']:
                result['purpose_reviews'].append({'document_id': name, 'actual': purpose, 'source_evidence': oracle['purpose_evidence'],
                                                  'review': 'not a literal source span; review semantic support and unsupported additions manually; this is not an automatic factual-error finding'})
        else:
            result['evidence_errors'].append({'document_id': name, 'error': 'no unique valid extraction to check'})
    return result


def render(result, expected, measured):
    configurations = []
    for row in measured:
        config = {'response_source': row['response_source'], 'settings': row.get('settings', {})}
        if config not in configurations:
            configurations.append(config)
    text = '# Accuracy check\n\n'
    text += f"Exact scalar comparisons: **{result['matches']}/{result['denominator']}**.\n\n"
    text += ('This report uses source-derived expectations authored before extraction. Mock observations measure pipeline handling, not live model accuracy. '
             'No live model call or live accuracy observation is claimed.\n\n'
             'Compare the 11 non-purpose scalar fields exactly, case-sensitively, including nulls; no normalization is applied. '
             'The denominator is expected cases × 11, normally 21 × 11 = 231. Missing/error or duplicate-case extractions count as 11 mismatches. '
             'Unexpected cases are reported separately and do not enlarge the denominator. Purpose, issues and exact evidence are separate checks, not hidden scalar successes. '
             'A faithful paraphrase of purpose does not fail solely because it differs from a canned sentence. Nonliteral purpose text requires manual source review; '
             'this checker cannot prove semantic support or attack resistance. No score threshold is specified.\n\n')
    text += 'Producing configuration and origin:\n\n```json\n' + json.dumps(configurations, indent=2) + '\n```\n\n'
    text += '| Document | Exact scalar matches |\n|---|---|\n'
    for case in result['by_case']:
        text += f"| {case['document_id']} | {case['matches']}/{case['denominator']} |\n"
    text += '\n| Field | Matches |\n|---|---|\n'
    for field, values in result['by_field'].items():
        text += f"| {field} | {values['matches']}/{values['denominator']} |\n"
    text += '\nExpected facts and exact source evidence (supplied messages plus the authored synthetic input):\n\n'
    for name, oracle in sorted(expected.items()):
        text += f"- **{name}**, `{oracle['source']}` /message: “" + oracle['source_quote'].replace('\n', '<br>') + '”\n'
        text += '  Expected fields: `' + json.dumps(oracle['fields'], ensure_ascii=False) + '`; '
        text += f"owner={oracle['owner']}, status={oracle['status']}, next_action={oracle['next_action']}.\n"
    policy = Path(__file__).resolve().parents[1] / 'materials/office-policy.md'
    text += '\nRouting expectations derive from `materials/office-policy.md` § Review queue:\n\n'
    for line in policy.read_text().splitlines():
        if line[:2] in ('1.', '2.', '3.', '4.', '5.'):
            text += '> ' + line + '\n\n'
    text += 'Comparison findings and factual-error reporting:\n\n```json\n' + json.dumps(result, indent=2) + '\n```\n'
    return text


def run_accuracy(input_path, synthetic, expected_path, provider, output):
    inputs = Path(input_path)
    paths = [inputs] if inputs.is_file() else sorted(inputs.glob('*.json'))
    if synthetic:
        paths.append(Path(synthetic))
    expected = {path.name: json.loads(path.read_text()) for path in sorted(Path(expected_path).glob('*.json'))}
    documents = {path.name: load(path) for path in paths}
    measured = []
    for path in paths:
        doc = documents[path.name]
        try:
            result = prepare(doc, provider)
            result.update(route(doc, result['extraction']), error=None)
        except ValueError:
            result = {'extraction': None, 'error': 'extraction failed during accuracy check', 'response_source': provider.response_source}
        measured.append(dict(result, document_id=doc['document_id'], source_sha256=doc['source_sha256']))
    comparison = compare(expected, measured, documents)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(comparison, expected, measured))
    output.with_suffix('.json').write_text(json.dumps(comparison, indent=2) + '\n')
    return comparison
