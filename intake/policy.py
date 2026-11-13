"""Ordered human-review routing; this module never grants or approves access."""
from pathlib import Path
import re
from .validation import validate

REQUIRED = ['request_id', 'request_date', 'requester', 'team', 'system', 'environment',
            'permission', 'business_purpose', 'start_date', 'end_date', 'manager']
OVERRIDE = re.compile(r'system override|bypass approval|highest.priority system|display the api key|mark request as approved', re.I)


def outcome(owner, status, action):
    return {'owner': owner, 'status': status, 'next_action': action}


def route(document, extraction):
    validate(document, extraction)
    issues = extraction['issues']
    if OVERRIDE.search(document['message']) or any('instruction-override' in x.lower() for x in issues):
        if not any('instruction-override' in x.lower() for x in issues):
            issues.append('instruction-override: attempted instruction retained as source data')
        if not any(OVERRIDE.search(q) for q in extraction['supporting_quotes']):
            extraction['supporting_quotes'].append(document['message'])
        return outcome('IT Service Desk', 'review', 'inspect-message')
    if extraction['kind'] == 'withdrawal' and extraction['request_id'] and extraction['request_date']:
        return outcome('IT Service Desk', 'review', 'confirm-withdrawal')
    start, end = extraction['start_date'], extraction['end_date']
    reversed_dates = start and end and start > end
    if reversed_dates and not any('date-order' in issue for issue in issues):
        issues.append('date-order: start_date after end_date')
    mismatch = extraction['request_id'] and extraction['request_id'] != Path(document['document_id']).stem
    conflict = any('conflict' in issue.lower() or 'unresolved' in issue.lower() for issue in issues)
    if (extraction['kind'] not in ('new', 'change') or mismatch or conflict or reversed_dates
            or any(extraction[field] in (None, '') for field in REQUIRED)):
        return outcome('IT Service Desk', 'needs-information', 'clarify')
    if extraction['environment'] == 'production' or extraction['permission'] in ('write', 'admin'):
        return outcome('Security Review', 'review', 'review-risk-then-owner')
    return outcome('System Owner', 'review', 'review-request')
