from __future__ import annotations

from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_DEJAVU = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
)
_MAC = (
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    "/System/Library/Fonts",
)

_FILES = {
    ("sans", False, False): ("DejaVuSans.ttf", "Arial.ttf", "Arial Unicode.ttf"),
    ("sans", True, False): ("DejaVuSans-Bold.ttf", "Arial Bold.ttf"),
    ("sans", False, True): ("DejaVuSans-Oblique.ttf", "Arial Italic.ttf"),
    ("sans", True, True): ("DejaVuSans-BoldOblique.ttf", "Arial Bold Italic.ttf"),
    ("serif", False, False): ("DejaVuSerif.ttf", "Times New Roman.ttf"),
    ("serif", True, False): ("DejaVuSerif-Bold.ttf", "Times New Roman Bold.ttf"),
    ("serif", False, True): ("DejaVuSerif-Italic.ttf", "Times New Roman Italic.ttf"),
    ("serif", True, True): ("DejaVuSerif-BoldItalic.ttf", "Times New Roman Bold Italic.ttf"),
    ("mono", False, False): ("DejaVuSansMono.ttf", "Courier New.ttf"),
    ("mono", True, False): ("DejaVuSansMono-Bold.ttf", "Courier New Bold.ttf"),
    ("mono", False, True): ("DejaVuSansMono-Oblique.ttf", "Courier New Italic.ttf"),
    ("mono", True, True): ("DejaVuSansMono-BoldOblique.ttf", "Courier New Bold Italic.ttf"),
}

_NAMES = {
    ("sans", False, False): "StarkSans",
    ("sans", True, False): "StarkSans-Bold",
    ("sans", False, True): "StarkSans-Oblique",
    ("sans", True, True): "StarkSans-BoldOblique",
    ("serif", False, False): "StarkSerif",
    ("serif", True, False): "StarkSerif-Bold",
    ("serif", False, True): "StarkSerif-Italic",
    ("serif", True, True): "StarkSerif-BoldItalic",
    ("mono", False, False): "StarkMono",
    ("mono", True, False): "StarkMono-Bold",
    ("mono", False, True): "StarkMono-Oblique",
    ("mono", True, True): "StarkMono-BoldOblique",
}

_BUILTIN = {
    ("sans", False, False): "Helvetica",
    ("sans", True, False): "Helvetica-Bold",
    ("sans", False, True): "Helvetica-Oblique",
    ("sans", True, True): "Helvetica-BoldOblique",
    ("serif", False, False): "Times-Roman",
    ("serif", True, False): "Times-Bold",
    ("serif", False, True): "Times-Italic",
    ("serif", True, True): "Times-BoldItalic",
    ("mono", False, False): "Courier",
    ("mono", True, False): "Courier-Bold",
    ("mono", False, True): "Courier-Oblique",
    ("mono", True, True): "Courier-BoldOblique",
}

_FAMILY = {
    "sans": "StarkSans",
    "serif": "StarkSerif",
    "mono": "StarkMono",
}

_FONTS: dict[tuple[str, bool, bool], str] | None = None


def _candidates(filenames: tuple[str, ...]) -> list[Path]:
    roots = [Path(root) for root in _DEJAVU] + [Path(root) for root in _MAC]
    found: list[Path] = []
    for name in filenames:
        for root in roots:
            path = root / name
            if path.exists():
                found.append(path)
    return found


def _register(name: str, path: Path) -> bool:
    try:
        pdfmetrics.registerFont(TTFont(name, str(path)))
    except Exception:
        return name in pdfmetrics.getRegisteredFontNames()
    return True


def _nearest(resolved: dict[tuple[str, bool, bool], str], key: tuple[str, bool, bool]) -> str:
    if key in resolved:
        return resolved[key]
    family, bold, italic = key
    for candidate in (
        (family, bold, False),
        (family, False, italic),
        (family, False, False),
        (family, True, False),
        (family, False, True),
        (family, True, True),
    ):
        if candidate in resolved:
            return resolved[candidate]
    return _BUILTIN.get(key, "Helvetica")


def ensure_fonts() -> dict[tuple[str, bool, bool], str]:
    global _FONTS
    if _FONTS is not None:
        return _FONTS
    resolved: dict[tuple[str, bool, bool], str] = {}
    for key, filenames in _FILES.items():
        for path in _candidates(filenames):
            if _register(_NAMES[key], path):
                resolved[key] = _NAMES[key]
                break
    mapping = {key: _nearest(resolved, key) for key in _FILES}
    for family, family_name in _FAMILY.items():
        pdfmetrics.registerFontFamily(
            family_name,
            normal=mapping[(family, False, False)],
            bold=mapping[(family, True, False)],
            italic=mapping[(family, False, True)],
            boldItalic=mapping[(family, True, True)],
        )
    _FONTS = mapping
    return mapping


def ensure_font() -> str:
    return ensure_fonts().get(("sans", False, False), "Helvetica")
