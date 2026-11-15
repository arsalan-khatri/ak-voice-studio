"""Subtitle helpers: cue building, speech-aware timing, SRT read/write, script conversion."""
import json
import re

STYLE_TAG = re.compile(r"^\[([^\]]{2,200})\]\s*", re.S)
RLM = "‏"  # right-to-left mark: keeps Urdu lines anchored right, punctuation on the correct side

SCRIPTS = {
    "original": "Original (jaisi script likhi)",
    "roman": "Roman (English letters)",
    "hindi": "Hindi (Devanagari)",
    "urdu": "Urdu (Nastaliq, right-to-left)",
}

SCRIPT_RULES = {
    "roman": ("Roman Urdu / Hinglish written in English (Latin) letters, the way Pakistani YouTubers write captions, "
              "e.g. 'Aaj hum seekhenge ke chatbot kaise kaam karta hai.' English words stay in normal English spelling "
              "(chatbot, AI, data). Use simple consistent spellings."),
    "hindi": ("Hindi in Devanagari script. English words are written in Devanagari as commonly done in Hindi captions "
              "(e.g. चैटबॉट, डेटा). Abbreviations like AI or RAG may stay in Latin letters."),
    "urdu": ("Urdu in Urdu/Nastaliq (Perso-Arabic) script. English words are written in Urdu script as commonly done "
             "(e.g. چیٹ بوٹ، ڈیٹا). Abbreviations like AI or RAG may stay in Latin letters. Use Urdu punctuation "
             "۔ ، ؟ instead of . , ?"),
}


# ---------------------------------------------------------------- text cleanup / splitting

