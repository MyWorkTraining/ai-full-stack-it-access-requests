"""Lossless source revision identity; export metadata is never a business fact."""
import hashlib
import json
from pathlib import Path


def load(path):
    path = Path(path)
    raw = path.read_bytes()
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('message'), str):
        raise ValueError('export must contain a message string')
    return {'document_id': path.name, 'source_sha256': hashlib.sha256(raw).hexdigest(),
            'message': data['message'], 'metadata': {k: v for k, v in data.items() if k != 'message'},
            'filename': str(path.resolve()), 'raw': raw.decode('utf-8')}
