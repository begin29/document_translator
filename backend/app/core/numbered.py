from __future__ import annotations

import json
import re

from app.core.domain import normalize_text


def parse_numbered_blocks(raw: str, expected: list[int]) -> tuple[dict[int, str], list[int], list[int]]:
    found: dict[int, str] = {}
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    payload = _json_blocks(text)
    if payload is None:
        payload = _line_blocks(text)
    for number, value in payload.items():
        found[number] = value.strip()
    missing = [n for n in expected if n not in found or not found[n]]
    extra = [n for n in found if n not in expected]
    return found, missing, extra


def usable_translation(text: str | None, source: str, target_lang: str | None = None) -> str | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    if normalize_text(cleaned) == normalize_text(source):
        return None
    if target_lang:
        from app.core.quality import flags_for

        reasons = flags_for(source, cleaned, None, target_lang, [])
        if "untranslated" in reasons or "script" in reasons:
            return None
    return cleaned


def _json_blocks(text: str) -> dict[int, str] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    rows = data.get("blocks") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None
    out: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = row.get("n", row.get("idx", row.get("id")))
        value = row.get("text", "")
        if number is None:
            continue
        out[int(number)] = str(value)
    return out


def _line_blocks(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    current: int | None = None
    buf: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*\[(\d+)\]\s*(.*)$", line)
        if match:
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current = int(match.group(1))
            buf = [match.group(2)]
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out
