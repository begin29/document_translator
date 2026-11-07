from __future__ import annotations

import re
from collections import Counter

_COMMON = {
    "en": {"the", "and", "of", "to", "in", "is", "for", "that", "with", "on", "this", "are"},
    "de": {"und", "der", "die", "das", "ist", "nicht", "ein", "eine", "mit", "auf", "für", "den"},
    "fr": {"les", "des", "une", "est", "dans", "pour", "que", "pas", "sur", "avec", "par"},
    "es": {"los", "las", "una", "del", "que", "por", "con", "para", "como", "más"},
    "uk": {
        "та",
        "і",
        "й",
        "не",
        "на",
        "що",
        "для",
        "від",
        "або",
        "як",
        "це",
        "але",
        "з",
        "у",
        "до",
        "ми",
        "ви",
        "він",
        "вона",
        "вони",
        "був",
        "була",
        "було",
        "буде",
        "також",
        "які",
        "який",
        "яка",
    },
    "ru": {
        "и",
        "что",
        "это",
        "как",
        "из",
        "он",
        "она",
        "они",
        "был",
        "была",
        "было",
        "будет",
        "также",
        "который",
        "которая",
    },
}


def detect_language(text: str, *, fallback_en: bool = True) -> str | None:
    sample = (text or "")[:8000]
    if not sample.strip():
        return None
    cjk = len(re.findall(r"[\u4e00-\u9fff]", sample))
    kana = len(re.findall(r"[\u3040-\u30ff]", sample))
    hangul = len(re.findall(r"[\uac00-\ud7af]", sample))
    cyr = len(re.findall(r"[\u0400-\u04ff]", sample))
    letters = len(re.findall(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff]", sample))
    if kana > 8:
        return "ja"
    if hangul > 8:
        return "ko"
    if cjk > 8 and kana < 3:
        return "zh"
    tokens = re.findall(r"\w+", sample.lower(), flags=re.UNICODE)
    if cyr > max(12, letters * 0.4):
        uk_letters = len(re.findall(r"[іїєґІЇЄҐ]", sample))
        ru_letters = len(re.findall(r"[ыэъёЫЭЪЁ]", sample))
        if uk_letters >= 2 and uk_letters > 2 * ru_letters:
            return "uk"
        if ru_letters >= 2 and ru_letters > 2 * uk_letters:
            return "ru"
        scores = {lang: sum(1 for t in tokens if t in words) for lang, words in _COMMON.items() if lang in {"uk", "ru"}}
        if scores["uk"] == scores["ru"]:
            return None
        return "uk" if scores["uk"] > scores["ru"] else "ru"
    scores = {lang: sum(1 for t in tokens if t in words) for lang, words in _COMMON.items() if lang not in {"uk", "ru"}}
    best = max(scores, key=scores.get)
    if scores[best] >= 2:
        return best
    if letters > 20 and fallback_en:
        return "en"
    return None


def script_matches_target(text: str, target: str) -> bool:
    target = (target or "").lower()
    if target in {"ja"}:
        return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text or ""))
    if target in {"zh"}:
        return bool(re.search(r"[\u4e00-\u9fff]", text or ""))
    if target in {"ko"}:
        return bool(re.search(r"[\uac00-\ud7af]", text or ""))
    if target in {"uk", "ru"}:
        return bool(re.search(r"[\u0400-\u04ff]", text or ""))
    return True


def length_ratio_ok(source: str, target: str, expansion: float) -> bool:
    src = max(len(source or ""), 1)
    tgt = len(target or "")
    ratio = tgt / src
    low = max(0.25, 0.35 * expansion)
    high = max(2.8, 2.2 * expansion)
    return low <= ratio <= high
