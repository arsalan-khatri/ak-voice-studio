"""WAV helpers: merging, silence detection / shortening, speech regions.

Pure standard library; uses numpy for speed when it is installed.
All functions work on 16-bit PCM WAV bytes (Gemini TTS output is 24 kHz mono 16-bit).
"""
import io
import math
import wave
from array import array

try:  # optional speed-up
    import numpy as np
except Exception:  # pragma: no cover - fallback path tested separately
    np = None

WINDOW_SEC = 0.01  # 10 ms analysis windows


def wav_params_and_frames(data: bytes):
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data)) as w:
            return (w.getnchannels(), w.getsampwidth(), w.getframerate()), w.readframes(w.getnframes())
    # headerless PCM fallback: 24kHz mono 16-bit
    return (1, 2, 24000), data


def make_wav(params, frames: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(params[0]); w.setsampwidth(params[1]); w.setframerate(params[2])
        w.writeframes(frames)
    return out.getvalue()


def wav_duration(data: bytes) -> float:
    (nch, sw, fr), frames = wav_params_and_frames(data)
    return len(frames) / float(nch * sw * fr)


def merge_wavs(parts: list, pause_ms: int = 0):
    """Merge WAV parts with optional silence between; returns (wav bytes, start time of each part)."""
    params, all_frames, starts, t = None, [], [], 0.0
    for i, p in enumerate(parts):
        prm, frames = wav_params_and_frames(p)
        params = params or prm
        nch, sw, fr = params
        starts.append(t)
        all_frames.append(frames)
        t += len(frames) / (nch * sw * fr)
        if i < len(parts) - 1 and pause_ms > 0:
            n = int(fr * pause_ms / 1000)
            all_frames.append(b"\x00" * n * nch * sw)
            t += n / fr
    return make_wav(params or (1, 2, 24000), b"".join(all_frames)), starts


# ---------------------------------------------------------------- analysis

def _window_db(frames: bytes, params) -> list:
    """dBFS level of every 10 ms window (mono mix)."""
    nch, sw, fr = params
    if sw != 2:
        raise ValueError("Sirf 16-bit WAV supported hai.")
    win = max(1, int(fr * WINDOW_SEC))
    if np is not None:
        s = np.frombuffer(frames[: len(frames) - len(frames) % (2 * nch)], dtype="<i2").astype(np.float64)
        if nch > 1:
            s = s.reshape(-1, nch).mean(axis=1)
        n = len(s) // win
        if n == 0:
            return []
        rms = np.sqrt((s[: n * win].reshape(n, win) ** 2).mean(axis=1))
        return list(20 * np.log10(np.maximum(rms, 1.0) / 32768.0))
    a = array("h"); a.frombytes(frames[: len(frames) - len(frames) % (2 * nch)])
    if nch > 1:
        a = array("h", (sum(a[i:i + nch]) // nch for i in range(0, len(a), nch)))
    out = []
    for i in range(0, len(a) - win + 1, win):
        acc = 0
        for x in a[i:i + win]:
            acc += x * x
        rms = math.sqrt(acc / win)
        out.append(20 * math.log10(max(rms, 1.0) / 32768.0))
    return out


def silent_runs(levels: list, threshold_db: float, min_windows: int) -> list:
    """[(start_window, end_window)] of runs below threshold that are at least min_windows long."""
    runs, start = [], None
    for i, db in enumerate(levels + [0.0]):  # sentinel closes the last run
        if db < threshold_db:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= min_windows:
                runs.append((start, i))
            start = None
    return runs


def speech_regions(data: bytes, threshold_db: float = -42.0, min_gap: float = 0.18):
    """Non-silent (start, end) seconds. Gaps shorter than min_gap are merged."""
    params, frames = wav_params_and_frames(data)
    levels = _window_db(frames, params)
    if not levels:
        return []
    gaps = silent_runs(levels, threshold_db, max(1, int(min_gap / WINDOW_SEC)))
    regions, cur = [], 0
    for s, e in gaps:
        if s > cur:
            regions.append((cur * WINDOW_SEC, s * WINDOW_SEC))
        cur = e
    if cur < len(levels):
        regions.append((cur * WINDOW_SEC, len(levels) * WINDOW_SEC))
    return regions


# ---------------------------------------------------------------- silence control

def shorten_silences(data: bytes, max_pause_ms: int = 500, threshold_db: float = -42.0,
                     edge_ms: int = 120, enabled: bool = True):
    """Shorten every pause longer than max_pause_ms to max_pause_ms, trim leading/trailing silence
    to edge_ms. Returns (wav bytes, cuts) where cuts = [(orig_start_sec, removed_sec)] sorted."""
    params, frames = wav_params_and_frames(data)
    if not enabled:
        return make_wav(params, frames), []
    nch, sw, fr = params
    bpf = nch * sw  # bytes per frame
    win = max(1, int(fr * WINDOW_SEC))
    levels = _window_db(frames, params)
    total_frames = len(frames) // bpf
    keep_w = max(0, int(round(max_pause_ms / 1000 / WINDOW_SEC)))
    edge_w = max(0, int(round(edge_ms / 1000 / WINDOW_SEC)))
    removes = []  # (start_frame, end_frame) to delete
    runs = silent_runs(levels, threshold_db, 1)
    n = len(levels)
    for s, e in runs:
        length = e - s
        if s == 0:  # leading silence
            if length > edge_w:
                removes.append((0, (e - edge_w) * win))
        elif e >= n:  # trailing silence (till end of audio)
            if length > edge_w:
                removes.append(((s + edge_w) * win, total_frames))
        elif length > keep_w:
            # keep half of the allowed pause on each side so word tails and breaths stay natural
            head = keep_w // 2
            tail = keep_w - head
            removes.append(((s + head) * win, (e - tail) * win))
    removes = [(a, min(b, total_frames)) for a, b in removes if min(b, total_frames) > a]
    if not removes:
        return make_wav(params, frames), []
    out, cuts, pos = [], [], 0
    fade = min(int(fr * 0.004), 96)  # 4 ms crossfade-free short fade to avoid clicks
    for a, b in removes:
        out.append(frames[pos * bpf: a * bpf])
        cuts.append((a / fr, (b - a) / fr))
        pos = b
    out.append(frames[pos * bpf:])
    joined = b"".join(out)
    if fade and sw == 2:
        joined = _soften_joins(joined, params, removes, fade)
    return make_wav(params, joined), cuts


def _soften_joins(frames: bytes, params, removes, fade):
    """Tiny fade-out/in around each cut point to avoid clicks."""
    nch, sw, fr = params
    a = array("h"); a.frombytes(frames)
    removed = 0
    for start, end in removes:
        cut = (start - removed) * nch  # sample index of join in output
        removed += end - start
        for k in range(fade):
            g = k / fade
            for c in range(nch):
                i = cut - (k + 1) * nch + c
                if 0 <= i < len(a):
                    a[i] = int(a[i] * g)
                j = cut + k * nch + c
                if 0 <= j < len(a):
                    a[j] = int(a[j] * g)
    return a.tobytes()


def map_time(t: float, cuts: list) -> float:
    """Map a time in the original audio to the processed audio after cuts."""
    shift = 0.0
    for start, removed in cuts:
        if t >= start + removed:
            shift += removed
        elif t > start:
            shift += t - start
            break
        else:
            break
    return max(0.0, t - shift)
