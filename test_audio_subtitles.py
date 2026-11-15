"""Tests for audio_tools.py and subtitles.py (no FastAPI needed)."""
import io
import math
import struct
import unittest
import wave

import audio_tools as at
import subtitles as st


def wav(pattern, fr=24000):
    """pattern: list of (seconds, loud)"""
    frames = bytearray()
    for sec, loud in pattern:
        n = int(sec * fr)
        frames += b"".join(struct.pack("<h", int(7000 * math.sin(i * 0.05)) if loud else 0) for i in range(n))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(fr); w.writeframes(bytes(frames))
    return out.getvalue()


class SilenceTests(unittest.TestCase):
    def test_long_pause_shortened_short_pause_kept(self):
        data = wav([(1, True), (2, False), (1, True), (0.2, False), (1, True)])
        out, cuts = at.shorten_silences(data, max_pause_ms=400, threshold_db=-42, edge_ms=100)
        self.assertAlmostEqual(at.wav_duration(out), 5.2 - 1.6, places=1)
        self.assertEqual(len(cuts), 1)

    def test_edges_trimmed(self):
        data = wav([(1.5, False), (1, True), (1.5, False)])
        out, _ = at.shorten_silences(data, 400, -42, 100)
        self.assertAlmostEqual(at.wav_duration(out), 1.2, places=1)

    def test_disabled(self):
        data = wav([(1, True), (2, False), (1, True)])
        out, cuts = at.shorten_silences(data, 400, -42, 100, enabled=False)
        self.assertEqual(cuts, []); self.assertAlmostEqual(at.wav_duration(out), 4.0, places=2)

    def test_map_time(self):
        cuts = [(1.2, 1.6)]
        self.assertEqual(at.map_time(1.0, cuts), 1.0)
        self.assertAlmostEqual(at.map_time(3.0, cuts), 1.4)
        self.assertAlmostEqual(at.map_time(2.0, cuts), 1.2)

    def test_speech_regions(self):
        regions = at.speech_regions(wav([(1, True), (1, False), (1, True)]))
        self.assertEqual(len(regions), 2)


class SubtitleTests(unittest.TestCase):
    def test_cues_skip_pauses(self):
        regions = [(0, 1), (3, 4)]
        cues = st.timed_cues([("Pehla jumla. Doosra jumla.", 0, 4)], regions)
        self.assertEqual(len(cues), 2)
        self.assertLessEqual(cues[0][1], 1.05)
        self.assertGreaterEqual(cues[1][0], 2.95)

    def test_urdu_srt_has_rtl_marks_and_round_trips(self):
        srt = st.format_srt([(0, 1.5, "کو عدالت پہنچا دیا!")], "urdu")
        self.assertIn("‏کو", srt)
        self.assertEqual(st.parse_srt(srt), [(0.0, 1.5, "کو عدالت پہنچا دیا!")])

    def test_detect_script(self):
        self.assertEqual(st.detect_script("चैटबॉट के एक झूठ"), "hindi")
        self.assertEqual(st.detect_script("چیٹ بوٹ"), "urdu")
        self.assertEqual(st.detect_script("Chatbot ka jhooth"), "roman")

    def test_parse_converted_checks_length(self):
        self.assertEqual(st.parse_converted('["a","b"]', 2), ["a", "b"])
        with self.assertRaises(ValueError):
            st.parse_converted('["a"]', 2)


if __name__ == "__main__":
    unittest.main()
