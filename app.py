"""
AK Voice Studio - Gemini 3.8 TTS based voice tool.
Features: Studio voiceover (chunked for a consistent voice), podcast dialogue, silence control,
subtitles (SRT) in Roman / Hindi / Urdu script, voice design, voice cloning, live voice agent, history.
Run:  python app.py   then open http://127.0.0.1:8000
"""
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from key_manager import KeyStore, failover_reason
from markup import clean_line, split_styles
from fastapi.staticfiles import StaticFiles
from audio_tools import map_time, merge_wavs, shorten_silences, speech_regions, wav_duration, wav_params_and_frames
from subtitles import (SCRIPTS, clean_for_subtitle, convert_prompt, detect_script, format_srt, offline_convert, parse_converted,
                       parse_srt, timed_cues, words_to_cues)

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "outputs"
SEG_DIR = OUTPUT_DIR / "segments"
RAW_DIR = OUTPUT_DIR / "raw"   # untouched generated audio + metadata, so pauses/subtitles can be re-done any time
SEG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(BASE_DIR / ".env")

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
UPLOAD_ROOT = "https://generativelanguage.googleapis.com/upload/v1beta"
DEFAULT_MODEL = os.getenv("TTS_MODEL", "gemini-3.8-flash-tts")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gemini-3.5-transcribe")
# Voice Agent (Live API) model. Empty = auto: the app asks the Google catalog which models support the Live API
# (bidiGenerateContent) for your key and uses the best one. Set it only to force a specific model.
LIVE_MODEL = os.getenv("LIVE_MODEL", "").strip().removeprefix("models/")
LIVE_FALLBACK_MODEL = "gemini-3.8-live"  # last resort when the catalog cannot be read
TEXT_MODEL = os.getenv("TEXT_MODEL", "")  # for subtitle script conversion; empty = pick newest Flash model automatically
MAX_CHUNK_CHARS = int(os.getenv("MAX_CHUNK_CHARS", "1800"))
# Shorter requests keep the voice / accent stable. 0 = whole script in one request.
CONSISTENCY_CHUNK_CHARS = int(os.getenv("CONSISTENCY_CHUNK_CHARS", "900"))
# When every key hits the rate limit, wait this long and try the same chunk again.
CHUNK_DELAY_SEC = float(os.getenv("CHUNK_DELAY_SEC", "21"))
RATE_LIMIT_RETRIES = int(os.getenv("RATE_LIMIT_RETRIES", "4"))
KNOWN_TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts",
                    "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"]

app = FastAPI(title="AK Voice Studio")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
KEYS = KeyStore(BASE_DIR / ".api_keys.json", os.getenv("GEMINI_API_KEY", ""))


@app.middleware("http")
async def protect_key_settings(request, call_next):
    if request.url.path.startswith("/api/keys") and request.method != "GET":
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Key settings require same-origin requests."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site key changes are blocked."}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/api/keys"):
        response.headers["Cache-Control"] = "no-store"
    elif not request.url.path.startswith(("/api/", "/outputs/")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/api/keys")
def get_keys():
    return KEYS.public()


@app.post("/api/keys")
def add_key(payload: dict = Body(...)):
    if not isinstance(payload.get("key"), str) or not isinstance(payload.get("label", ""), str):
        raise HTTPException(400, "Key aur naam text mein dein.")
    return KEYS.add(payload["key"], payload.get("label", ""))


@app.post("/api/keys/{key_id}/select")
def select_key(key_id: str):
    return KEYS.select(key_id)


@app.delete("/api/keys/{key_id}")
def remove_key(key_id: str):
    return KEYS.remove(key_id)



# ======================= helpers =======================

def log_error(where: str, exc: BaseException):
    import traceback
    try:
        with open(OUTPUT_DIR / "error.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {where}\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass


@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    log_error(request.url.path, exc)
    return JSONResponse({"detail": f"Server error ({type(exc).__name__}): {str(exc)[:300]} — poori detail outputs/error.log mein."}, status_code=500)


def api_key() -> str:
    return KEYS.ordered()[0]["key"]


def headers() -> dict:
    return {"x-goog-api-key": api_key(), "Content-Type": "application/json"}


def raise_api_error(r: httpx.Response):
    try:
        msg = r.json().get("error", {}).get("message") or r.text
    except Exception:
        msg = r.text
    for item in KEYS.items:
        msg = msg.replace(item["key"], "[hidden key]")
    raise HTTPException(r.status_code, f"Gemini API error: {msg[:800]}")


def safe_name(name: str, default: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name or "").strip("_")[:60] or default


def find_audio_b64(obj):
    """Return the last base64 audio payload found anywhere in the response."""
    found = None
    if isinstance(obj, dict):
        if obj.get("type") == "audio" and isinstance(obj.get("data"), str):
            found = obj["data"]
        inline = obj.get("inlineData") or obj.get("inline_data")
        if isinstance(inline, dict) and str(inline.get("mimeType", inline.get("mime_type", ""))).startswith("audio"):
            found = inline.get("data")
        for v in obj.values():
            sub = find_audio_b64(v)
            if sub:
                found = sub
    elif isinstance(obj, list):
        for v in obj:
            sub = find_audio_b64(v)
            if sub:
                found = sub
    return found


def gemini_request(client, method, url, **kwargs):
    """Try each configured key once, starting at the active key and wrapping around."""
    candidates = KEYS.ordered()
    for i, item in enumerate(candidates):
        for attempt in range(3):  # Google 500/502/503/504 or network hiccup: wait and try again
            try:
                r = client.request(method, url, headers={"x-goog-api-key": item["key"], "Content-Type": "application/json"}, **kwargs)
            except httpx.TransportError as e:
                if attempt == 2:
                    raise HTTPException(504, f"Google se connection nahi bana ya time out hua ({type(e).__name__}). Internet check karke dobara try karein.")
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            break
        reason = failover_reason(r)
        if reason:
            KEYS.mark(item["id"], reason)
            if i + 1 < len(candidates):
                KEYS.advance(item, candidates[i + 1], reason)
                continue
            KEYS.event("Saari configured keys try ho gayi hain. Key update karein ya quota reset ke baad Generate dabayein.")
            raise HTTPException(r.status_code, "Saari keys unavailable hain (" + reason + "). Key update karein ya quota reset ke baad dobara try karein.")
        if r.status_code >= 400:
            raise_api_error(r)
        KEYS.mark(item["id"], "Working")
        r.extensions["key_attempts"] = i + 1
        return r


def tts_request(client: httpx.Client, body: dict, *, retry: bool = True, stats: dict | None = None) -> bytes:
    """Whole request failover only; never repeat a key in the same operation."""
    model = body["model"]
    if model.startswith("gemini-2.5-"):
        # Legacy TTS models use generateContent and natural-language style prompts.
        config = body["generation_config"]["speech_config"]
        voices = config if isinstance(config, list) else config.get("speakers", [])
        if any(v["voice"].startswith(("voice_", "voicekey_")) for v in voices):
            raise HTTPException(400, "Is legacy model mein custom/cloned voice supported nahi. Prebuilt voice ya Gemini 3.8 chunein.")
        lines = []
        for entry in body["input"][0]["content"]:
            meta = next(iter(entry.get("annotations", [])), {})
            prefix = (meta.get("speaker", "") + ": ") if meta.get("speaker") else ""
            direction = "[" + meta["style"] + "] " if meta.get("style") else ""
            lines.append(prefix + direction + entry["text"])
        if isinstance(config, list):
            speech = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": config[0]["voice"]}}}
        else:
            speech = {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
                {"speaker": v["speaker"], "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": v["voice"]}}} for v in voices]}}
        language = next((v.get("language") for v in voices if v.get("language")), None)
        if language:
            speech["languageCode"] = language
        payload = {"contents": [{"parts": [{"text": "Read the transcript aloud. Bracketed directions control delivery; do not read them aloud.\n\n" + "\n\n".join(lines)}]}],
                   "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": speech}}
        r = gemini_request(client, "POST", f"{API_ROOT}/models/{model}:generateContent", json=payload)
    else:
        r = gemini_request(client, "POST", f"{API_ROOT}/interactions", json=body)
    if stats is not None:
        stats["requests"] = r.extensions.get("key_attempts", 1)
    b64 = find_audio_b64(r.json())
    if not b64:
        raise HTTPException(502, "Response mein audio nahi mili.")
    return base64.b64decode(b64)


