import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from report_review_api import BASE, DraftStore, Server, digest, handler


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.folder = self.work / 'reports/drafts/fixture'
        self.folder.mkdir(parents=True)
        self.relative = 'public/report-assets/previews/music-generation/2025-08/v1/en/cohort.csv'
        self.file = self.folder / self.relative
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b'synthetic,data\n1,2\n')
        self.body = {'schemaVersion': 1, 'status': 'review', 'reportIds': ['music-generation-2025-08'], 'files': {self.relative: hashlib.sha256(self.file.read_bytes()).hexdigest()}}
        self.key = digest(self.body)
        (self.folder / 'draft.json').write_text(json.dumps({**self.body, 'draftDigest': self.key}))
        state = self.work / 'reports/automation/2025-08/music-generation/state.json'
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'month': '2025-08', 'sector': 'music-generation', 'status': 'review', 'folder': str(self.folder)}))
        self.token = 'synthetic-review-test-token-' + 'a' * 40
        self.server = Server(('127.0.0.1', 0), handler(DraftStore(self.work, self.root / 'exports'), self.token))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, token=None, method='GET'):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        client.request(method, BASE + path, headers={} if token is None else {'Authorization': 'Bearer ' + token})
        response = client.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        client.close()
        return result

    def test_authentication_required_and_only_declared_files_are_downloadable(self):
        self.assertEqual(self.request('/catalog')[0], 401)
        self.assertEqual(self.request('/catalog', 'incorrect')[0], 401)
        self.assertEqual(self.request('/health')[0], 401)
        self.assertEqual(self.request('/health', self.token)[0], 200)
        status, data, headers = self.request('/catalog', self.token)
        self.assertEqual(status, 200)
        self.assertNotIn(str(self.folder).encode(), data)
        self.assertEqual(json.loads(data)['reports'][0]['draftDigest'], self.key)
        self.assertEqual(headers['Cache-Control'], 'private, no-store')
        self.assertIn('noindex', headers['X-Robots-Tag'])
        file_url = '/drafts/' + self.key + '/files/'
        self.assertEqual(self.request(file_url + self.relative, self.token)[1], self.file.read_bytes())
        for path in ['.env', '../../secret', '%2e%2e/secret', self.relative + '?redirect=elsewhere']:
            self.assertIn(self.request(file_url + path, self.token)[0], [400, 404, 409])
        self.assertEqual(self.request('/catalog', self.token, 'POST')[0], 405)

    def test_tampering_is_reported_without_serving_modified_content(self):
        self.file.write_bytes(b'changed after generation')
        self.assertEqual(self.request('/drafts/' + self.key + '/files/' + self.relative, self.token)[0], 409)
        (self.folder / 'draft.json').write_text(json.dumps({**self.body, 'status': 'published', 'draftDigest': self.key}))
        status, data, _ = self.request('/catalog', self.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)['reports'][0]['status'], 'blocked')
        self.assertEqual(self.request('/drafts/' + self.key + '/manifest', self.token)[0], 409)

    def test_unrelated_invalid_draft_does_not_block_valid_downloads(self):
        state = self.work / 'reports/automation/2025-08/image-generation/state.json'
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'month': '2025-08', 'sector': 'image-generation', 'status': 'review', 'folder': str(self.work / 'missing')}))
        self.assertEqual(self.request('/drafts/' + self.key + '/manifest', self.token)[0], 200)
        status, data, _ = self.request('/catalog', self.token)
        self.assertEqual(status, 200)
        self.assertEqual([r['status'] for r in json.loads(data)['reports']], ['blocked', 'review'])


if __name__ == '__main__':
    unittest.main()
