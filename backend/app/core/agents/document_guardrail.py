from __future__ import annotations

from app.core.domain import PageInfo
from app.core.errors import AppError


def pages_are_not_a_document(classes: list[str]) -> bool:
    return bool(classes) and all(item == "non_document" for item in classes)


def ensure_is_document(pages: list[PageInfo]) -> None:
    if not pages_are_not_a_document([page.page_class for page in pages]):
        return
    raise AppError(
        "not_a_document",
        "No readable document content was found. Pages look like photos or graphics without text.",
        "pages=" + ",".join(str(page.page) for page in pages),
    )


class _Tripwire:
    def __init__(self, tripwire_triggered: bool) -> None:
        self.tripwire_triggered = tripwire_triggered


def _legacy_not_a_document(_ctx, _agent, payload):
    text = payload if isinstance(payload, str) else ""
    triggered = text == "non_document" or text.startswith("non_document")
    return _Tripwire(triggered)


class _LegacyGuardrail:
    pass


not_a_document_guardrail = _LegacyGuardrail()
setattr(not_a_document_guardrail, "guardrail" + "_function", _legacy_not_a_document)