def _is_tts_model(m: dict) -> bool:
    text = " ".join(str(m.get(k, "")) for k in ("name", "displayName", "description")).lower()
    return "tts" in text or "text-to-speech" in text or "speech generation" in text


@app.get("/api/models")
def tts_models():
    """Every TTS model in the Google catalog for the active key (all pages)."""
    models, token = {}, None
    with httpx.Client(timeout=60) as client:
        while True:
            params = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            r = gemini_request(client, "GET", f"{API_ROOT}/models", params=params)
            data = r.json()
            for m in data.get("models", []):
                name = m.get("name", "").removeprefix("models/")
                if name and _is_tts_model(m):
                    models[name] = {"id": name, "name": m.get("displayName") or name,
                                    "legacy": name.startswith("gemini-2.5-"),
                                    "preview": "preview" in name or "exp" in name}
            token = data.get("nextPageToken")
            if not token:
                break
    source = "catalog"
    if not models:  # empty catalog: still offer the known names
        source = "fallback"
        models = {n: {"id": n, "name": n, "legacy": n.startswith("gemini-2.5-"), "preview": "preview" in n}
                  for n in KNOWN_TTS_MODELS}
    return {"models": sorted(models.values(), key=lambda m: (m["legacy"], m["id"]), reverse=False), "source": source}


_TEXT_MODEL_CACHE = {}


def _version_key(name: str):
    return tuple(int(x) for x in re.findall(r"\d+", name)[:3]) or (0,)


def text_model(client) -> str:
    """Model used to convert subtitle scripts. TEXT_MODEL env wins; otherwise newest stable Flash."""
    if TEXT_MODEL:
        return TEXT_MODEL
    cached = _TEXT_MODEL_CACHE.get("m")
    if cached and time.monotonic() - cached[0] < 3600:
        return cached[1]
    names, token = [], None
    try:
        while True:
            params = {"pageSize": 1000, **({"pageToken": token} if token else {})}
            data = gemini_request(client, "GET", f"{API_ROOT}/models", params=params).json()
            for m in data.get("models", []):
                n = m.get("name", "").removeprefix("models/")
                bad = ("tts", "image", "live", "audio", "embed", "transcribe", "vision", "thinking", "robotics", "computer")
                if ("generateContent" in m.get("supportedGenerationMethods", []) and n.startswith("gemini-")
                        and "flash" in n and not any(b in n for b in bad)):
                    names.append(n)
            token = data.get("nextPageToken")
            if not token:
                break
    except HTTPException:
        names = []
    stable = [n for n in names if "preview" not in n and "exp" not in n] or names
    pick = max(stable, key=_version_key) if stable else "gemini-2.5-flash"
    _TEXT_MODEL_CACHE["m"] = (time.monotonic(), pick)
    return pick


_LIVE_MODEL_CACHE = {}


def _live_rank(name: str):
    """Sort key, best last: native-audio Live models, then stable over preview/exp, then newest version."""
    return ("native-audio" in name, "preview" not in name and "exp" not in name, _version_key(name), name)


def live_models(client) -> list:
    """Models of the Google catalog that support the Live API (bidiGenerateContent) for the active key, best first."""
    cached = _LIVE_MODEL_CACHE.get("list")
    if cached and cached[2] == KEYS.active and time.monotonic() - cached[0] < 600:
        return cached[1]
    found, token = set(), None
    while True:
        params = {"pageSize": 1000, **({"pageToken": token} if token else {})}
        data = gemini_request(client, "GET", f"{API_ROOT}/models", params=params).json()
        for m in data.get("models", []):
            n = m.get("name", "").removeprefix("models/")
            if ("bidiGenerateContent" in m.get("supportedGenerationMethods", [])
                    and not any(b in n for b in ("tts", "image", "embed", "transcribe"))):
                found.add(n)
        token = data.get("nextPageToken")
        if not token:
            break
    ranked = sorted(found, key=_live_rank, reverse=True)
    if ranked:  # never cache an empty answer: the user may fix the key and retry at once
        _LIVE_MODEL_CACHE["list"] = (time.monotonic(), ranked, KEYS.active)
    return ranked


def _live_models_sync() -> list:
    with httpx.Client(timeout=60) as client:
        return live_models(client)


def _model_unavailable(err: Exception) -> bool:
    """True when Live refused the MODEL itself (404 / not found / not supported), so another model may work."""
    text = f"{type(err).__name__} {err}".lower()
    return any(t in text for t in ("404", "not found", "not supported", "bidigeneratecontent", "no such model"))


LANGUAGE_FILTERS = {"ur-PK": "ur-PK,ur-IN,ur", "en-US": "en-US", "hi-IN": "hi-IN"}
_LANGUAGE_VOICES = {}
MULTILINGUAL_VOICES = set("Zephyr Puck Charon Kore Fenrir Leda Orus Aoede Callirrhoe Autonoe Enceladus Iapetus Umbriel Algieba Despina Erinome Algenib Rasalgethi Laomedeia Achernar Alnilam Schedar Gacrux Pulcherrima Achird Zubenelgenubi Vindemiatrix Sadachbia Sadaltager Sulafat".split())


def apply_speech_language(body, language):
    if language:
        config = body["generation_config"]["speech_config"]
        for speaker in config if isinstance(config, list) else config["speakers"]:
            speaker["language"] = language

@app.get("/api/language-voices")
def language_voices(language: str):
    if not isinstance(language, str) or language not in LANGUAGE_FILTERS:
        raise HTTPException(400, "Supported speaking language select karein.")
    cache_key = (hashlib.sha256(api_key().encode()).hexdigest(), language)
    cached = _LANGUAGE_VOICES.get(cache_key)
    if cached and time.monotonic() - cached[0] < 600:
        return {"voices": cached[1]}
    params = [("type", "prebuilt"), ("page_size", "1000")]
    params += [("language_code", lang) for lang in LANGUAGE_FILTERS[language].split(",")]
    voices, token = [], None
    with httpx.Client(timeout=60) as client:
        while True:
            page_params = params + ([("page_token", token)] if token else [])
            data = gemini_request(client, "GET", f"{API_ROOT}/voices", params=page_params).json()
            for v in data.get("voices", []):
                code = v.get("language_code") or v.get("languageCode") or ""
                if code.lower() not in LANGUAGE_FILTERS[language].lower().split(","):
                    continue
                voice_id = v.get("id") or v.get("name", "").split("/")[-1]
                if voice_id:
                    voices.append({"id": voice_id, "display_name": v.get("display_name") or v.get("displayName") or voice_id,
                                   "language_code": code, "gender": v.get("gender", ""), "accent": v.get("accent", "")})
            token = data.get("next_page_token") or data.get("nextPageToken")
            if not token:
                break
    _LANGUAGE_VOICES[cache_key] = (time.monotonic(), voices)
    return {"voices": voices}


