from __future__ import annotations

import re

from app.core.domain import normalize_text
from app.core.langdetect import detect_language, length_ratio_ok, script_matches_target
from app.core.languages import expansion_ratio

_LEADING_TAG = re.compile(r"^(?:\[[a-z]{2,8}\]\s*)+", re.IGNORECASE)


def _untag(text: str) -> str:
    return "\n".join(_LEADING_TAG.sub("", line).strip() for line in (text or "").splitlines()).strip()


def flags_for(source: str, target: str, source_lang: str | None, target_lang: str, glossary: list[dict] | None) -> list[str]:
    flags: list[str] = []
    visible = _untag(target)
    tagged = bool(_LEADING_TAG.match((target or "").lstrip()))
    if normalize_text(source) and normalize_text(source) == normalize_text(target):
        flags.append("untranslated")
    if source and visible and not length_ratio_ok(source, visible, expansion_ratio(source_lang, target_lang)):
        flags.append("length_ratio")
    if visible and not script_matches_target(visible, target_lang):
        flags.append("script")
    detected = None if tagged else detect_language(visible, fallback_en=False)
    if detected and target_lang and detected != target_lang.lower() and "script" not in flags:
        flags.append("script")
    for item in glossary or []:
        term = (item.get("source") or item.get("term") or "").strip()
        translated = (item.get("target") or "").strip()
        if term and term in source and translated and translated not in visible:
            flags.append("glossary")
            break
    return flags
