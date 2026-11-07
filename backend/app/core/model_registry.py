from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from app.config import get_settings

TIER_RANK = {"fast": 1, "balanced": 2, "best": 3}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    alias: str
    tokenizer: str
    context_window: int
    max_output_tokens: int
    input_price_per_1m: float
    output_price_per_1m: float
    quality_tier: str
    supports_vision: bool = True
    image_base_tokens: int = 85
    image_tokens_per_tile: int = 170
    image_token_multiplier: float = 1
    uses_max_completion_tokens: bool = True
    segment_cap_tokens: int | None = None
    cached_input_price_per_1m: float | None = None

    def public(self) -> dict:
        data = asdict(self)
        return data


_DEFAULTS: list[ModelSpec] = [
    ModelSpec(
        id="gpt-4o-mini-2024-07-18",
        alias="gpt-4o-mini",
        tokenizer="o200k_base",
        context_window=128_000,
        max_output_tokens=16_384,
        input_price_per_1m=0.15,
        output_price_per_1m=0.60,
        quality_tier="fast",
        supports_vision=True,
        image_base_tokens=2833,
        image_tokens_per_tile=5667,
        image_token_multiplier=1,
        cached_input_price_per_1m=0.075,
    ),
    ModelSpec(
        id="gpt-4o-2024-08-06",
        alias="gpt-4o",
        tokenizer="o200k_base",
        context_window=128_000,
        max_output_tokens=16_384,
        input_price_per_1m=2.50,
        output_price_per_1m=10.00,
        quality_tier="best",
        supports_vision=True,
        image_base_tokens=85,
        image_tokens_per_tile=170,
        image_token_multiplier=1,
        cached_input_price_per_1m=1.25,
    ),
]


def _from_mapping(row: dict) -> ModelSpec:
    fields = {key: row[key] for key in ModelSpec.__dataclass_fields__ if key in row}
    return ModelSpec(**fields)


def _models_path() -> Path | None:
    explicit = get_settings().models_config
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    here = Path(__file__).resolve()
    candidates.extend([here.parents[2] / "models.yaml", here.parents[3] / "models.yaml"])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_models() -> list[ModelSpec]:
    path = _models_path()
    if path is None:
        return list(_DEFAULTS)
    raw = yaml.safe_load(path.read_text()) or {}
    rows = raw.get("models") or []
    specs = [_from_mapping(row) for row in rows]
    return specs or list(_DEFAULTS)


def all_models() -> list[ModelSpec]:
    return load_models()


def resolve_model(name: str | None) -> ModelSpec:
    wanted = name or get_settings().default_model
    models = load_models()
    for spec in models:
        if spec.id == wanted or spec.alias == wanted:
            return spec
    known = ", ".join(spec.alias for spec in models)
    from app.core.errors import AppError

    raise AppError(
        "unknown_model",
        f"Model '{wanted}' is not available. Choose one of: {known}.",
        wanted,
        False,
        400,
    )


def tier_allows_reuse(stored_model: str | None, requested: ModelSpec) -> bool:
    if not stored_model:
        return True
    try:
        stored = resolve_model(stored_model)
    except Exception:
        return True
    return TIER_RANK.get(stored.quality_tier, 0) >= TIER_RANK.get(requested.quality_tier, 0)


def cached_input_price(spec: ModelSpec) -> float:
    if spec.cached_input_price_per_1m is None:
        return spec.input_price_per_1m / 2
    return spec.cached_input_price_per_1m


def cost_cents(spec: ModelSpec, tokens_in: int, tokens_out: int, tokens_cached: int = 0) -> Decimal:
    incoming = max(int(tokens_in or 0), 0)
    outgoing = max(int(tokens_out or 0), 0)
    cached = min(max(int(tokens_cached or 0), 0), incoming)
    uncached = incoming - cached
    dollars = (
        Decimal(uncached) * Decimal(str(spec.input_price_per_1m))
        + Decimal(cached) * Decimal(str(cached_input_price(spec)))
        + Decimal(outgoing) * Decimal(str(spec.output_price_per_1m))
    ) / Decimal(1_000_000)
    return (dollars * Decimal(100)).quantize(Decimal("0.0001"))


def image_tokens(spec: ModelSpec, width: int, height: int, detail: str = "high") -> int:
    if detail == "low":
        return int(spec.image_base_tokens * spec.image_token_multiplier)
    tiles_x = max(1, -(-width // 512))
    tiles_y = max(1, -(-height // 512))
    tiles = tiles_x * tiles_y
    return int((spec.image_base_tokens + spec.image_tokens_per_tile * tiles) * spec.image_token_multiplier)


def image_cost_cents(spec: ModelSpec, width: int, height: int, detail: str = "high") -> Decimal:
    tokens = image_tokens(spec, width, height, detail)
    return cost_cents(spec, tokens, 0)
