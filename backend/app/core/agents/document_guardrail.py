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
