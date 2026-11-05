from __future__ import annotations

EXPANSION: dict[tuple[str, str], float] = {
    ("en", "de"): 1.35,
    ("en", "fr"): 1.20,
    ("en", "es"): 1.25,
    ("en", "it"): 1.20,
    ("en", "pt"): 1.20,
    ("en", "nl"): 1.20,
    ("en", "pl"): 1.20,
    ("en", "uk"): 1.15,
    ("en", "ru"): 1.15,
    ("en", "ja"): 1.60,
    ("en", "zh"): 0.80,
    ("en", "ko"): 1.10,
    ("de", "en"): 0.85,
    ("fr", "en"): 0.90,
    ("es", "en"): 0.90,
    ("ja", "en"): 1.30,
    ("zh", "en"): 1.40,
}

DEFAULT_EXPANSION = 1.3

LANGUAGES: list[dict[str, str]] = [
    {"code": "en", "name": "English"},
    {"code": "de", "name": "German"},
    {"code": "fr", "name": "French"},
    {"code": "es", "name": "Spanish"},
    {"code": "it", "name": "Italian"},
    {"code": "pt", "name": "Portuguese"},
    {"code": "nl", "name": "Dutch"},
    {"code": "pl", "name": "Polish"},
    {"code": "uk", "name": "Ukrainian"},
    {"code": "ru", "name": "Russian"},
    {"code": "ja", "name": "Japanese"},
    {"code": "zh", "name": "Chinese"},
    {"code": "ko", "name": "Korean"},
]

_CODES = {item["code"] for item in LANGUAGES}


def expansion_ratio(source: str | None, target: str | None) -> float:
    src = (source or "en").lower()
    tgt = (target or "en").lower()
    if src == tgt:
        return 1.0
    return EXPANSION.get((src, tgt), DEFAULT_EXPANSION)


def known_language(code: str | None) -> bool:
    return bool(code) and code.lower() in _CODES
