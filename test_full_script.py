import base64
import json
import math
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
import app


def tone_with_pauses(n_items):
    """Fake TTS audio: 1 s speech + 1.5 s silence per paragraph."""
    fr, out = 24000, bytearray()
    for _ in range(n_items):
        out += b"".join(struct.pack("<h", int(8000 * math.sin(i * 0.06))) for i in range(fr))
        out += b"\0\0" * int(fr * 1.5)
    return bytes(out)


class FullScriptTests(unittest.TestCase):
    def request(self, script, status=200, **extra):
        calls = []

        def respond(request):
            body = json.loads(request.content)
            if ":generateContent" in str(request.url):  # subtitle script conversion
                arr = json.loads(body["contents"][0]["parts"][0]["text"].split("Input JSON array:\n", 1)[1])
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(["اردو" for _ in arr])}]}}]})
            calls.append(body)
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "limit reached"}})
            n = len(body["input"][0]["content"])
            return httpx.Response(200, json={"output": [{"type": "audio", "data": base64.b64encode(tone_with_pauses(n)).decode()}]})

        real_client = httpx.Client
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); (tmp / "segments").mkdir(); (tmp / "raw").mkdir()
            with TestClient(app.app) as client, \
                    patch.object(app, "KEYS", app.KeyStore(tmp / "keys.json", "test-key")), \
                    patch.object(app, "OUTPUT_DIR", tmp), patch.object(app, "SEG_DIR", tmp / "segments"), \
                    patch.object(app, "RAW_DIR", tmp / "raw"), patch.object(app, "TEXT_MODEL", "gemini-test-flash"), \
                    patch.object(app, "RATE_LIMIT_RETRIES", 0), \
                    patch.object(app.httpx, "Client", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)):
                data = {"script": script, "voice": "Charon", "style": "calm", "take": str(time.time())}
                data.update({k: str(v) for k, v in extra.items()})
                response = client.post("/api/tts", data=data)
                if response.status_code != 200:
                    return response.status_code, response.json(), calls, None
                for _ in range(200):
                    job = client.get("/api/jobs/" + response.json()["job"]).json()
                    if job["status"] != "running":
                        break
                    time.sleep(0.05)
                srt = None
                if job["status"] == "done":
                    srt = (tmp / f"{job['result']['base']}.srt").read_text(encoding="utf-8")
                    self.assertEqual((tmp / f"{job['result']['base']}.wav").read_bytes()[:4], b"RIFF")
                return job.get("code") or 200, job, calls, srt

    def test_long_script_is_split_with_same_voice_and_styles(self):
        first = "Hello world. " * 120
        second = "Goodbye world. " * 120
        code, job, calls, srt = self.request("[excited] " + first + "\n\n" + second, chunk_chars=600)
        self.assertEqual(code, 200, job)
        self.assertGreater(len(calls), 2)
        for c in calls:
            self.assertEqual(c["generation_config"]["speech_config"], [{"voice": "Charon"}])
        styles = [p["annotations"][0]["style"] for c in calls for p in c["input"][0]["content"]]
        self.assertEqual(styles[0], "excited"); self.assertEqual(styles[-1], "calm")
        self.assertNotIn("[excited]", srt)

    def test_chunking_off_sends_one_request(self):
        code, job, calls, _ = self.request("Para one.\n\nPara two.", chunk_chars=0)
        self.assertEqual(code, 200, job)
        self.assertEqual(len(calls), 1)

    def test_long_pauses_are_shortened(self):
        code, job, _, _ = self.request("Ek.\n\nDo.\n\nTeen.", chunk_chars=0, max_pause_ms=300, edge_ms=100)
        self.assertEqual(code, 200, job)
        raw = 3 * 2.5
        self.assertLess(job["result"]["duration"], raw - 3)
        self.assertGreater(job["result"]["removed"], 3)

    def test_silence_off_keeps_audio(self):
        code, job, _, _ = self.request("Ek.\n\nDo.", chunk_chars=0, silence_enabled="false")
        self.assertEqual(code, 200, job)
        self.assertEqual(job["result"]["removed"], 0)
        self.assertAlmostEqual(job["result"]["duration"], 5.0, places=1)

    def test_urdu_subtitles_are_right_to_left(self):
        code, job, _, srt = self.request("Salam dosto.", chunk_chars=0, script_out="urdu")
        self.assertEqual(code, 200, job)
        self.assertIn("‏اردو", srt)

    def test_rate_limit_error_is_reported(self):
        code, job, calls, _ = self.request("Hello.", 429)
        self.assertEqual(code, 429)
        self.assertEqual(len(calls), 1)

    def test_empty_style_only_script_does_not_call_api(self):
        code, body, calls, _ = self.request("[excited]")
        self.assertEqual(code, 400)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
