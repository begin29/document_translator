PROMPT_VERSION = "v1"

SYSTEM_TEMPLATE = """You translate document blocks from {source} to {target}.
Return JSON only: {{"blocks":[{{"n":1,"text":"..."}}]}}.
Use the same n values you were given. Do not add or drop blocks.
Keep numbers, URLs, and glossary terms exactly as specified.
Glossary:
{glossary}
Document summary:
{summary}
"""


def system_prompt(source: str, target: str, glossary: list[dict] | None, summary: str | None) -> str:
    lines = []
    for item in glossary or []:
        src = item.get("source") or item.get("term") or ""
        tgt = item.get("target") or ""
        if src:
            lines.append(f"- {src} -> {tgt or src}")
    glossary_text = "\n".join(lines) if lines else "(none)"
    return SYSTEM_TEMPLATE.format(
        source=source or "auto",
        target=target,
        glossary=glossary_text,
        summary=summary or "(not available)",
    )


def user_prompt(pairs: list[tuple[int, str]]) -> str:
    return "\n".join(f"[{number}] {text}" for number, text in pairs)


OCR_PROMPT = """Transcribe this page verbatim into JSON.
{"blocks":[{"kind":"paragraph","text":"..."}]}
kind is one of heading, paragraph, list_item, table_cell, caption.
Do not translate. Do not guess. Mark unreadable spans as [illegible].
"""

CLASSIFY_PROMPT = """Classify this page image. Reply JSON only:
{"kind":"document|photo_of_document|non_document","legibility":"good|partial|poor","layout":"prose|table|form|mixed","handwriting":false,"orientation_deg":0,"language":"en","image_contains_text":false}
"""

SUMMARY_PROMPT = "Summarize the following document excerpt in one short paragraph in the source language. Do not translate."