def clean_for_subtitle(text: str) -> str:
    text = STYLE_TAG.sub("", text.strip())
    text = re.sub(r"<[^<>\n]{1,40}>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_caption_lines(text: str, max_chars: int = 42) -> list:
    """Split text into caption-sized pieces at sentence/comma/word boundaries."""
    out = []
    for sent in re.split(r"(?<=[.!?۔؟])\s+", text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= max_chars:
            out.append(sent); continue
        words = sent.split()
        n = -(-len(sent) // max_chars)
        target = len(sent) / n
        cur = ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > target * 1.15:
                out.append(cur); cur = w
            else:
                cur = f"{cur} {w}" if cur else w
        if cur:
            out.append(cur)
    return out


# ---------------------------------------------------------------- timing

def srt_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(round(t * 1000)), 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def parse_srt_time(s: str) -> float:
    h, m, rest = s.strip().split(":")
    sec, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def _speech_clock(regions, start, end):
    """Speech-only intervals clipped to [start, end]; falls back to the whole span."""
    spans = [(max(a, start), min(b, end)) for a, b in regions if b > start and a < end]
    spans = [(a, b) for a, b in spans if b - a > 0.02]
    return spans or [(start, end)]


def _at_speech_offset(spans, offset, after_pause=False):
    """Real time where `offset` seconds of speech have elapsed. A start time that lands exactly on a
    pause boundary (after_pause=True) goes to the start of the next speech span, not the end of the last."""
    for i, (a, b) in enumerate(spans):
        d = b - a
        if offset < d - 1e-6 or (offset <= d + 1e-6 and (not after_pause or i == len(spans) - 1)):
            return a + min(offset, d)
        offset -= d
    return spans[-1][1]


def timed_cues(segments, regions=None, max_chars: int = 42):
    """segments: [(text, start, end)] seconds. Returns [(start, end, text)].
    Inside each segment, caption time is spread by character count over *speech* time only, so
    pauses are not given to any caption (much better sync than spreading over the whole span)."""
    cues = []
    for text, start, end in segments:
        pieces = split_caption_lines(clean_for_subtitle(text), max_chars)
        if not pieces or end <= start:
            continue
        spans = _speech_clock(regions or [], start, end)
        total_speech = sum(b - a for a, b in spans)
        total_chars = sum(len(p) for p in pieces) or 1
        # speech-clock offsets where a real pause happens (end of each span except the last)
        pause_at, acc = [], 0.0
        for a, b in spans[:-1]:
            acc += b - a
            pause_at.append(acc)
        offsets = [0.0]
        done = 0
        for p in pieces:
            done += len(p)
            offsets.append(total_speech * done / total_chars)
        # snap inner caption boundaries to the nearest real pause (captions change when the speaker pauses)
        tol = min(0.8, max(0.25, total_speech / max(1, len(pieces)) * 0.45))
        for k in range(1, len(offsets) - 1):
            near = min(pause_at, key=lambda x: abs(x - offsets[k]), default=None)
            if near is not None and abs(near - offsets[k]) <= tol and offsets[k - 1] < near < offsets[k + 1]:
                offsets[k] = near
        for k, p in enumerate(pieces):
            o1, o2 = offsets[k], offsets[k + 1]
            a = _at_speech_offset(spans, o1, after_pause=True)
            b = _at_speech_offset(spans, o2)
            cues.append((a, max(b, a + 0.2), p))
    # no overlaps
    for i in range(len(cues) - 1):
        a, b, t = cues[i]
        if b > cues[i + 1][0]:
            cues[i] = (a, max(a + 0.05, cues[i + 1][0]), t)
    return cues


def words_to_cues(words: list, max_chars: int = 42, max_dur: float = 4.0):
    cues, cur = [], []

    def flush():
        nonlocal cur
        if cur:
            cues.append((cur[0]["start"], cur[-1]["end"], " ".join(w["text"] for w in cur).strip()))
            cur = []

    for w in words:
        if cur and (len(" ".join(x["text"] for x in cur)) + len(w["text"]) + 1 > max_chars
                    or w["end"] - cur[0]["start"] > max_dur
                    or w["start"] - cur[-1]["end"] > 0.7):
            flush()
        cur.append(w)
        if re.search(r"[.!?۔।؟]$", w["text"]):
            flush()
    flush()
    return cues


# ---------------------------------------------------------------- SRT io

def format_srt(cues, script: str = "original") -> str:
    out = []
    for i, (a, b, text) in enumerate(cues, 1):
        if script == "urdu":
            text = "\n".join(RLM + line.strip() + RLM for line in text.split("\n"))
        out.append(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n")
    return "\n".join(out)


def parse_srt(srt: str):
    cues = []
    for block in re.split(r"\n\s*\n", srt.replace("\r\n", "\n").strip()):
        lines = [l for l in block.split("\n")]
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        a, b = [x.strip() for x in lines[idx].split("-->")]
        text = "\n".join(lines[idx + 1:]).replace(RLM, "").strip()
        cues.append((parse_srt_time(a), parse_srt_time(b.split()[0]), text))
    return cues


# ---------------------------------------------------------------- script conversion (transliteration)

def convert_prompt(texts: list, script: str) -> str:
    return (
        "You convert subtitle lines from Urdu/Hindi/Hinglish speech into a different WRITING SCRIPT.\n"
        f"Target: {SCRIPT_RULES[script]}\n"
        "Rules:\n"
        "- Transliterate only. Do NOT translate, summarize, add or remove words. Keep the same meaning and word order.\n"
        "- Keep numbers, names and line breaks.\n"
        f"- Return a JSON array of exactly {len(texts)} strings, item i is the converted item i.\n\n"
        "Input JSON array:\n" + json.dumps(texts, ensure_ascii=False)
    )


def parse_converted(raw: str, expected: int):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw)
    data = json.loads(raw)
    if isinstance(data, dict):  # tolerate {"lines": [...]}
        data = next((v for v in data.values() if isinstance(v, list)), data)
    if not isinstance(data, list) or len(data) != expected or not all(isinstance(x, str) for x in data):
        raise ValueError(f"Model ne {expected} lines ki jagah galat format diya.")
    return [x.strip() for x in data]


def detect_script(text: str) -> str:
    """Rough guess of the script a text is written in."""
    dev = len(re.findall(r"[ऀ-ॿ]", text))
    arab = len(re.findall(r"[؀-ۿ]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    best = max((dev, "hindi"), (arab, "urdu"), (latin, "roman"))
    return best[1] if best[0] else "roman"


# ---------------------------------------------------------------- offline fallback (no API)
# Devanagari -> Urdu / Roman, used only when the Gemini conversion fails. Hindi and Urdu share
# sounds, so a letter map gives readable results (English loan-words are less exact than AI).

_CONS = {
    "क": ("ک", "k"), "ख": ("کھ", "kh"), "ग": ("گ", "g"), "घ": ("گھ", "gh"), "ङ": ("ن", "n"),
    "च": ("چ", "ch"), "छ": ("چھ", "chh"), "ज": ("ج", "j"), "झ": ("جھ", "jh"), "ञ": ("ن", "n"),
    "ट": ("ٹ", "t"), "ठ": ("ٹھ", "th"), "ड": ("ڈ", "d"), "ढ": ("ڈھ", "dh"), "ण": ("ن", "n"),
    "त": ("ت", "t"), "थ": ("تھ", "th"), "द": ("د", "d"), "ध": ("دھ", "dh"), "न": ("ن", "n"),
    "प": ("پ", "p"), "फ": ("پھ", "ph"), "ब": ("ب", "b"), "भ": ("بھ", "bh"), "म": ("م", "m"),
    "य": ("ی", "y"), "र": ("ر", "r"), "ल": ("ل", "l"), "व": ("و", "w"), "श": ("ش", "sh"),
    "ष": ("ش", "sh"), "स": ("س", "s"), "ह": ("ہ", "h"),
    "क़": ("ق", "q"), "ख़": ("خ", "kh"), "ग़": ("غ", "gh"), "ज़": ("ز", "z"), "फ़": ("ف", "f"),
    "ड़": ("ڑ", "r"), "ढ़": ("ڑھ", "rh"), "य़": ("ی", "y"),
}
_IND = {"अ": ("ا", "a"), "आ": ("آ", "aa"), "इ": ("ا", "i"), "ई": ("ای", "ee"), "उ": ("ا", "u"), "ऊ": ("او", "oo"),
        "ए": ("ای", "e"), "ऐ": ("اے", "ai"), "ओ": ("او", "o"), "औ": ("او", "au"), "ऑ": ("آ", "o"), "ऋ": ("رِ", "ri")}
_MAT = {"ा": ("ا", "aa"), "ि": ("", "i"), "ी": ("ی", "ee"), "ु": ("", "u"), "ू": ("و", "oo"), "े": ("ی", "e"),
        "ै": ("ی", "ai"), "ो": ("و", "o"), "ौ": ("و", "au"), "ॉ": ("ا", "o"), "ृ": ("ر", "ri")}
_PUNCT_UR = {"।": "۔", "?": "؟", ",": "،", ";": "؛"}
_NUKTA = "़"


def _translit_word(w: str, urdu: bool) -> str:
    k = 0 if urdu else 1
    out, i, n = [], 0, len(w)
    while i < n:
        ch = w[i]
        if i + 1 < n and w[i + 1] == _NUKTA and ch + _NUKTA in _CONS:
            ch = ch + _NUKTA; i += 1
        nxt = w[i + 1] if i + 1 < n else ""
        last = i + 1 >= n or (i + 2 >= n and nxt in "ंँ")
        if ch in _CONS:
            out.append(_CONS[ch][k])
            # inherent "a" for Roman: not before a matra/virama and not at word end
            if not urdu and nxt not in _MAT and nxt != "्" and i + 1 < n and nxt not in "ंँ":
                out.append("a")
        elif ch in _IND:
            v = _IND[ch][k]
            if urdu and i > 0:  # vowel after a vowel inside a word: hamza seat (ائی، آئے)
                v = {"इ": "ئ", "ई": "ئی", "ए": "ئے" if last else "ئی", "ऐ": "ئے", "उ": "ؤ", "ऊ": "ؤ", "ओ": "و", "औ": "و"}.get(ch, v)
            elif urdu and ch in "एऐ" and last:
                v = "اے"
            out.append(v)
        elif ch in _MAT:
            v = _MAT[ch][k]
            if urdu and ch in "ेै":
                at_end = i + 1 >= n
                nasal_end = i + 2 == n and nxt in "ंँ"
                v = "ے" if at_end else ("ی" if nasal_end else "ی")
            out.append(v)
        elif ch in "ंँ":
            out.append(("ں" if i + 1 >= n else "ن") if urdu else "n")
        elif ch == "ः":
            out.append("ہ" if urdu else "h")
        elif ch == "्":
            pass
        elif urdu and ch in _PUNCT_UR:
            out.append(_PUNCT_UR[ch])
        elif not urdu and ch == "।":
            out.append(".")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def offline_convert(text: str, script: str) -> str:
    """Devanagari -> 'urdu' or 'roman' without any API. Other text is returned unchanged."""
    if script not in ("urdu", "roman") or not re.search(r"[ऀ-ॿ]", text):
        return text
    urdu = script == "urdu"
    out = []
    for p in re.split(r"(\s+)", text):
        if p.isspace() or not p:
            out.append(p); continue
        m = re.match(r"^(.*?)([^\u0900-\u0963\u0966-\u097F]*)$", p)  # keep trailing punctuation apart
        core, tail = m.group(1), m.group(2)
        tail = "".join(_PUNCT_UR.get(c, c) if urdu else ("." if c == "।" else c) for c in tail)
        out.append(_translit_word(core, urdu) + tail)
    return "".join(out)
