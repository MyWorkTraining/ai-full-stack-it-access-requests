"""Publish matching queue and extraction files through one atomic generation pointer."""
import json
import os
from pathlib import Path
import uuid

QUEUE_FIELDS = ['document_id', 'source_sha256', 'attempt_id', 'source_revision', 'status', 'owner', 'next_action', 'error']


def write_synced(path, text):
    with path.open('w', encoding='utf-8') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(store, output, hook=None):
    hook = hook or (lambda stage: None)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    generations = output / '.generations'
    generations.mkdir(exist_ok=True)
    name = uuid.uuid4().hex
    generation = generations / name
    generation.mkdir()
    # One SELECT snapshot provides the input for both files.
    records = store.current()
    write_synced(generation / 'extractions.jsonl', ''.join(json.dumps(row, sort_keys=True) + '\n' for row in records))
    hook('after-extractions')
    queue = [{key: row.get(key) for key in QUEUE_FIELDS} for row in records]
    write_synced(generation / 'queue.json', json.dumps(queue, indent=2, sort_keys=True) + '\n')
    hook('after-queue')
    sync_directory(generation)
    sync_directory(generations)
    # These stable links always traverse the same directory pointer. Readers pin
    # `current.resolve()` before reading multiple files across concurrent publication.
    for filename in ['extractions.jsonl', 'queue.json']:
        public = output / filename
        if not public.is_symlink():
            temporary = output / f'.{filename}-{name}'
            temporary.symlink_to(f'current/{filename}')
            os.replace(temporary, public)
    pointer = output / f'.current-{name}'
    pointer.symlink_to(f'.generations/{name}', target_is_directory=True)
    hook('before-publish')
    os.replace(pointer, output / 'current')
    sync_directory(output)
    hook('after-publish')
    return generation
