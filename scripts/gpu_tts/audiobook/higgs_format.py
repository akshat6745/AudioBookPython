"""Turn emotion-tagger output into Higgs v3 input text (tag syntax from the model card's PROMPTING.md).

Sentence-level tags (emotion, style, speed/pitch/expressive) go at a sentence start; sfx goes inline, followed
directly by its onomatopoeia. Anything outside the catalog is read aloud literally, so validate() gates every chunk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Full Higgs v3 catalog (PROMPTING.md, 43 tags).
CATALOG = {
    "emotion": {"affection", "amusement", "anger", "arousal", "awe", "bitterness", "confusion", "contemplation",
                "contentment", "determination", "disgust", "elation", "enthusiasm", "fear", "helplessness",
                "longing", "pride", "relief", "sadness", "shame", "surprise"},
    "prosody": {"speed_very_slow", "speed_slow", "speed_fast", "speed_very_fast", "pitch_low", "pitch_high",
                "expressive_high", "expressive_low", "pause", "long_pause"},
    "style": {"singing", "shouting", "whispering"},
    "sfx": {"cough", "laughter", "crying", "screaming", "burping", "humming", "sigh", "sniff", "sneeze"},
}
INLINE_PROSODY = {"pause", "long_pause"}

# Interjections that already voice the sound; otherwise the canonical one is inserted.
ONOMATOPOEIA = {
    "laughter": (r"\b(?:[Aa]?[Hh]a(?:ha)+|[Hh]e(?:he)+|[Hh]eh)\b", "Haha,"),
    "sigh": (r"\b(?:[Hh]aa+h|[Pp]hew)\b", "Haah,"),
    "cough": (r"\b(?:[Aa]hem|[Kk]ough|[Cc]ough cough)\b", "Ahem,"),
    "sniff": (r"\b[Ss]niff\b", "Sniff,"),
    "crying": (r"\b[Ss]ob\b", "Sob,"),
    "screaming": (r"\b(?:[Aa]a+h+|[Aa]rgh+)\b", "Aaah!"),
}

TAG_RE = re.compile(r"<\|([a-z]+):([a-z_]+)\|>")
INLINE_META_RE = re.compile(r"<\|[^>]+?\|>")
SPAN_TAG_RE = re.compile(r"<\|(voice:(?:narrator|dialogue)\|[^>]+?)\|>")
SENT_SPLIT = re.compile(r"(?<=[.!?…])([\"”’']*)\s+(?=[\"“‘'(\[]?[A-Z0-9])")
QUOTE_OPEN = ("“", '"')
SEPARATOR = re.compile(r"^[\W_]*$")  # "* * *", "---", "…" and similar scene breaks
WATERMARK = re.compile(r"^\W*(?:[A-Za-z0-9-]+\.)+(?:me|com|net|org|io|co|info)\W*$")  # "ReadNovelFull.me😉"
BOILERPLATE = re.compile(
    r"\s*([^S]*?website on Google to access chapters of novels early and in the highest quality[^.]*\.)\s*", re.I)


@dataclass(frozen=True)
class Cuts:
    """Minimum tagger confidence per head; below it the paragraph gets no tag of that kind."""
    emotion: float = 0.8
    style: float = 0.9
    sfx: float = 0.7


class TagError(ValueError):
    pass


def clean(text: str) -> str:
    """Same clean-up as app/api/tts.py, and strip tag delimiters so book text can never inject a tag."""
    text = text.replace("<|", "").replace("|>", "")
    text = re.sub(r"\*\s*\*\s*\*", "Asterisk Asterisk Asterisk", text)
    text = BOILERPLATE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def sentences(text: str) -> list[str]:
    """Split after . ! ? … (keeping closing quotes with their sentence)."""
    parts, last = [], 0
    for m in SENT_SPLIT.finditer(text):
        parts.append(text[last:m.start()] + m.group(1))
        last = m.end()
    parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]


def speech_flags(sents: list[str]) -> list[bool]:
    """True for sentences that are (partly) inside quotation marks, including middle sentences of a long quote."""
    flags, inside = [], False
    for s in sents:
        flags.append(inside or any(q in s for q in QUOTE_OPEN))
        for ch in s:
            if ch == "“":
                inside = True
            elif ch == "”":
                inside = False
            elif ch == '"':
                inside = not inside
    return flags


def format_paragraph(text: str, tag: dict, cuts: Cuts = Cuts(), mode: str = "every") -> list[str]:
    """Tagged sentences for one paragraph. mode: 'first' = emotion on the first sentence only, 'every' = each one."""
    if SEPARATOR.match(text):
        return []  # scene break: render.py leaves a longer pause instead of reading it out
    sents = sentences(clean(text))
    if not sents:
        return []
    emotion = tag.get("emotion", "neutral")
    emotion = emotion if emotion != "neutral" and tag.get("confidence", 0) >= cuts.emotion else None
    style = tag.get("style", "none")
    style = style if style != "none" and tag.get("style_conf", 0) >= cuts.style else None
    sfx = tag.get("sfx", "none")
    sfx = sfx if sfx != "none" and tag.get("sfx_conf", 0) >= cuts.sfx else None

    spoken = [i for i, f in enumerate(speech_flags(sents)) if f]
    style_at = set(spoken or range(len(sents))) if style else set()  # a speech verb describes the quoted words
    out = []
    for i, s in enumerate(sents):
        lead = ""
        if emotion and (mode == "every" or i == 0):
            lead += f"<|emotion:{emotion}|>"
        if i in style_at:
            lead += f"<|style:{style}|>"
        out.append(lead + s)

    if sfx and sfx in ONOMATOPOEIA:
        pattern, canon = ONOMATOPOEIA[sfx]
        hit = next(((i, m) for i, s in enumerate(out) if (m := re.search(pattern, s))), None)
        if hit:
            i, m = hit
            out[i] = f"{out[i][:m.start()]}<|sfx:{sfx}|>{out[i][m.start():]}"
        elif spoken:  # in narration the sound is already described ("he sighed"); inserting one garbles it
            i = spoken[0]
            lead = re.match(r"(?:<\|[a-z]+:[a-z_]+\|>)*", out[i]).group(0)
            out[i] = f"{lead}<|sfx:{sfx}|>{canon} {out[i][len(lead):]}"
    return out


def is_watermark(paragraph: str) -> bool:
    return bool(WATERMARK.match(paragraph.strip()))


def speech_spans(text: str) -> list[tuple[bool, str]]:
    """Split cleaned text into (is_dialogue, span) at quotation marks; quote marks stay with the dialogue."""
    spans, cur, inside = [], "", False
    for ch in text:
        opening = ch == "“" or (ch == '"' and not inside)
        closing = ch == "”" or (ch == '"' and inside)
        if opening:
            spans.append((inside, cur))
            cur, inside = ch, True
        elif closing:
            spans.append((True, cur + ch))
            cur, inside = "", False
        else:
            cur += ch
    spans.append((inside, cur))
    merged: list[tuple[bool, str]] = []
    for d, s in ((d, s.strip()) for d, s in spans if not SEPARATOR.match(s)):
        d = d and not is_scare_quote(s)
        if merged and not d and not merged[-1][0]:
            merged[-1] = (False, f"{merged[-1][1]} {s}".strip())
        else:
            merged.append((d, s))
    return [(d, s) for d, s in merged if s]


def is_scare_quote(span: str) -> bool:
    """A short quoted phrase without closing punctuation ('his “summoning” wasn’t') is part of the narration.

    Splitting it out cut the sentence into fragments read by two voices, and Higgs garbles short fragments.
    """
    inner = span.strip("“”\"").strip()
    return len(inner.split()) <= 3 and not re.search(r"[.,!?…:;—–-]$", inner)


def plain(text: str) -> str:
    return INLINE_META_RE.sub("", text)


def encode_span_tag(voice: str, tag: dict) -> str:
    """Format a combined inline span tag: <|voice:narrator|emotion:fear|conf:0.89|style:none|style_conf:0.99|sfx:none|sfx_conf:0.99|>."""
    emo = tag.get("emotion", "neutral")
    conf = round(float(tag.get("confidence", 0.0)), 3)
    style = tag.get("style", "none")
    style_conf = round(float(tag.get("style_conf", 0.0)), 3)
    sfx = tag.get("sfx", "none")
    sfx_conf = round(float(tag.get("sfx_conf", 0.0)), 3)
    return (
        f"<|voice:{voice}|emotion:{emo}|conf:{conf:.3f}"
        f"|style:{style}|style_conf:{style_conf:.3f}"
        f"|sfx:{sfx}|sfx_conf:{sfx_conf:.3f}|>"
    )


def parse_span_tag(header: str) -> tuple[str, dict]:
    """Parse 'voice:narrator|emotion:fear|conf:0.89|...' -> (voice, tag_dict)."""
    kv = dict(part.split(":", 1) for part in header.split("|") if ":" in part)
    voice = kv.get("voice", "narrator")
    tag = {
        "emotion": kv.get("emotion", "neutral"),
        "confidence": float(kv.get("conf", 0.0)),
        "style": kv.get("style", "none"),
        "style_conf": float(kv.get("style_conf", 0.0)),
        "sfx": kv.get("sfx", "none"),
        "sfx_conf": float(kv.get("sfx_conf", 0.0)),
    }
    return voice, tag


def paragraph_spans_for_tagging(paragraph: str) -> list[tuple[str, str]]:
    """Split a raw paragraph into [('narrator'|'dialogue', raw_span_text_with_original_spacing), ...]."""
    raw = INLINE_META_RE.sub("", paragraph).strip()
    if not raw:
        return []
    if SEPARATOR.match(raw) or is_watermark(raw):
        return [("narrator", raw)]
    spans, cur, inside = [], "", False
    for ch in raw:
        opening = ch == "“" or (ch == '"' and not inside)
        closing = ch == "”" or (ch == '"' and inside)
        if opening:
            if cur:
                if SEPARATOR.match(cur) and spans:
                    prev_v, prev_s = spans[-1]
                    spans[-1] = (prev_v, prev_s + cur)
                elif not SEPARATOR.match(cur):
                    spans.append(("dialogue" if inside else "narrator", cur))
            cur, inside = ch, True
        elif closing:
            spans.append(("dialogue", cur + ch))
            cur, inside = "", False
        else:
            cur += ch
    if cur:
        if SEPARATOR.match(cur) and spans:
            prev_v, prev_s = spans[-1]
            spans[-1] = (prev_v, prev_s + cur)
        elif not SEPARATOR.match(cur):
            spans.append(("dialogue" if inside else "narrator", cur))
    return spans or [("narrator", raw)]


def annotate_paragraph(paragraph: str, span_tags: list[dict]) -> str:
    """Embed per-span <|voice:...|emotion:...|conf:...|style:...|sfx:...|> tags inline without altering text spacing."""
    spans = paragraph_spans_for_tagging(paragraph)
    if not spans or len(spans) != len(span_tags):
        return paragraph
    if SEPARATOR.match(spans[0][1]) or is_watermark(spans[0][1]):
        return spans[0][1]
    return "".join(f"{encode_span_tag(voice, tag)}{text}" for (voice, text), tag in zip(spans, span_tags))


def strip_inline_tags(paragraph: str) -> str:
    """Strip inline <|...|> tags while preserving 100% of the original characters and whitespace."""
    return INLINE_META_RE.sub("", paragraph).strip()


def parse_annotated_paragraph(paragraph: str) -> list[tuple[str, str, dict]]:
    """Extract [(voice, clean_span_text, tag_dict), ...] from an inline-tagged paragraph."""
    matches = list(SPAN_TAG_RE.finditer(paragraph))
    if not matches:
        return []
    out: list[tuple[str, str, dict]] = []
    for idx, m in enumerate(matches):
        voice, tag = parse_span_tag(m.group(1))
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(paragraph)
        span_text = paragraph[start:end].strip()
        if span_text:
            out.append((voice, span_text, tag))
    return out


def format_dual(text: str, tag: dict, cuts: Cuts = Cuts()) -> list[tuple[str, list[str]]]:
    """Two-voice version: [("narrator"|"dialogue", tagged sentences), ...] in reading order.

    If `text` already contains inline `<|voice:...|emotion:...>` span tags (or `tag` has a `"spans"` list),
    each narrator and dialogue span uses its own dedicated emotion/style/sfx tag!
    Otherwise falls back to paragraph-level `tag`.
    """
    inline_spans = parse_annotated_paragraph(text)
    if inline_spans:
        out, sfx_done = [], False
        for voice, span_text, span_tag in inline_spans:
            if SEPARATOR.match(span_text):
                continue
            t = dict(span_tag)
            if voice == "narrator":
                # Narrator spans keep their own emotion, while style/sfx apply to spoken dialogue
                t["style"] = "none"
                t["sfx"] = "none"
            elif sfx_done:
                t["sfx"] = "none"
            units = format_paragraph(span_text, t, cuts)
            sfx_done |= any("<|sfx:" in u for u in units)
            if units:
                out.append((voice, units))
        return out

    if "spans" in tag and isinstance(tag["spans"], list):
        out, sfx_done = [], False
        for sp in tag["spans"]:
            voice = sp.get("voice", "narrator")
            span_text = sp.get("text", "")
            if not span_text or SEPARATOR.match(span_text):
                continue
            t = dict(sp)
            if voice == "narrator":
                t["style"] = "none"
                t["sfx"] = "none"
            elif sfx_done:
                t["sfx"] = "none"
            units = format_paragraph(span_text, t, cuts)
            sfx_done |= any("<|sfx:" in u for u in units)
            if units:
                out.append((voice, units))
        return out

    if SEPARATOR.match(text):
        return []
    spans = speech_spans(clean(text))
    has_dialogue = any(d for d, _ in spans)
    out, sfx_done = [], False
    for is_dialogue, span in spans:
        t = tag if is_dialogue or not has_dialogue else {}
        if sfx_done:
            t = {**t, "sfx": "none"}
        units = format_paragraph(span, t, cuts)
        sfx_done |= any("<|sfx:" in u for u in units)
        if units:
            out.append(("dialogue" if is_dialogue else "narrator", units))
    return out


def validate(text: str) -> None:
    """Raise TagError unless every tag is in the catalog and placed the way PROMPTING.md requires."""
    if "<|" in TAG_RE.sub("", text) or "|>" in TAG_RE.sub("", text):
        raise TagError(f"malformed tag in: {text[:80]!r}")
    for m in TAG_RE.finditer(text):
        cat, name = m.groups()
        if name not in CATALOG.get(cat, ()):
            raise TagError(f"unknown tag <|{cat}:{name}|>")
        if cat == "sfx":
            nxt = text[m.end():m.end() + 1]
            if not nxt.isalpha():
                raise TagError(f"<|sfx:{name}|> must be followed directly by its onomatopoeia")
        elif not (cat == "prosody" and name in INLINE_PROSODY):
            before = TAG_RE.sub("", text[:m.start()]).rstrip()
            if before and not re.search(r"[.!?…][\"”’']*$", before):
                raise TagError(f"<|{cat}:{name}|> must start a sentence: {text[max(0, m.start() - 40):m.end()]!r}")


def chunks(units: list[str], max_chars: int = 500) -> list[str]:
    """Pack a paragraph's sentences into requests of <= max_chars, re-stating the emotion where a new one starts."""
    out, cur, lead = [], "", ""
    for u in units:
        m = re.match(r"<\|emotion:[a-z_]+\|>", u)
        lead = m.group(0) if m else lead
        if cur and len(TAG_RE.sub("", cur)) + len(TAG_RE.sub("", u)) + 1 > max_chars:
            out.append(cur)
            cur = u if u.startswith(lead) else lead + u
        else:
            cur = f"{cur} {u}".strip()
    if cur:
        out.append(cur)
    for c in out:
        validate(c)
    return out


