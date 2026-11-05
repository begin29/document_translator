from __future__ import annotations

from app.core.domain import normalize_text
from app.core.langdetect import length_ratio_ok, script_matches_target
from app.core.languages import expansion_ratio


def flags_for(source: str, target: str, source_lang: str | None, target_lang: str, glossary: list[dict] | None) -> list[str]:
    flags: list[str] = []
    if normalize_text(source) and normalize_text(source) == normalize_text(target):
        flags.append("untranslated")
    if source and target and not length_ratio_ok(source, target, expansion_ratio(source_lang, target_lang)):
        flags.append("length_ratio")
    if target and not script_matches_target(target, target_lang):
        flags.append("script")
    for item in glossary or []:
        term = (item.get("source") or item.get("term") or "").strip()
        translated = (item.get("target") or "").strip()
        if term and term in source and translated and translated not in target:
            flags.append("glossary")
            break
    return flags
