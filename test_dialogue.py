import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
import app


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_patch = patch.object(app, 'KEYS', app.KeyStore(Path(self.tmp.name) / 'keys.json', 'test-key'))
        self.key_patch.start()
        self.speakers = [{'name':'Ali','voice':'urdu-male'}, {'name':'Sara','voice':'urdu-female'}]
        self.voices = [{'id':'urdu-male'}, {'id':'urdu-female'}]

    def tearDown(self):
        self.key_patch.stop()
        self.tmp.cleanup()

    def post(self, script, speakers=None, language='ur-PK', **extra):
        calls=[]
        def handler(req):
            calls.append(json.loads(req.content))
            return httpx.Response(200,json={'output':[{'type':'audio','data':base64.b64encode(b'\0\0'*24000).decode()}]})
        real=httpx.Client
        tmp = Path(self.tmp.name)
        (tmp / 'segments').mkdir(exist_ok=True); (tmp / 'raw').mkdir(exist_ok=True)
        with TestClient(app.app) as client, patch.object(app, 'OUTPUT_DIR', tmp), patch.object(app, 'SEG_DIR', tmp / 'segments'), patch.object(app, 'RAW_DIR', tmp / 'raw'), patch.object(app,'language_voices',return_value={'voices':self.voices}), patch.object(app.httpx,'Client',side_effect=lambda **kw:real(transport=httpx.MockTransport(handler),**kw)):
            result=client.post('/api/dialogue',json={'script':script,'speakers':speakers or self.speakers,'language':language,'take':str(time.time()),**extra})
            if result.status_code == 200 and 'job' in result.json():
                for _ in range(200):
                    job = client.get('/api/jobs/' + result.json()['job']).json()
                    if job['status'] != 'running':
                        break
                    time.sleep(0.05)
                self.assertEqual(job['status'], 'done', job)
                return job['result'], calls
        return result,calls

    def test_long_dialogue_keeps_each_speaker_and_effect_in_one_request(self):
        text='Yeh sirf Ali ki line hai. '*150
        result,calls=self.post('ALI [excited]: '+text+'<laugh>\n\nSara [calm]: Yeh sirf Sara ki line hai. <sigh>', chunk_chars=0)
        self.assertEqual(len(calls),1)
        self.assertEqual(result['requests'],1)
        cfg=calls[0]['generation_config']['speech_config']
        self.assertEqual(cfg,{'speakers':[{'speaker':'Ali','voice':'urdu-male','language':'ur-PK'},{'speaker':'Sara','voice':'urdu-female','language':'ur-PK'}]})
        turns=calls[0]['input'][0]['content']
        self.assertEqual([t['annotations'][0]['speaker'] for t in turns],['Ali','Sara'])
        self.assertEqual(turns[0]['text'],text+'<laugh>')
        self.assertEqual(turns[1]['annotations'][0]['style'],'calm')

    def test_long_podcast_is_split_but_every_request_keeps_same_speakers_and_language(self):
        lines = '\n'.join(f"{'Ali' if i % 2 == 0 else 'Sara'}: Line {i} " + 'baat cheet ' * 20 for i in range(12))
        result, calls = self.post(lines, chunk_chars=600)
        self.assertGreater(len(calls), 1)
        self.assertEqual(result['requests'], len(calls))
        for call in calls:
            self.assertEqual(call['generation_config']['speech_config'], {'speakers': [
                {'speaker': 'Ali', 'voice': 'urdu-male', 'language': 'ur-PK'},
                {'speaker': 'Sara', 'voice': 'urdu-female', 'language': 'ur-PK'}]})
        spoken = [t['text'] for c in calls for t in c['input'][0]['content']]
        self.assertEqual(len(spoken), 12)

    def test_unlabeled_lines_continue_previous_speaker_and_stage_directions_dropped(self):
        result, calls = self.post('[FADE IN] [MUSIC: suspense]\n[confident] Main chatbot banata hoon. [serious] kaafi nahi.\nSara: Sach? [laughs]', chunk_chars=0)
        turns = calls[0]['input'][0]['content']
        self.assertEqual([t['annotations'][0]['speaker'] for t in turns], ['Ali', 'Ali', 'Sara'])
        self.assertEqual([t['annotations'][0].get('style') for t in turns], ['confident', 'serious', None])
        self.assertEqual(turns[2]['text'], 'Sach? <laugh>')
        self.assertNotIn('MUSIC', str(calls))

    def test_other_names_are_mapped_to_free_speaker(self):
        result, calls = self.post('Ali: Hello\nAhmed: Hello', chunk_chars=0)
        turns = calls[0]['input'][0]['content']
        self.assertEqual([t['annotations'][0]['speaker'] for t in turns], ['Ali', 'Sara'])

    def test_three_people_rejected(self):
        result, calls = self.post('Ali: a\nSara: b\nAhmed: c')
        self.assertEqual(result.status_code, 400)
        self.assertEqual(calls, [])

    def test_duplicate_names_or_voices_rejected(self):
        for speakers in ([{'name':'Ali','voice':'a'},{'name':' ali ','voice':'b'}], [{'name':'Ali','voice':'a'},{'name':'Sara','voice':'a'}]):
            with self.subTest(speakers=speakers):
                result,calls=self.post('Ali: Hello',speakers)
                self.assertEqual(result.status_code,400)
                self.assertEqual(calls,[])

    def test_urdu_rejects_unverified_english_voice(self):
        speakers=[{'name':'Ali','voice':'english-voice'},{'name':'Sara','voice':'urdu-female'}]
        result,calls=self.post('Ali: Assalam o alaikum',speakers)
        self.assertEqual(result.status_code,400)
        self.assertIn('fallback',result.json()['detail'])
        self.assertEqual(calls,[])

    def test_manual_standard_voices_allowed_only_when_catalog_empty(self):
        self.voices = []
        speakers = [{'name':'Ali','voice':'Puck'}, {'name':'Sara','voice':'Kore'}]
        result, calls = self.post('Ali: Salam\nSara: Walaikum assalam', speakers)
        config = calls[0]['generation_config']['speech_config']['speakers']
        self.assertEqual([v['voice'] for v in config], ['Puck', 'Kore'])
        self.assertEqual([v['language'] for v in config], ['ur-PK', 'ur-PK'])
        self.voices = [{'id':'urdu-male'}, {'id':'urdu-female'}]
        result, calls = self.post('Ali: Salam', speakers)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(calls, [])

    def test_catalog_checks_language_and_reuses_metadata_cache(self):
        app._LANGUAGE_VOICES.clear()
        seen=[]
        def handler(req):
            seen.append(req)
            return httpx.Response(200,json={'voices':[{'id':'urdu-male','language_code':'ur-PK'}, {'id':'english','language_code':'en-US'}]})
        real=httpx.Client
        with patch.object(app.httpx,'Client',side_effect=lambda **kw:real(transport=httpx.MockTransport(handler),**kw)):
            self.assertEqual([v['id'] for v in app.language_voices('ur-PK')['voices']],['urdu-male'])
            app.language_voices('ur-PK')
        self.assertEqual(len(seen),1)
        self.assertIn('ur-PK',seen[0].url.params.get_list('language_code'))
        app._LANGUAGE_VOICES.clear()

if __name__=='__main__':
    unittest.main()
