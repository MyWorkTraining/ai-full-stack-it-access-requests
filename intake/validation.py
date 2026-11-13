"""Structural, completion and literal-evidence validation, before business routing."""
from datetime import date
import json
from pathlib import Path
import re
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / 'snapshot.schema.json').read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


def validate(document, extraction):
    errors = sorted(VALIDATOR.iter_errors(extraction), key=lambda e: str(e.path))
    if errors:
        raise ValueError('invalid extraction structure: ' + errors[0].message)
    for field in ['request_date', 'start_date', 'end_date']:
        value = extraction[field]
        if value is not None:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError(f'{field}: expected YYYY-MM-DD')
            date.fromisoformat(value)
            if value not in document['message']:
                raise ValueError(f'{field}: date has no explicit source evidence')
    for field in SCHEMA['required'][:-2]:
        if extraction[field] is None and not any(field in issue for issue in extraction['issues']):
            raise ValueError(f'{field}: null requires an explanatory issue naming the field')
    for quote in extraction['supporting_quotes']:
        if not quote or quote not in document['message']:
            raise ValueError('supporting quote is not an exact message substring')
    if not extraction['supporting_quotes']:
        raise ValueError('supporting quotes required for extracted facts')
    stated = extraction['request_id']
    if stated and stated != Path(document['document_id']).stem:
        if not any('mismatch' in issue.lower() for issue in extraction['issues']):
            raise ValueError('request_id: filename mismatch requires an issue')
    return extraction


def extract_response(document, response):
    if not isinstance(response, dict) or response.get('status') != 'completed':
        raise ValueError('provider response incomplete or invalid completion status')
    texts = []
    output = response.get('output')
    if not isinstance(output, list):
        raise ValueError('provider output must be an array')
    for item in output:
        if not isinstance(item, dict):
            raise ValueError('provider output item must be an object')
        if item.get('type') != 'message':
            continue
        if item.get('status') not in (None, 'completed'):
            raise ValueError('provider message incomplete')
        for content in item.get('content', []):
            if content.get('type') == 'refusal':
                raise ValueError('provider refusal')
            if content.get('type') == 'output_text':
                texts.append(content['text'])
    if len(texts) != 1 or not isinstance(texts[0], str):
        raise ValueError('expected exactly one extraction output text')
    try:
        value = json.loads(texts[0])
    except (ValueError, TypeError) as exc:
        raise ValueError('malformed model JSON') from exc
    return validate(document, value)
