"""SQLite source revisions, immutable attempt results and transactional current pointers."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def fingerprint(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    document TEXT NOT NULL, settings TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', result TEXT, created REAL NOT NULL,
                    token TEXT, expires REAL, tries INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL, revision INTEGER NOT NULL,
                    current_attempt INTEGER NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, document, settings, force=False):
        name = document['document_id']
        identity = fingerprint(settings)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT a.* FROM documents d JOIN attempts a ON a.id=d.current_attempt WHERE d.document_id=?', (name,)).fetchone()
            if current and current['fingerprint'] == identity and json.loads(current['document'])['source_sha256'] == document['source_sha256'] and not force:
                if current['state'] == 'done':
                    return None
                if current['state'] in ('pending', 'running'):
                    return current['id']
            if current and json.loads(current['document'])['source_sha256'] == document['source_sha256']:
                revision = current['revision']
            else:
                revision = db.execute('INSERT INTO revisions(document_id,source_sha256,document) VALUES(?,?,?)',
                                      (name, document['source_sha256'], json.dumps(document))).lastrowid
            attempt = db.execute('INSERT INTO attempts(document_id,revision,document,settings,fingerprint,created) VALUES(?,?,?,?,?,?)',
                                 (name, revision, json.dumps(document), json.dumps(settings), identity, time.time())).lastrowid
            db.execute('INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET source_sha256=excluded.source_sha256,revision=excluded.revision,current_attempt=excluded.current_attempt',
                       (name, document['source_sha256'], revision, attempt))
            return attempt

    def finish(self, attempt, result, token=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM attempts WHERE id=?', (attempt,)).fetchone()
            if row is None or row['state'] in ('done', 'error'):
                return False
            state = 'error' if result.get('error') else 'done'
            db.execute('UPDATE attempts SET state=?,result=?,token=NULL,expires=NULL WHERE id=?', (state, json.dumps(result), attempt))
            return True

    def get(self, attempt):
        with self.connection() as db:
            row = db.execute('SELECT * FROM attempts WHERE id=?', (attempt,)).fetchone()
            return self.decode(row) if row else None

    @staticmethod
    def decode(row):
        value = dict(row)
        for key in ['document', 'settings', 'result']:
            value[key] = json.loads(value[key]) if value[key] else None
        return value

    def history(self, document_id):
        with self.connection() as db:
            return [self.decode(row) for row in db.execute('SELECT * FROM attempts WHERE document_id=? ORDER BY id', (document_id,))]

    def current(self, include_pending=False):
        with self.connection() as db:
            rows = db.execute('SELECT a.* FROM documents d JOIN attempts a ON a.id=d.current_attempt ORDER BY d.document_id').fetchall()
            records = []
            for row in rows:
                value = self.decode(row)
                if not value['result'] and not include_pending:
                    continue
                doc = value['document']
                records.append(dict(value['result'] or {}, document_id=doc['document_id'], source_sha256=doc['source_sha256'],
                                    attempt_id=value['id'], source_revision=value['revision'], settings=value['settings'],
                                    work_state=value['state']))
            return records

    def pending(self):
        with self.connection() as db:
            return [self.decode(row) for row in db.execute("SELECT a.* FROM documents d JOIN attempts a ON a.id=d.current_attempt WHERE a.state IN ('pending','running') ORDER BY a.id")]


    def claim(self, attempt, now=None, lease=60):
        import uuid
        now = time.time() if now is None else now
        token = uuid.uuid4().hex
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM attempts WHERE id=?', (attempt,)).fetchone()
            if row is None or row['state'] in ('done', 'error') or row['not_before'] > now:
                return None
            if row['state'] == 'running' and row['expires'] > now:
                return None
            db.execute("UPDATE attempts SET state='running',token=?,expires=?,tries=tries+1 WHERE id=?", (token, now + lease, attempt))
            return token

    def defer(self, attempt, token, not_before):
        with self.connection() as db:
            return db.execute("UPDATE attempts SET state='pending',not_before=?,token=NULL,expires=NULL WHERE id=? AND token=? AND state='running'", (not_before, attempt, token)).rowcount == 1