def validate_language_voices(language: str, voices: list[str]):
    if not language:
        return
    allowed = {v["id"] for v in language_voices(language)["voices"]}
    if not allowed:
        allowed = MULTILINGUAL_VOICES
    if not all(v in allowed for v in voices):
        raise HTTPException(400, "Matching regional voice chunein; agar catalog khaali hai to dropdown se standard voice manually select karein. Unknown voice par fallback nahi kiya gaya.")



def split_script(text: str, limit: int) -> list[str]:
    """Split script by paragraphs, then sentences, keeping chunks under limit."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        pieces = [p] if len(p) <= limit else re.split(r"(?<=[.!?۔])\s+", p)
        for piece in pieces:
            while len(piece) > limit:  # very long sentence fallback
                if cur:
                    chunks.append(cur); cur = ""
                chunks.append(piece[:limit]); piece = piece[limit:]
            if len(cur) + len(piece) + 2 <= limit:
                cur = f"{cur}\n\n{piece}" if cur else piece
            else:
                if cur:
                    chunks.append(cur)
                cur = piece
    if cur:
        chunks.append(cur)
    return chunks


STYLE_TAG = re.compile(r"^\[([^\]]{2,200})\]\s*", re.S)


def parse_paragraph(para: str, default_style: str) -> tuple[str, str]:
    m = STYLE_TAG.match(para)
    if m:
        return para[m.end():].strip(), m.group(1).strip()
    return para.strip(), default_style


# ======================= background jobs (progress + cancel) =======================

JOBS: dict = {}
JOBS_LOCK = threading.Lock()


class Cancelled(Exception):
    pass


def start_job(kind: str, fn) -> dict:
    job = {"id": uuid.uuid4().hex[:12], "kind": kind, "status": "running", "done": 0, "total": 1,
           "message": "Shuru ho raha hai…", "result": None, "error": None, "code": None,
           "cancel": False, "started": time.time()}
    with JOBS_LOCK:
        JOBS[job["id"]] = job
        for old in sorted(JOBS.values(), key=lambda j: j["started"])[:-40]:  # keep memory small
            JOBS.pop(old["id"], None)

    def run():
        try:
            job["result"] = fn(job)
            job["status"], job["message"] = "done", "Tayyar!"
        except Cancelled:
            job["status"], job["message"] = "cancelled", "Rok diya gaya. Bane hue hisse cache mein hain; dobara Generate par wahin se shuru hoga."
        except HTTPException as e:
            job["status"], job["error"], job["code"] = "error", str(e.detail), e.status_code
        except Exception as e:  # pragma: no cover - unexpected
            log_error("job " + kind, e)
            job["status"], job["error"], job["code"] = "error", f"{type(e).__name__}: {str(e)[:400]} — detail outputs/error.log mein.", 500

    threading.Thread(target=run, daemon=True).start()
    return {"job": job["id"]}


def job_step(job: dict | None, done=None, total=None, message=None):
    if job is None:
        return
    if job.get("cancel"):
        raise Cancelled()
    if done is not None:
        job["done"] = done
    if total is not None:
        job["total"] = total
    if message is not None:
        job["message"] = message


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job nahi mila (server restart hua ho sakta hai).")
    return {k: v for k, v in job.items() if k != "cancel"}


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job nahi mila.")
    job["cancel"] = True
    return {"ok": True}


# ======================= generation settings =======================

def silence_settings(src: dict) -> dict:
    def num(key, default, lo, hi):
        try:
            return max(lo, min(hi, float(src.get(key, default))))
        except (TypeError, ValueError):
            return default
    enabled = str(src.get("silence_enabled", "true")).lower() not in ("false", "0", "off", "")
    return {"enabled": enabled,
            "max_pause_ms": int(num("max_pause_ms", 450, 50, 5000)),
            "threshold_db": num("threshold_db", -42, -70, -15),
            "edge_ms": int(num("edge_ms", 120, 0, 2000))}


def sub_script(value) -> str:
    value = str(value or "original").lower()
    if value not in SCRIPTS:
        raise HTTPException(400, "Subtitle script: original, roman, hindi ya urdu.")
    return value


def chunk_limit(value) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = CONSISTENCY_CHUNK_CHARS
    return 0 if v <= 0 else max(200, min(v, 5000))


def plan_chunks(items: list[dict], limit: int, size=lambda it: len(it["text"])) -> list[list[dict]]:
    """Pack items (paragraphs or dialogue turns) into requests of at most `limit` characters.
    Long single paragraphs are split at sentence boundaries. limit=0 -> one request."""
    if limit <= 0:
        return [items]
    expanded = []
    for it in items:
        if size(it) > limit and "speaker" not in it:
            expanded += [{**it, "text": piece} for piece in split_script(it["text"], limit)]
        else:
            expanded.append(it)
    chunks, cur, cur_len = [], [], 0
    for it in expanded:
        n = size(it)
        if cur and cur_len + n > limit:
            chunks.append(cur); cur, cur_len = [], 0
        cur.append(it); cur_len += n
    if cur:
        chunks.append(cur)
    return chunks


def synth_cached(client, body: dict, take: str, job=None, label="") -> bytes:
    """One TTS request with on-disk cache (resume after failure) and patient rate-limit retries."""
    key = hashlib.sha1((take + "|" + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()[:20]
    path = SEG_DIR / f"c_{key}.wav"
    if path.exists():
        return path.read_bytes()
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            data = tts_request(client, body)
            break
        except HTTPException as e:
            if e.status_code != 429 or attempt >= RATE_LIMIT_RETRIES:
                raise
            for left in range(int(CHUNK_DELAY_SEC), 0, -1):
                job_step(job, message=f"{label}: rate limit. {left}s baad dobara try (attempt {attempt + 2})…")
                time.sleep(1)
    audio, _ = merge_wavs([data], 0)  # normalise to a proper WAV
    path.write_bytes(audio)
    return audio


# ======================= render: silence control + subtitles =======================

_CONVERT_CACHE: dict = {}


def convert_texts(texts: list[str], script: str) -> list[str]:
    """Transliterate caption texts into the chosen script with a Gemini text model."""
    if script == "original" or not texts:
        return texts
    if script != "english" and detect_script(" ".join(texts)) == script:
        return texts
    todo = [t for t in dict.fromkeys(texts) if (script, t) not in _CONVERT_CACHE and t.strip()]
    with httpx.Client(timeout=180) as client:
        model = text_model(client)

        def run(batch):
            payload = {"contents": [{"role": "user", "parts": [{"text": convert_prompt(batch, script)}]}],
                       "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
            r = gemini_request(client, "POST", f"{API_ROOT}/models/{model}:generateContent", json=payload)
            parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            raw = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            try:
                out = parse_converted(raw, len(batch))
            except (ValueError, json.JSONDecodeError):
                if len(batch) == 1:
                    raise HTTPException(502, "Subtitle script convert nahi hui (model ka jawab samajh nahi aaya).")
                mid = len(batch) // 2
                run(batch[:mid]); run(batch[mid:])
                return
            for src, dst in zip(batch, out):
                _CONVERT_CACHE[(script, src)] = dst

        for i in range(0, len(todo), 40):
            run(todo[i:i + 40])
    return [_CONVERT_CACHE.get((script, t), t) for t in texts]


def meta_path(base: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", base or ""):
        raise HTTPException(400, "Galat file naam.")
    return RAW_DIR / f"{base}.json"


def save_raw(base: str, audio: bytes, meta: dict):
    (RAW_DIR / f"{base}.wav").write_bytes(audio)
    meta_path(base).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def load_meta(base: str) -> dict:
    p = meta_path(base)
    if not p.exists():
        raise HTTPException(404, "Is file ka raw data nahi mila (purani file?). Nayi generate karein.")
    return json.loads(p.read_text(encoding="utf-8"))


def render(base: str, silence: dict | None = None, script: str | None = None, job=None) -> dict:
    """(Re)build outputs/<base>.wav and .srt from raw audio: shorten pauses, time captions, convert script."""
    meta = load_meta(base)
    silence = silence or meta.get("silence") or silence_settings({})
    script = script or meta.get("script") or "original"
    v = int(time.time() * 1000)
    if meta.get("kind") == "transcript":
        cues = [tuple(c) for c in meta["cues"]]
        duration = meta.get("duration", cues[-1][1] if cues else 0)
        removed = 0.0
    else:
        raw = (RAW_DIR / f"{base}.wav").read_bytes()
        job_step(job, message="Khali jagah (pauses) theek ho rahi hai…")
        audio, cuts = shorten_silences(raw, silence["max_pause_ms"], silence["threshold_db"],
                                       silence["edge_ms"], silence["enabled"])
        (OUTPUT_DIR / f"{base}.wav").write_bytes(audio)
        duration = wav_duration(audio)
        removed = sum(c[1] for c in cuts)
        segs = [(t, map_time(s, cuts), map_time(e, cuts)) for t, s, e in meta["segments"]]
        regions = speech_regions(audio, silence["threshold_db"])
        cues = timed_cues(segs, regions)
    texts = [c[2] for c in cues]
    if script != "original":
        job_step(job, message=f"Subtitles {SCRIPTS[script].split(' (')[0]} script mein convert ho rahe hain…")
    warning = None
    try:
        converted = convert_texts(texts, script)
    except Exception as e:  # never lose the subtitles because of the optional conversion step
        if not isinstance(e, HTTPException):
            log_error("subtitle conversion", e)
        detail = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
        name = SCRIPTS[script].split(' (')[0]
        offline = [offline_convert(t, script) for t in texts]
        if offline != texts:
            converted = offline
            warning = f"Gemini se {name} conversion nahi hui ({str(detail)[:160]}), is liye offline conversion use hui — English alfaaz thode ghalat ho sakte hain."
        else:
            warning = f"{name} mein convert nahi hua ({str(detail)[:200]}). Original subtitles de diye — script button se dobara try karein."
            converted, script = texts, "original"
    cues = [(a, b, t) for (a, b, _), t in zip(cues, converted)]
    srt = format_srt(cues, script)
    (OUTPUT_DIR / f"{base}.srt").write_text(srt, encoding="utf-8")
    (OUTPUT_DIR / f"{base}.txt").write_text("\n".join(c[2] for c in cues), encoding="utf-8")
    meta.update({"silence": silence, "script": script, "duration": round(duration, 2), "removed": round(removed, 2)})
    meta_path(base).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    out = {"base": base, "kind": meta.get("kind"), "title": meta.get("title", base),
           "srt": f"/outputs/{base}.srt?v={v}", "txt": f"/outputs/{base}.txt?v={v}", "file": f"{base}.wav",
           "duration": round(duration, 2), "removed": round(removed, 2), "script": script, "silence": silence,
           "cues": [{"start": round(a, 3), "end": round(b, 3), "text": t} for a, b, t in cues],
           "requests": meta.get("requests", 1), "chunks": meta.get("chunks", 1), "warning": warning}
    if meta.get("kind") != "transcript":
        out["wav"] = out["url"] = f"/outputs/{base}.wav?v={v}"
        out["raw"] = f"/outputs/raw/{base}.wav"
    return out


@app.post("/api/render")
def rerender(payload: dict = Body(...)):
    """Re-apply pause settings and/or subtitle script to an existing generation (no new TTS call)."""
    base = str(payload.get("base", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", base):
        raise HTTPException(400, "File chunein.")
    meta = load_meta(base)
    keys = ("silence_enabled", "max_pause_ms", "threshold_db", "edge_ms")
    silence = None
    if any(k in payload for k in keys):
        cur = meta.get("silence") or {}
        merged = {"silence_enabled": cur.get("enabled", True), "max_pause_ms": cur.get("max_pause_ms", 450),
                  "threshold_db": cur.get("threshold_db", -42), "edge_ms": cur.get("edge_ms", 120)}
        merged.update({k: payload[k] for k in keys if k in payload})
        silence = silence_settings(merged)
    return render(base, silence, sub_script(payload["script"]) if payload.get("script") else None)


@app.post("/api/convert-text")
def convert_text_api(payload: dict = Body(...)):
    """Chat transcript: show text in English / Roman / Hindi / Urdu."""
    texts, script = payload.get("texts"), str(payload.get("script", "original")).lower()
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts) or len(texts) > 100:
        raise HTTPException(400, "texts list chahiye.")
    if script not in ("original", "english", "roman", "hindi", "urdu"):
        raise HTTPException(400, "Script: original, english, roman, hindi ya urdu.")
    try:
        return {"texts": convert_texts(texts, script), "offline": False}
    except Exception as e:
        if not isinstance(e, HTTPException):
            log_error("convert-text", e)
        offline = [offline_convert(t, script) for t in texts]
        if offline != texts:
            return {"texts": offline, "offline": True}
        raise


@app.get("/api/history")
def history(limit: int = 40):
    items = []
    for p in sorted(RAW_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:max(1, min(limit, 200))]:
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        base = p.stem
        v = int(p.stat().st_mtime * 1000)
        items.append({"base": base, "kind": m.get("kind"), "title": m.get("title", base),
                      "created": m.get("created"), "duration": m.get("duration"), "script": m.get("script", "original"),
                      "voice": m.get("voice"), "silence": m.get("silence"),
                      "wav": f"/outputs/{base}.wav?v={v}" if (OUTPUT_DIR / f"{base}.wav").exists() else None,
                      "srt": f"/outputs/{base}.srt?v={v}" if (OUTPUT_DIR / f"{base}.srt").exists() else None})
    return {"items": items}


@app.delete("/api/history/{base}")
def history_delete(base: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", base or ""):
        raise HTTPException(400, "Galat file naam.")
    found = False
    for p in [RAW_DIR / f"{base}.json", RAW_DIR / f"{base}.wav", OUTPUT_DIR / f"{base}.wav",
              OUTPUT_DIR / f"{base}.srt", OUTPUT_DIR / f"{base}.txt"]:
        if base and p.exists():
            p.unlink(); found = True
    if not found:
        raise HTTPException(404, "File nahi mili.")
    return {"deleted": base}


# ======================= Studio =======================

@app.post("/api/segment")
def segment(
    text: str = Form(...),
    voice: str = Form(...),
    style: str = Form(""),
    model: str = Form(DEFAULT_MODEL),
    take: int = Form(0),
    language: str = Form(""),
):
    """Generate ONE paragraph (preview / per-paragraph takes)."""
    text = text.strip()
    if not text:
        raise HTTPException(400, "Paragraph khaali hai.")
    validate_language_voices(language, [voice.strip()])
    key = hashlib.sha1(f"v2|{language}|{text}|{voice}|{style}|{model}|{take}".encode()).hexdigest()[:16]
    path = SEG_DIR / f"{key}.wav"
    if not path.exists():
        chunks = split_script(text, MAX_CHUNK_CHARS)
        parts = []
        with httpx.Client(timeout=300) as c:
            for ch in chunks:
                parts.append(synth_cached(c, synth_body(ch, voice.strip(), style, model, language), "seg"))
        audio, _ = merge_wavs(parts, 150)
        path.write_bytes(audio)
    data = path.read_bytes()
    return {"id": key, "url": f"/outputs/segments/{key}.wav", "duration": round(wav_duration(data), 3)}


def synth_body(text, voice, style, model, language):
    content = {"type": "text", "text": text}
    if style.strip():
        content["annotations"] = [{"type": "speech_metadata", "style": style.strip()}]
    body = {"model": model, "input": [{"type": "user_input", "content": [content]}],
            "response_format": {"type": "audio"}, "generation_config": {"speech_config": [{"voice": voice}]}}
    apply_speech_language(body, language)
    return body


def new_base(filename: str, default: str) -> str:
    return f"{safe_name(filename, default)}_{datetime.now():%Y%m%d_%H%M%S_%f}"


@app.post("/api/export")
def export(payload: dict = Body(...)):
    """Join generated paragraph takes: payload {segments:[{id, text}], pause_ms, filename, + silence/script}"""
    segs = payload.get("segments") or []
    if not segs:
        raise HTTPException(400, "Koi bana hua paragraph nahi hai.")
    parts = []
    for s in segs:
        p = SEG_DIR / f"{safe_name(s.get('id', ''), 'x')}.wav"
        if not p.exists():
            raise HTTPException(400, "Kuch paragraphs ki audio nahi mili - pehle generate karein.")
        parts.append(p.read_bytes())
    audio, starts = merge_wavs(parts, int(payload.get("pause_ms") or 0))
    durs = [wav_duration(p) for p in parts]
    base = new_base(payload.get("filename", ""), "voiceover")
    save_raw(base, audio, {"kind": "voiceover", "created": datetime.now().isoformat(timespec="seconds"),
                           "title": clean_for_subtitle(segs[0].get("text", ""))[:70],
                           "segments": [(s.get("text", ""), st, st + d) for s, st, d in zip(segs, starts, durs)],
                           "silence": silence_settings(payload), "script": sub_script(payload.get("script"))})
    return render(base)


@app.post("/api/tts")
def tts(
    script: str = Form(...),
    voice: str = Form(...),
    style: str = Form(""),
    model: str = Form(DEFAULT_MODEL),
    filename: str = Form(""),
    language: str = Form(""),
    chunk_chars: int = Form(CONSISTENCY_CHUNK_CHARS),
    take: str = Form("1"),
    script_out: str = Form("original"),
    silence_enabled: str = Form("true"),
    max_pause_ms: float = Form(450),
    threshold_db: float = Form(-42),
    edge_ms: float = Form(120),
):
    """Whole script -> job. Script is split into short requests (same voice, language and settings)
    so the voice/accent does not drift on long scripts; audio is joined, pauses tidied, SRT made."""
    script = script.strip()
    voice = voice.strip()
    if not script:
        raise HTTPException(400, "Script khaali hai.")
    items = []
    for para in re.split(r"\n\s*\n", script):
        para = "\n".join(clean_line(l) for l in para.splitlines()).strip()  # drop [FADE IN], [MUSIC: ..] etc.
        text, paragraph_style = parse_paragraph(para, style)
        for piece, piece_style in (split_styles(text, paragraph_style) if text else []):
            items.append({"text": piece, "style": piece_style})
    if not items:
        raise HTTPException(400, "Script khaali hai.")
    if not voice:
        raise HTTPException(400, "Voice chunein.")
    validate_language_voices(language, [voice])
    silence = silence_settings({"silence_enabled": silence_enabled, "max_pause_ms": max_pause_ms,
                                "threshold_db": threshold_db, "edge_ms": edge_ms})
    out_script = sub_script(script_out)
    chunks = plan_chunks(items, chunk_limit(chunk_chars))

    def body_for(chunk):
        content = []
        for it in chunk:
            c = {"type": "text", "text": it["text"]}
            if it["style"]:
                c["annotations"] = [{"type": "speech_metadata", "style": it["style"]}]
            content.append(c)
        body = {"model": model, "input": [{"type": "user_input", "content": content}],
                "response_format": {"type": "audio"}, "generation_config": {"speech_config": [{"voice": voice}]}}
        apply_speech_language(body, language)
        return body

    def work(job):
        parts = []
        job_step(job, 0, len(chunks) + 1)
        with httpx.Client(timeout=600) as c:
            for i, chunk in enumerate(chunks):
                job_step(job, i, message=f"Hissa {i + 1}/{len(chunks)} ban raha hai (same voice + accent)…")
                parts.append(synth_cached(c, body_for(chunk), take, job, f"Hissa {i + 1}"))
        job_step(job, len(chunks), message="Audio jor raha hoon…")
        audio, starts = merge_wavs(parts, 0)
        durs = [wav_duration(p) for p in parts]
        base = new_base(filename, "voiceover")
        save_raw(base, audio, {"kind": "voiceover", "created": datetime.now().isoformat(timespec="seconds"),
                               "title": clean_for_subtitle(items[0]["text"])[:70], "voice": voice, "model": model,
                               "language": language, "chunks": len(chunks), "requests": len(chunks),
                               "segments": [("\n\n".join(it["text"] for it in ch), st, st + d)
                                            for ch, st, d in zip(chunks, starts, durs)],
                               "silence": silence, "script": out_script})
        return render(base, job=job)

    return start_job("tts", work)


# ======================= Dialogue / podcast =======================

LABEL_RE = re.compile(r"^\s*([^\s:\[\]\n,.!?\"'<>][^:\[\]\n,.!?\"'<>]{0,24}?)\s*(?:\[([^\]\n]{1,200})\])?\s*[:：]\s*(.*)$")


def parse_dialogue(script: str, names: list[str]):
    """Forgiving parser. Returns (turns, notes).
    - 'Naam: baat' / 'Naam [emotion]: baat' lines are assigned to that speaker.
    - Lines without a name continue the previous speaker (or Speaker 1 at the start).
    - Other names in the script are mapped to the two speakers in order of appearance.
    - Stage directions ([FADE IN], [MUSIC: ..], [SFX: ..]) are dropped; [laughs] -> <laugh>;
      an [emotion] inside a line changes the style from there on."""
    raw, labels = [], []
    for line in script.splitlines():
        line = clean_line(line)
        if not line:
            continue
        m = LABEL_RE.match(line)
        if m and len(m.group(1).split()) <= 3 and m.group(3).strip():
            label = m.group(1).strip()
            raw.append((label, (m.group(2) or "").strip(), m.group(3)))
            if label.casefold() not in [l.casefold() for l in labels]:
                labels.append(label)
        else:
            raw.append((None, "", line))
    if not raw:
        return [], []
    by_fold = {n.casefold(): n for n in names}
    mapping, notes = {}, []
    free = [n for n in names if n.casefold() not in [l.casefold() for l in labels]]
    for label in labels:
        if label.casefold() in by_fold:
            mapping[label.casefold()] = by_fold[label.casefold()]
        elif free:
            mapping[label.casefold()] = free.pop(0)
            notes.append(f"'{label}' ki lines {mapping[label.casefold()]} ki voice mein.")
        else:
            raise HTTPException(400, f"Script mein 2 se zyada log hain ('{label}' bhi). Podcast abhi 2 speakers tak hai — "
                                     f"'{label}' ki lines {names[0]} ya {names[1]} ke naam se likhein.")
    turns, current, unlabeled = [], names[0], 0
    for label, style, text in raw:
        if label is not None:
            current = mapping[label.casefold()]
        else:
            unlabeled += 1
        for piece, piece_style in split_styles(text, style):
            turns.append({"speaker": current, "style": piece_style, "text": piece})
    if unlabeled:
        notes.append(f"{unlabeled} line(s) par naam nahi tha — pichhle speaker (ya {names[0]}) ki awaz mein.")
    return turns, notes


@app.post("/api/dialogue")
def dialogue(payload: dict = Body(...)):
    """payload: {script, speakers:[{name, voice},{name, voice}], model, filename, language,
    chunk_chars, take, script_out, silence settings}"""
    speakers = payload.get("speakers", [])
    if not isinstance(speakers, list) or len(speakers) != 2 or any(
        not isinstance(s, dict) or not isinstance(s.get("name"), str) or not isinstance(s.get("voice"), str)
        or not s["name"].strip() or not s["voice"].strip() for s in speakers
    ):
        raise HTTPException(400, "Dono speakers ka naam aur voice chunein.")
    speakers = [{"name": s["name"].strip(), "voice": s["voice"].strip()} for s in speakers]
    if any(not re.fullmatch(r"[^:\[\]\n]{1,40}", s["name"]) for s in speakers):
        raise HTTPException(400, "Speaker naam 1–40 characters ho; colon, brackets ya newline na ho.")
    names = {s["name"].casefold(): s["name"] for s in speakers}
    if len(names) != 2:
        raise HTTPException(400, "Dono speakers ke naam alag hone chahiye.")
    if speakers[0]["voice"] == speakers[1]["voice"]:
        raise HTTPException(400, "Dono speakers ke liye alag voices chunein.")
    script = payload.get("script", "")
    if not isinstance(script, str):
        raise HTTPException(400, "Dialogue script text honi chahiye.")
    turns, notes = parse_dialogue(script, [s["name"] for s in speakers])
    if not turns:
        raise HTTPException(400, "Dialogue khaali hai (sirf stage directions the?).")
    language = payload.get("language", "")
    validate_language_voices(language, [s["voice"] for s in speakers])
    model = payload.get("model") or DEFAULT_MODEL
    take = str(payload.get("take", "1"))
    silence = silence_settings(payload)
    out_script = sub_script(payload.get("script_out"))
    # Short requests: every request carries the SAME two speaker->voice bindings and language,
    # so a long podcast cannot drift into another accent half way through.
    chunks = plan_chunks(turns, chunk_limit(payload.get("chunk_chars", CONSISTENCY_CHUNK_CHARS)))

    def body_for(chunk):
        content = []
        for turn in chunk:
            meta = {"type": "speech_metadata", "speaker": turn["speaker"]}
            if turn["style"]:
                meta["style"] = turn["style"]
            content.append({"type": "text", "text": turn["text"], "annotations": [meta]})
        body = {"model": model, "input": [{"type": "user_input", "content": content}],
                "response_format": {"type": "audio"},
                "generation_config": {"speech_config": {"speakers": [
                    {"speaker": s["name"], "voice": s["voice"]} for s in speakers]}}}
        apply_speech_language(body, language)
        return body

    def work(job):
        parts = []
        job_step(job, 0, len(chunks) + 1)
        with httpx.Client(timeout=600) as client:
            for i, chunk in enumerate(chunks):
                job_step(job, i, message=f"Hissa {i + 1}/{len(chunks)} ({len(chunk)} lines) ban raha hai…")
                parts.append(synth_cached(client, body_for(chunk), take, job, f"Hissa {i + 1}"))
        job_step(job, len(chunks), message="Audio jor raha hoon…")
        audio, starts = merge_wavs(parts, 0)
        durs = [wav_duration(p) for p in parts]
        base = new_base((payload.get("filename") or "podcast"), "podcast")
        save_raw(base, audio, {"kind": "dialogue", "created": datetime.now().isoformat(timespec="seconds"),
                               "title": " / ".join(s["name"] for s in speakers) + ": " + clean_for_subtitle(turns[0]["text"])[:50],
                               "voice": " + ".join(f"{s['name']}={s['voice']}" for s in speakers), "model": model,
                               "language": language, "chunks": len(chunks), "requests": len(chunks), "turns": len(turns),
                               "segments": [("\n".join(t["text"] for t in ch), st, st + d)
                                            for ch, st, d in zip(chunks, starts, durs)],
                               "silence": silence, "script": out_script})
        result = render(base, job=job)
        result["turns"] = len(turns)
        result["notes"] = notes
        return result

    return start_job("dialogue", work)


# ======================= Subtitles (speech to text) =======================

def parse_offset(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):  # {"seconds": 1, "nanos": 5e8}
        return float(v.get("seconds", 0)) + float(v.get("nanos", 0)) / 1e9
    try:
        return float(str(v).rstrip("s"))
    except ValueError:
        return 0.0


def collect_words(obj, out: list):
    if isinstance(obj, dict):
        s = obj.get("start_offset", obj.get("startOffset"))
        e = obj.get("end_offset", obj.get("endOffset"))
        if s is not None and e is not None and isinstance(obj.get("text"), str):
            out.append({"text": obj["text"].strip(), "start": parse_offset(s), "end": parse_offset(e),
                        "speaker": obj.get("speaker")})
            return
        for v in obj.values():
            collect_words(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_words(v, out)


def collect_text(obj) -> str:
    """Join model output text blocks."""
    texts = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") == "text" and isinstance(o.get("text"), str):
                texts.append(o["text"])
            for k, v in o.items():
                if k != "annotations":
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj.get("steps", obj) if isinstance(obj, dict) else obj)
    return "\n".join(t for t in texts if t.strip())


def upload_file(client: httpx.Client, data: bytes, mime: str, display: str) -> dict:
    start = client.post(
        f"{UPLOAD_ROOT}/files",
        headers={"x-goog-api-key": api_key(), "X-Goog-Upload-Protocol": "resumable",
                 "X-Goog-Upload-Command": "start", "X-Goog-Upload-Header-Content-Length": str(len(data)),
                 "X-Goog-Upload-Header-Content-Type": mime, "Content-Type": "application/json"},
        json={"file": {"display_name": display}})
    if start.status_code >= 400:
        raise_api_error(start)
    url = start.headers.get("x-goog-upload-url")
    if not url:
        raise HTTPException(502, "Upload URL nahi mila.")
    up = client.post(url, headers={"Content-Length": str(len(data)), "X-Goog-Upload-Offset": "0",
                                   "X-Goog-Upload-Command": "upload, finalize"}, content=data)
    if up.status_code >= 400:
        raise_api_error(up)
    f = up.json().get("file", up.json())
    # wait while processing
    for _ in range(60):
        state = str(f.get("state", "ACTIVE")).upper()
        if state not in ("PROCESSING", "STATE_PROCESSING"):
            break
        time.sleep(2)
        g = client.get(f"{API_ROOT}/{f['name']}", headers=headers())
        if g.status_code < 400:
            f = g.json()
    if "FAILED" in str(f.get("state", "")).upper():
        raise HTTPException(502, "Google ne file process nahi ki.")
    return f




@app.post("/api/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form(""),
    diarize: bool = Form(False),
    filename: str = Form(""),
    script_out: str = Form("original"),
):
    data = await file.read()
    if len(data) < 1000:
        raise HTTPException(400, "File khaali hai.")
    out_script = sub_script(script_out)
    mime = file.content_type or "audio/mpeg"

    def run():
        with httpx.Client(timeout=600) as c:
            f = upload_file(c, data, mime, file.filename or "audio")
            mode = {"type": "verbatim", "timestamp_granularities": ["word"]}
            if diarize:
                mode["diarization_mode"] = "speaker"
            cfg = {"mode": mode}
            if language:
                cfg["language_codes"] = [language]
            body = {"model": TRANSCRIBE_MODEL,
                    "input": [{"type": "audio", "uri": f.get("uri"), "mime_type": f.get("mimeType") or f.get("mime_type") or mime}],
                    "generation_config": {"transcription_config": cfg}}
            return gemini_request(c, "POST", f"{API_ROOT}/interactions", json=body).json()

    result = await asyncio.to_thread(run)
    words = []
    collect_words(result, words)
    words = [w for w in words if w["text"]]
    text = result.get("output_text") or collect_text(result)
    cues = words_to_cues(words) if words else []
    if not cues and text.strip():  # no word timing: keep plain text as one cue so it can still be converted
        cues = [(0.0, 0.0, line) for line in text.splitlines() if line.strip()]
    base = new_base(filename or Path(file.filename or "").stem, "subtitles")
    (RAW_DIR / f"{base}.json").write_text(json.dumps({
        "kind": "transcript", "created": datetime.now().isoformat(timespec="seconds"),
        "title": (file.filename or base)[:70], "cues": cues, "has_timing": bool(words),
        "duration": round(cues[-1][1], 2) if cues else 0, "script": out_script}, ensure_ascii=False), encoding="utf-8")
    out = await asyncio.to_thread(render, base, None, out_script)
    out.update({"words": len(words), "has_timing": bool(words), "text": "\n".join(c["text"] for c in out["cues"])})
    out["srt_text"] = (OUTPUT_DIR / f"{base}.srt").read_text(encoding="utf-8") if words else ""
    return out


@app.post("/api/subtitles/upload")
async def subtitles_upload(file: UploadFile = File(...), script_out: str = Form("original")):
    """Bring an existing .srt (e.g. from Filmora or older outputs) and convert its script."""
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    cues = parse_srt(raw)
    if not cues:
        raise HTTPException(400, "Yeh valid SRT file nahi lagti.")
    base = new_base(Path(file.filename or "subtitles").stem, "subtitles")
    (RAW_DIR / f"{base}.json").write_text(json.dumps({
        "kind": "transcript", "created": datetime.now().isoformat(timespec="seconds"),
        "title": (file.filename or base)[:70], "cues": cues, "has_timing": True,
        "duration": round(cues[-1][1], 2), "script": "original"}, ensure_ascii=False), encoding="utf-8")
    out = await asyncio.to_thread(render, base, None, sub_script(script_out))
    out["srt_text"] = (OUTPUT_DIR / f"{base}.srt").read_text(encoding="utf-8")
    return out


# ======================= Live voice agent =======================

URL_RE = re.compile(r"https?://[^\s<>\"']+")


def html_to_text(html: str) -> tuple[str, str]:
    import html as htmllib
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    body = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", htmllib.unescape(body)).strip()
    return (htmllib.unescape(title.group(1)).strip() if title else ""), body


async def fetch_link(url: str) -> str:
    """Read a web page for the agent (title + main text, trimmed)."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (AK Voice Studio agent)"}) as c:
            r = await c.get(url)
        ctype = r.headers.get("content-type", "")
        if "html" in ctype or not ctype:
            title, text = html_to_text(r.text[:3_000_000])
        elif ctype.startswith("text/") or "json" in ctype:
            title, text = "", r.text
        else:
            return f"[Link {url}: yeh page text nahi hai ({ctype}), is liye parha nahi ja saka.]"
        return f"[Link: {url}]\nTitle: {title}\nContent: {text[:12000]}"
    except Exception as e:
        return f"[Link {url} khul nahi saka: {type(e).__name__}]"


