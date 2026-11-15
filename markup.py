"""Make free-form scripts (ChatGPT / YouTube style) speakable.

- Stage directions like [FADE IN], [MUSIC: suspense], [SFX: whoosh], (cut to) are removed.
- Sound cues in brackets become Gemini tags: [laughs] -> <laugh>, [short pause] -> <short pause>.
- An emotion in brackets in the middle of a line starts a new piece with that style:
  "Phir bhi... [serious] chatbot kaafi nahi" -> ("Phir bhi...", old style), ("chatbot kaafi nahi", "serious").
"""
import re

STAGE = re.compile(
    r"\[\s*(?:fade|music|sfx|sound|sounds|bgm|background|scene|cut|transition|intro|outro|title|camera|visual|"
    r"on[- ]?screen|text on screen|b-?roll|zoom|pan|beat|silence|end|start|ambience|ambient|footsteps|door|phone)\b[^\]]*\]",
    re.I)
PAREN_STAGE = re.compile(r"\(\s*(?:fade|music|sfx|sound|cut|scene|transition|bgm)[^)]*\)", re.I)

SOUND_TAGS = {
    "laugh": "<laugh>", "laughs": "<laugh>", "laughing": "<laugh>", "laughter": "<laughter>",
    "chuckle": "<chuckle>", "chuckles": "<chuckle>", "giggle": "<giggle>", "giggles": "<giggle>",
    "sigh": "<sigh>", "sighs": "<sigh>", "gasp": "<gasp>", "gasps": "<gasp>", "cough": "<cough>", "coughs": "<cough>",
    "breath": "<breath>", "breathes": "<breath>", "inhales": "<breath>", "exhales": "<exhales>",
    "pause": "<short pause>", "short pause": "<short pause>", "long pause": "<long pause>", "beat": "<short pause>",
    "whisper": "<whispers>", "whispers": "<whispers>", "whispering": "<whispering>",
    "sniff": "<snort>", "groan": "<groan>", "groans": "<groan>", "yawn": "<yawn>", "yawns": "<yawn>",
    "cry": "<cry>", "cries": "<cry>", "sob": "<sob>", "sobs": "<sob>", "cheer": "<cheer>", "cheers": "<cheer>",
    "clears throat": "<throat-clearing>", "throat clearing": "<throat-clearing>", "scream": "<scream>", "shouts": "<shout>",
}
BRACKET = re.compile(r"\[([^\[\]\n]{1,80})\]")


def clean_line(text: str) -> str:
    text = STAGE.sub(" ", text)
    text = PAREN_STAGE.sub(" ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def split_styles(text: str, style: str) -> list:
    """[(text, style)] after converting sound cues and splitting at inline emotions."""
    text = clean_line(text)
    pieces, cur_style, last = [], style, 0
    buf = ""
    for m in BRACKET.finditer(text):
        word = m.group(1).strip()
        key = word.lower().rstrip(".")
        buf += text[last:m.start()]
        last = m.end()
        if key in SOUND_TAGS:
            buf += " " + SOUND_TAGS[key] + " "
        else:  # an emotion / delivery note in the middle of the line
            if buf.strip():
                pieces.append((buf, cur_style))
            buf, cur_style = "", word
    buf += text[last:]
    if buf.strip():
        pieces.append((buf, cur_style))
    out, pending = [], ""
    for t, s in pieces:
        t = re.sub(r"\s+", " ", t).strip()
        if re.sub(r"<[^<>]{1,40}>", "", t).strip(" .,!?…-—"):
            out.append(((pending + " " + t).strip(), (s or "").strip())); pending = ""
        elif out:  # only a sound tag: attach to previous piece
            out[-1] = (out[-1][0] + " " + t, out[-1][1])
        else:
            pending += " " + t
    return out
