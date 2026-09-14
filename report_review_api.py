"""Authenticated, read-only access to completed report drafts. No D1 credentials.

Only files named by a verified completed draft manifest can be downloaded.
The service never generates, approves, publishes or modifies a report.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
import hashlib
import hmac
import json
import os
import re
import threading

BASE = '/__sigpik_report_review/v1'
MONTH = re.compile(r'\d{4}-(?:0[1-9]|1[0-2])')
SECTOR = re.compile(r'[a-z]+(?:-[a-z]+)*')
SHA = re.compile(r'[a-f0-9]{64}')
MAX_FILE = 16 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def read(file):
    if file.stat().st_size > MAX_FILE:
        raise ValueError('File exceeds limit')
    return json.loads(file.read_text('utf-8'))


def within(root, relative):
    if not isinstance(relative, str) or '\\' in relative or any(p in ('', '.', '..') for p in relative.split('/')):
        raise ValueError('Invalid path')
    file = root / relative
    if not file.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [file, *file.parents] if p != root.parent):
        raise ValueError('Invalid path')
    return file


class DraftStore:
    def __init__(self, work, exports):
        self.work, self.exports = Path(work).resolve(), Path(exports).resolve()

    def states(self):
        result = []
        for file in sorted((self.work / 'reports/automation').glob('*/*/state.json'))[-300:]:
            month, sector = file.parent.parent.name, file.parent.name
            if not MONTH.fullmatch(month) or not SECTOR.fullmatch(sector):
                continue
            row = read(file)
            if row.get('month') != month or row.get('sector') != sector:
                raise ValueError('Invalid state identity')
            result.append(row)
        return result

    def manifest(self, row):
        folder = Path(row['folder']).resolve()
        # Worker paths are absolute inside the shared /app/work volume.
        if not folder.is_relative_to(self.work / 'reports/drafts') or folder.is_symlink():
            raise ValueError('Invalid draft folder')
        manifest = read(folder / 'draft.json')
        body = {k: v for k, v in manifest.items() if k != 'draftDigest'}
        if manifest.get('schemaVersion') != 1 or manifest.get('status') != 'review' or manifest.get('draftDigest') != digest(body):
            raise ValueError('Invalid draft manifest')
        if manifest.get('reportIds') != [f"{row['sector']}-{row['month']}"]:
            raise ValueError('Draft identity mismatch')
        if not isinstance(manifest.get('files'), dict) or len(manifest['files']) > 500:
            raise ValueError('Invalid draft files')
        for name, sha in manifest['files'].items():
            within(folder, name)
            if not SHA.fullmatch(sha):
                raise ValueError('Invalid file hash')
        return folder, manifest

    def catalog(self):
        rows = []
        for state in self.states():
            row = {k: state[k] for k in ('month', 'sector', 'status', 'finishedAt', 'attempts') if k in state}
            if state['status'] in ('review', 'unchanged'):
                try:
                    _, manifest = self.manifest(state)
                    row['draftDigest'] = manifest['draftDigest']
                except (ValueError, OSError, KeyError):
                    row.update(status='blocked', reason='Completed draft could not be verified')
            elif state.get('reason'):
                row['reason'] = str(state['reason'])[:500]
            rows.append(row)
        inventory = []
        for file in sorted(self.exports.glob('*/market-inventory.json'))[-2:]:
            if MONTH.fullmatch(file.parent.name):
                inventory.append(read(file))
        readiness = []
        for file in sorted(self.exports.glob('*/export-status.json'))[-2:]:
            data = read(file)
            if MONTH.fullmatch(str(data.get('month', ''))):
                readiness.append({'month': data['month'], 'checkedAt': data.get('checkedAt'), 'results': data.get('results', [])})
        return {'schemaVersion': 1, 'reports': rows, 'marketInventory': inventory, 'readiness': readiness, 'deployed': False}

    def find(self, key):
        if not SHA.fullmatch(key):
            raise FileNotFoundError()
        invalid = False
        for row in self.states():
            if row['status'] in ('review', 'unchanged'):
                try:
                    folder, manifest = self.manifest(row)
                except (ValueError, OSError, KeyError, TypeError):
                    invalid = True
                    continue
                if manifest['draftDigest'] == key:
                    return folder, manifest
        if invalid:
            raise ValueError('A completed draft could not be verified')
        raise FileNotFoundError()

    def file(self, key, relative):
        folder, manifest = self.find(key)
        if relative not in manifest['files']:
            raise FileNotFoundError()
        file = within(folder, relative)
        if file.stat().st_size > MAX_FILE:
            raise ValueError('File exceeds limit')
        body = file.read_bytes()
        if hashlib.sha256(body).hexdigest() != manifest['files'][relative]:
            raise ValueError('Draft file changed')
        return body


def handler(store, token):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *args):
            pass  # No credentials, draft payloads or query strings in logs.

        def send(self, status, body, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'private, no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Robots-Tag', 'noindex, nofollow, noarchive')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                return self.send(401, {'error': 'authentication_required'})
            url = urlsplit(self.path)
            if len(self.path) > 2000 or url.query or url.fragment:
                return self.send(400, {'error': 'invalid_request'})
            path = unquote(url.path)
            try:
                if path == BASE + '/health':
                    return self.send(200, {'status': 'ready', 'readOnly': True})
                if path == BASE + '/catalog':
                    return self.send(200, store.catalog())
                prefix = BASE + '/drafts/'
                if path.startswith(prefix):
                    key, separator, tail = path[len(prefix):].partition('/')
                    if separator and tail == 'manifest':
                        return self.send(200, store.find(key)[1])
                    if separator and tail.startswith('files/'):
                        return self.send(200, store.file(key, tail[6:]), 'application/octet-stream')
                return self.send(404, {'error': 'not_found'})
            except FileNotFoundError:
                return self.send(404, {'error': 'not_found'})
            except (ValueError, OSError, KeyError, TypeError):
                return self.send(409, {'error': 'draft_unavailable_or_changed'})

        def do_POST(self):
            self.send(405, {'error': 'read_only'})
        do_PUT = do_DELETE = do_PATCH = do_POST
    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args)
    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise
    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


if __name__ == '__main__':
    token = os.environ.get('REPORT_REVIEW_TOKEN', '')
    if len(token) < 40 or not token.isascii():
        raise SystemExit('REPORT_REVIEW_TOKEN must be a randomly generated secret of at least 40 characters')
    store = DraftStore(os.environ.get('REPORT_WORK_ROOT', '/app/work'), os.environ.get('REPORT_EXPORT_ROOT', '/exports'))
    print('Report review service ready (authenticated, read-only)', flush=True)
    Server(('0.0.0.0', int(os.environ.get('REPORT_REVIEW_PORT', '8091'))), handler(store, token)).serve_forever()
