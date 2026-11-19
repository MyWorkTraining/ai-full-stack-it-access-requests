"""Linear, labelled terminal output for operators."""


WORK_STATES = {
    'pending': 'Pending',
    'running': 'Running',
    'done': 'Completed',
    'error': 'Failed',
    'superseded': 'Superseded',
}


def printable(value):
    text = str(value)
    return ''.join(char if char.isprintable() else ascii(char)[1:-1] for char in text)


def lines(label, value, indent=''):
    """Render nested data in reading order without columns or terminal controls."""
    label = printable(label)
    if isinstance(value, dict) and value:
        yield f'{indent}{label}:'
        for key, item in value.items():
            yield from lines(key.replace('_', ' ').capitalize(), item, indent + '  ')
    elif isinstance(value, list) and value:
        yield f'{indent}{label}:'
        for index, item in enumerate(value, start=1):
            yield from lines(f'Item {index}', item, indent + '  ')
    else:
        if value is None:
            value = 'Unavailable'
        elif value in ({}, []):
            value = 'None'
        yield f'{indent}{label}: {printable(value)}'


def queue_text(records):
    if not records:
        return 'Queue: No matching requests.'
    blocks = []
    for record in records:
        state = WORK_STATES.get(record['work_state'], record['work_state'])
        block = [*lines('Document', record['document_id']), *lines('Work state', state, '  ')]
        for field in ('owner', 'status', 'next_action', 'error', 'retry_command'):
            if record.get(field):
                value = record[field]
                if field == 'retry_command':
                    value += ' --format text'
                block.extend(lines(field.replace('_', ' ').capitalize(), value, '  '))
        for label, key in (('Attempt', 'attempt_id'), ('Source revision', 'source_revision'),
                           ('Source SHA-256', 'source_sha256'), ('Response source', 'response_source'),
                           ('Extraction', 'extraction'), ('Settings', 'settings')):
            if key in record:
                block.extend(lines(label, record[key], '  '))
        blocks.append('\n'.join(block))
    return '\n\n'.join(blocks)


def history_text(document, attempts):
    blocks = [*lines('Document', document)]
    if not attempts:
        return blocks[0] + '\nHistory: No attempts found.'
    for attempt in attempts:
        state = WORK_STATES.get(attempt['state'], attempt['state'])
        block = [*lines('Attempt', attempt['id']), *lines('Work state', state, '  ')]
        for label, key in (('Source revision', 'revision'), ('Created (Unix time)', 'created'),
                           ('Settings fingerprint', 'fingerprint'), ('Settings', 'settings'),
                           ('Result', 'result'), ('Source document', 'document')):
            block.extend(lines(label, attempt[key], '  '))
        blocks.append('\n'.join(block))
    return '\n\n'.join(blocks)
