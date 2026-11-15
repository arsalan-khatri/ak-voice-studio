import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
import app
from key_manager import KeyStore


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = KeyStore(Path(self.tmp.name) / 'keys.json')
        self.patch = patch.object(app, 'KEYS', self.store)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def keys(self):
        for i in range(3):
            self.store.add(f'secret-{i}', f'Key {i}')
        return list(self.store.items)

    def run_request(self, statuses):
        calls = []
        def handler(req):
            calls.append((req.headers['x-goog-api-key'], json.loads(req.content)))
            code, message = statuses[len(calls)-1]
            return httpx.Response(code, json={'error': {'message': message}} if code >= 400 else {'ok': True})
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            result = app.gemini_request(c, 'POST', 'https://test.invalid', json={'script': 'entire script'})
        return calls, result

    def test_sticky_success_and_secrets_masked(self):
        self.keys()
        calls, _ = self.run_request([(200, '')])
        self.assertEqual([c[0] for c in calls], ['secret-0'])
        self.assertNotIn('secret-', json.dumps(self.store.public()))

    def test_expired_last_key_wraps_to_first(self):
        keys = self.keys()
        self.store.select(keys[-1]['id'])
        calls, response = self.run_request([(400, 'API key expired'), (200, '')])
        self.assertEqual([c[0] for c in calls], ['secret-2', 'secret-0'])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(self.store.active, keys[0]['id'])
        self.assertEqual(response.extensions['key_attempts'], 2)
        self.assertIn('Auto switch', self.store.events[-1]['message'])

    def test_all_quota_failures_are_bounded(self):
        self.keys()
        calls = []
        def handler(req):
            calls.append(req.headers['x-goog-api-key'])
            return httpx.Response(429, json={'error': {'message': 'quota'}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            with self.assertRaises(HTTPException) as error:
                app.gemini_request(c, 'POST', 'https://test.invalid', json={})
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(calls, ['secret-0', 'secret-1', 'secret-2'])

    def test_bad_script_does_not_rotate(self):
        keys = self.keys()
        with self.assertRaises(HTTPException):
            self.run_request([(400, 'script too long')])
        self.assertEqual(self.store.active, keys[0]['id'])
        self.assertFalse(any('Auto switch' in e['message'] for e in self.store.events))

    def test_persistence_manual_selection_deletion_and_duplicate(self):
        keys = self.keys()
        self.store.select(keys[1]['id'])
        with self.assertRaises(HTTPException):
            self.store.add('secret-1')
        reloaded = KeyStore(self.store.path)
        self.assertEqual(reloaded.ordered()[0]['key'], 'secret-1')
        self.store.remove(keys[1]['id'])
        self.assertEqual(self.store.active, keys[0]['id'])

    def test_manual_change_is_not_overridden_by_inflight_failover(self):
        keys = self.keys()
        self.store.select(keys[2]['id'])
        self.store.advance(keys[0], keys[1], 'expired')
        self.assertEqual(self.store.active, keys[2]['id'])

    def test_key_endpoints_first_key_works_without_restart(self):
        with TestClient(app.app) as client:
            self.assertFalse(client.get('/api/config').json()['has_key'])
            response = client.post('/api/keys', json={'key': 'new-secret-key', 'label': 'Primary'})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('new-secret-key', response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertTrue(client.get('/api/config').json()['has_key'])
            self.assertEqual(app.api_key(), 'new-secret-key')
            denied = client.post('/api/keys', json={'key': 'bad'}, headers={'Origin': 'https://external.example'})
            self.assertEqual(denied.status_code, 403)

    def test_model_catalog_pagination_and_filtering(self):
        self.keys()
        seen=[]
        def handler(req):
            seen.append(str(req.url))
            if len(seen)==1:
                return httpx.Response(200,json={'models':[{'name':'models/gemini-3.8-flash-tts'}, {'name':'models/gemini-text'}], 'nextPageToken':'page2'})
            return httpx.Response(200,json={'models':[{'name':'models/gemini-2.5-pro-preview-tts'}]})
        real = httpx.Client
        with patch.object(app.httpx, 'Client', side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            models=app.tts_models()['models']
        self.assertEqual(len(models),2)
        self.assertIn('pageToken=page2',seen[1])
        self.assertTrue(next(m for m in models if '2.5' in m['id'])['legacy'])

    def test_full_script_endpoint_survives_expired_key(self):
        self.keys()
        sent = []
        def handler(req):
            sent.append(json.loads(req.content))
            if len(sent) == 1:
                return httpx.Response(400, json={"error": {"message": "API key expired"}})
            return httpx.Response(200, json={"output": [{"type": "audio", "data": base64.b64encode(b"\0\0" * 24000).decode()}]})
        real = httpx.Client
        with TestClient(app.app) as client, patch.object(app, 'OUTPUT_DIR', Path(self.tmp.name)), patch.object(app.httpx, 'Client', side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            response = client.post('/api/tts', data={'script':'[happy] Hello.\n\n[calm] Goodbye.', 'voice':'Kore'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['requests'], 2)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(response.json()['duration'], 1)
        self.assertEqual(Path(self.tmp.name, response.json()['file']).read_bytes()[:4], b'RIFF')

    def test_legacy_tts_payload_and_audio(self):
        self.keys()
        seen=[]
        def handler(req):
            seen.append(req)
            return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'inlineData':{'mimeType':'audio/pcm','data':base64.b64encode(b'1234').decode()}}]}}]})
        body={'model':'gemini-2.5-pro-preview-tts','input':[{'content':[{'text':'Full script','annotations':[{'style':'happy'}]}]}], 'generation_config':{'speech_config':[{'voice':'Kore'}]}}
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            self.assertEqual(app.tts_request(c,body),b'1234')
        self.assertEqual(len(seen),1)
        self.assertTrue(seen[0].url.path.endswith(':generateContent'))
        self.assertIn('[happy] Full script',json.loads(seen[0].content)['contents'][0]['parts'][0]['text'])

if __name__ == '__main__':
    unittest.main()