def file_to_message(name: str, mime: str, data: bytes):
    """Attachment -> (text for the agent, image bytes or None, image mime, label)."""
    low = name.lower()
    if mime.startswith("image/"):
        return (f"[User ne chat mein ek TASVEER bheji hai: '{name}'. Woh tasveer abhi tumhe video frame ke taur par mili hai. "
                f"Use dekh kar batao us mein kya hai aur apne andaaz mein us par baat karo.]"), data, mime, "tasveer"
    if mime == "application/pdf" or low.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:40])
        except ImportError:
            raise HTTPException(400, "PDF parhne ke liye pypdf chahiye — start.bat dobara chalayein (woh install kar dega).")
        except Exception as e:
            raise HTTPException(400, f"PDF khul nahi saki ({type(e).__name__}). Doosri file ya uski tasveer bhejein.")
        if not text.strip():
            raise HTTPException(400, "Is PDF mein parhne layak text nahi mila (shayad scan hai). Uski tasveer bhejein.")
        return f"[User ne chat mein PDF file bheji hai: '{name}'. Us ka poora text yeh hai:]\n{text[:20000]}\n[PDF khatam. Ab is par baat karo.]", None, None, "PDF"
    if mime.startswith("text/") or "subrip" in mime or "json" in mime or low.endswith((".txt", ".md", ".csv", ".json", ".srt", ".vtt", ".html", ".py", ".js")):
        text = data.decode("utf-8-sig", errors="replace")
        return f"[User ne chat mein file bheji hai: '{name}'. Us ka poora content yeh hai:]\n{text[:20000]}\n[File khatam. Ab is par baat karo.]", None, None, "file"
    raise HTTPException(400, f"'{name}' is qisam ki file abhi support nahi. Tasveer, PDF ya text file bhejein.")


@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    """Browser <-> this server <-> Gemini Live API.
    Browser sends: first a JSON frame {voice, system, greet}; then binary PCM16 16kHz mic chunks and
    JSON frames {type: "text", text} or {type: "file", name, mime, data(base64)}.
    Server sends: binary PCM16 24kHz audio, and JSON frames for transcripts/events."""
    import contextlib
    await ws.accept()
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        await ws.send_text(json.dumps({"type": "error", "message": "google-genai package install nahi. start.bat dobara chalayein."}))
        await ws.close(); return
    try:
        cfg_msg = json.loads(await ws.receive_text())
        base = {
            "response_modalities": ["AUDIO"],
            "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": cfg_msg.get("voice") or "Charon"}}},
            "system_instruction": cfg_msg.get("system") or "You are a helpful assistant.",
            "input_audio_transcription": {},
            "output_audio_transcription": {},
        }
        # Richest setup first; fall back if this model / key does not support a feature.
        attempts = [
            ("emotions + Google search", {"api_version": "v1alpha"}, {**base, "enable_affective_dialog": True, "tools": [{"google_search": {}}]}),
            ("Google search", None, {**base, "tools": [{"google_search": {}}]}),
            ("basic", None, base),
        ]
        stack = contextlib.AsyncExitStack()

        async def connect(model_name):
            last = None
            for label, http_options, config in attempts:
                try:
                    client = genai.Client(api_key=api_key(), **({"http_options": http_options} if http_options else {}))
                    return await stack.enter_async_context(client.aio.live.connect(model=model_name, config=config)), label
                except Exception as e:  # feature not supported -> try simpler setup
                    last = e
            raise last

        # Model order: the one that worked last time, then the one asked for / set in .env, then the models
        # the Google catalog says support Live for this key. A 404 on a model just moves on to the next one.
        first = [m for m in (cfg_msg.get("model"), _LIVE_MODEL_CACHE.get("ok"), LIVE_MODEL) if m]
        queue = list(dict.fromkeys(first))
        tried, session, mode, model, last_err, catalog_done = [], None, "", "", None, False
        while session is None:
            if not queue:
                if catalog_done:
                    break
                catalog_done = True
                try:
                    found = await asyncio.to_thread(_live_models_sync)
                except Exception as e:
                    log_error("agent live catalog", e)
                    found = []
                queue = [m for m in (found[:4] or [LIVE_FALLBACK_MODEL]) if m not in tried]
                if not found:
                    last_err = last_err or RuntimeError("Google catalog mein Live model nahi mila.")
                continue
            name = queue.pop(0)
            tried.append(name)
            try:
                session, mode = await connect(name)
                model = name
                _LIVE_MODEL_CACHE["ok"] = name
            except Exception as e:
                last_err = e
                if _LIVE_MODEL_CACHE.get("ok") == name:
                    _LIVE_MODEL_CACHE.pop("ok", None)
                if not _model_unavailable(e):  # bad key / quota / network: another model will not fix it
                    break
        if session is None:
            log_error("agent", last_err)
            message = ("Voice Agent connect nahi hua. Models try kiye: " + ", ".join(tried or ["-"]) +
                       f". Aakhri error: {type(last_err).__name__}: {str(last_err)[:300]}.")
            if tried and _model_unavailable(last_err):
                message += (" Is API key par koi chalne wala Live model nahi mila. /api/live-models kholkar dekhein ke key ko kaunse "
                            "models milte hain, aur .env mein LIVE_MODEL=<naam> likhein; ya Live-enabled key use karein.")
            await ws.send_text(json.dumps({"type": "error", "message": message}))
            await ws.close()
            return
        async with stack:
            await ws.send_text(json.dumps({"type": "ready", "mode": mode, "model": model}))
            async def send_to_agent(text: str, image: bytes | None = None, image_mime: str = "image/jpeg"):
                """Live audio models only accept new user content mid-call via realtime input
                (send_client_content is ignored there), so typed text, files and images go this way."""
                try:
                    if image:
                        await session.send_realtime_input(video=types.Blob(data=image, mime_type=image_mime))
                    await session.send_realtime_input(text=text)
                except TypeError:  # older google-genai without text= / video= support
                    parts = ([types.Part(inline_data=types.Blob(data=image, mime_type=image_mime))] if image else []) + [types.Part(text=text)]
                    await session.send_client_content(turns=types.Content(role="user", parts=parts), turn_complete=True)

            if cfg_msg.get("greet"):
                greet = cfg_msg["greet"] if isinstance(cfg_msg["greet"], str) else \
                    "(Call abhi connect hui hai. Apne andaaz mein garam-joshi se salaam karo aur baat shuru karo.)"
                await send_to_agent(greet[:500])

            async def handle_json(data):
                kind = data.get("type")
                if kind == "text":
                    text = str(data.get("text", ""))[:4000]
                    links = URL_RE.findall(text)[:3]
                    message = f"[User ne chat mein TYPE kiya hai:] {text}"
                    if links:
                        await ws.send_text(json.dumps({"type": "notice", "message": "Link parh raha hoon…"}))
                        pages = await asyncio.gather(*(fetch_link(u) for u in links))
                        message += "\n\n" + "\n\n".join(pages) + "\n[User ke bheje hue link(s) ka content upar hai. Is par baat karo.]"
                    await send_to_agent(message)
                elif kind == "file":
                    raw = base64.b64decode(data.get("data", ""))
                    if len(raw) > 15 * 1024 * 1024:
                        raise HTTPException(400, "File 15 MB se badi hai.")
                    name, mime = str(data.get("name", "file")), str(data.get("mime", ""))
                    text, image, image_mime, what = file_to_message(name, mime, raw)
                    if data.get("caption"):
                        text += "\n[User ka sawal is ke saath:] " + str(data["caption"])[:2000]
                    await send_to_agent(text, image, image_mime or "image/jpeg")
                    await ws.send_text(json.dumps({"type": "notice", "message": f"{what.capitalize()} '{name}' agent ko mil gayi."}))

            async def from_browser():
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        raise WebSocketDisconnect()
                    if msg.get("bytes"):
                        await session.send_realtime_input(
                            audio=types.Blob(data=msg["bytes"], mime_type="audio/pcm;rate=16000"))
                    elif msg.get("text"):
                        try:
                            await handle_json(json.loads(msg["text"]))
                        except HTTPException as e:
                            await ws.send_text(json.dumps({"type": "notice", "message": str(e.detail), "error": True}))
                        except Exception as e:  # a bad file / message must never end the call
                            log_error("agent message", e)
                            await ws.send_text(json.dumps({"type": "notice", "message": f"Yeh bheja nahi ja saka ({type(e).__name__}: {str(e)[:150]})", "error": True}))

            async def from_gemini():
                while True:
                    async for resp in session.receive():
                        sc = getattr(resp, "server_content", None)
                        if not sc:
                            continue
                        if getattr(sc, "interrupted", False):
                            await ws.send_text(json.dumps({"type": "interrupted"}))
                        it = getattr(sc, "input_transcription", None)
                        if it and getattr(it, "text", None):
                            await ws.send_text(json.dumps({"type": "user", "text": it.text}))
                        ot = getattr(sc, "output_transcription", None)
                        if ot and getattr(ot, "text", None):
                            await ws.send_text(json.dumps({"type": "agent", "text": ot.text}))
                        mt = getattr(sc, "model_turn", None)
                        for part in (getattr(mt, "parts", None) or []):
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                await ws.send_bytes(inline.data)
                        if getattr(sc, "turn_complete", False):
                            await ws.send_text(json.dumps({"type": "turn_complete"}))

            tasks = [asyncio.create_task(from_browser()), asyncio.create_task(from_gemini())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception() and not isinstance(t.exception(), WebSocketDisconnect):
                    raise t.exception()
    except WebSocketDisconnect:
        pass
    except Exception as e:  # report to browser
        log_error("agent", e)
        try:
            await ws.send_text(json.dumps({"type": "error", "message": f"{type(e).__name__}: {str(e)[:500]}"}))
            await ws.close()
        except Exception:
            pass


# ======================= misc =======================

@app.get("/api/estimate")
def estimate(chars: int, chunk_chars: int = CONSISTENCY_CHUNK_CHARS):
    limit = chunk_limit(chunk_chars)
    n = 1 if limit == 0 else max(1, -(-chars // limit))
    return {"chunks": n, "approx_seconds": int(n * 12)}


@app.get("/outputs/{name:path}")
def get_output(name: str):
    path = (OUTPUT_DIR / name).resolve()
    if OUTPUT_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "File nahi mili.")
    media = {"wav": "audio/wav", "srt": "application/x-subrip; charset=utf-8", "txt": "text/plain; charset=utf-8"}
    return FileResponse(path, media_type=media.get(path.suffix[1:], "application/octet-stream"), filename=path.name,
                        headers={"Cache-Control": "no-store"})


@app.get("/api/live-models")
def live_models_endpoint():
    """Diagnosis: which Live (bidiGenerateContent) models does Google offer to the active key?"""
    with httpx.Client(timeout=60) as client:
        return {"configured": LIVE_MODEL or None, "last_working": _LIVE_MODEL_CACHE.get("ok"), "models": live_models(client)}


@app.get("/api/config")
def config():
    return {"version": 2, "has_key": bool(KEYS.public()["keys"]), "model": DEFAULT_MODEL, "known_models": KNOWN_TTS_MODELS,
            "chunk_chars": CONSISTENCY_CHUNK_CHARS, "delay": CHUNK_DELAY_SEC, "live_model": LIVE_MODEL or "auto",
            "scripts": SCRIPTS}


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")

if __name__ == "__main__":
    import socket
    import uvicorn
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8000)) == 0:
            print("\n  Port 8000 par pehle se AK Voice Studio (shayad purana version) chal raha hai.")
            print("  Pehle saari purani AK Voice Studio / cmd windows band karein, phir start.bat dobara chalayein.\n")
            raise SystemExit(1)
    print("\n  AK Voice Studio chal raha hai:  http://127.0.0.1:8000\n")
    uvicorn.run(app, host="127.0.0.1", port=8000)
