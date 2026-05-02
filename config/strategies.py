"""Predefined strategy templates with parameter boundaries.

These strategies define the framework within which Gemini AI can recommend.
Immutable at runtime — only code changes can add/modify strategies.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StrategyBounds:
    """Parameter boundaries for a strategy. Gemini must stay within these."""

    grid_spacing_pct_min: float
    grid_spacing_pct_max: float
    num_grids_min: int
    num_grids_max: int
    price_range_width_pct_min: float
    price_range_width_pct_max: float
    # Futures-specific
    leverage_min: int = 1
    leverage_max: int = 3
    allowed_directions: tuple[str, ...] = ("NEUTRAL",)


@dataclass(frozen=True)
class StrategyProfile:
    name: str
    display_name: str
    description: str
    risk_level: str
    suitable_conditions: tuple[str, ...]
    defaults: dict[str, float | int] = field(default_factory=dict)
    bounds: StrategyBounds = field(default_factory=lambda: StrategyBounds(0, 0, 0, 0, 0, 0))


STRATEGY_REGISTRY: dict[str, StrategyProfile] = {
    "conservative": StrategyProfile(
        name="conservative",
        display_name="保守型",
        description="適合低波動、橫盤整理的市場，以小格距密集捕捉微利",
        risk_level="low",
        suitable_conditions=("低波動", "橫盤整理", "穩定市場"),
        defaults={
            "grid_spacing_pct": 1.0, "num_grids": 15,
            "price_range_width_pct": 10.0, "leverage": 2,
        },
        bounds=StrategyBounds(
            grid_spacing_pct_min=0.5, grid_spacing_pct_max=1.5,
            num_grids_min=10, num_grids_max=20,
            price_range_width_pct_min=5.0, price_range_width_pct_max=15.0,
            leverage_min=1, leverage_max=3,
            allowed_directions=("NEUTRAL",),
        ),
    ),
    "moderate": StrategyProfile(
        name="moderate",
        display_name="穩健型",
        description="適合正常波動、輕微趨勢的市場，平衡風險與收益",
        risk_level="medium",
        suitable_conditions=("正常波動", "輕微趨勢", "一般市場"),
        defaults={
            "grid_spacing_pct": 1.5, "num_grids": 12,
            "price_range_width_pct": 18.0, "leverage": 3,
        },
        bounds=StrategyBounds(
            grid_spacing_pct_min=1.0, grid_spacing_pct_max=2.5,
            num_grids_min=8, num_grids_max=15,
            price_range_width_pct_min=10.0, price_range_width_pct_max=25.0,
            leverage_min=2, leverage_max=5,
            allowed_directions=("NEUTRAL", "LONG", "SHORT"),
        ),
    ),
    "aggressive": StrategyProfile(
        name="aggressive",
        display_name="積極型",
        description="適合高波動、大幅震盪的市場，以大格距捕捉大幅價差",
        risk_level="high",
        suitable_conditions=("高波動", "大幅震盪", "劇烈行情"),
        defaults={
            "grid_spacing_pct": 3.0, "num_grids": 8,
            "price_range_width_pct": 30.0, "leverage": 5,
        },
        bounds=StrategyBounds(
            grid_spacing_pct_min=2.0, grid_spacing_pct_max=5.0,
            num_grids_min=5, num_grids_max=12,
            price_range_width_pct_min=20.0, price_range_width_pct_max=40.0,
            leverage_min=3, leverage_max=10,
            allowed_directions=("NEUTRAL", "LONG", "SHORT"),
        ),
    ),
    "range_bound": StrategyProfile(
        name="range_bound",
        display_name="區間型",
        description="適合有明確支撐壓力的窄幅盤整市場，密集格子最大化成交",
        risk_level="medium",
        suitable_conditions=("強支撐壓力", "窄幅盤整", "區間震盪"),
        defaults={
            "grid_spacing_pct": 0.5, "num_grids": 30,
            "price_range_width_pct": 6.0, "leverage": 2,
        },
        bounds=StrategyBounds(
            grid_spacing_pct_min=0.3, grid_spacing_pct_max=1.0,
            num_grids_min=20, num_grids_max=50,
            price_range_width_pct_min=3.0, price_range_width_pct_max=10.0,
            leverage_min=1, leverage_max=3,
            allowed_directions=("NEUTRAL",),
        ),
    ),
    "trending": StrategyProfile(
        name="trending",
        display_name="趨勢型",
        description="適合有方向性偏差或預期突破的市場，寬範圍應對趨勢",
        risk_level="medium-high",
        suitable_conditions=("方向性偏差", "突破預期", "趨勢行情"),
        defaults={
            "grid_spacing_pct": 2.5, "num_grids": 8,
            "price_range_width_pct": 25.0, "leverage": 4,
        },
        bounds=StrategyBounds(
            grid_spacing_pct_min=1.5, grid_spacing_pct_max=4.0,
            num_grids_min=6, num_grids_max=10,
            price_range_width_pct_min=15.0, price_range_width_pct_max=35.0,
            leverage_min=2, leverage_max=7,
            allowed_directions=("LONG", "SHORT", "NEUTRAL"),
        ),
    ),
}


def get_strategy(name: str) -> StrategyProfile:
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(STRATEGY_REGISTRY.keys())}")
    return STRATEGY_REGISTRY[name]


def clamp_param(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(max_val, value))


def validate_and_clamp_adjustments(
    strategy_name: str,
    adjustments: dict[str, float | int],
) -> tuple[dict[str, float | int], list[str]]:
    """Validate parameter adjustments against strategy bounds. Returns clamped values and warnings."""
    strategy = get_strategy(strategy_name)
    bounds = strategy.bounds
    clamped: dict[str, float | int] = {}
    warnings: list[str] = []

    bound_map: dict[str, tuple[float, float]] = {
        "grid_spacing_pct": (bounds.grid_spacing_pct_min, bounds.grid_spacing_pct_max),
        "num_grids": (bounds.num_grids_min, bounds.num_grids_max),
        "price_range_width_pct": (bounds.price_range_width_pct_min, bounds.price_range_width_pct_max),
        "leverage": (bounds.leverage_min, bounds.leverage_max),
    }

    for param, value in adjustments.items():
        if param == "direction":
            if value not in bounds.allowed_directions:
                warnings.append(f"方向 '{value}' 不在允許範圍 {bounds.allowed_directions}，已忽略")
            else:
                clamped[param] = value
            continue

        if param not in bound_map:
            warnings.append(f"未知參數 '{param}'，已忽略")
            continue

        lo, hi = bound_map[param]
        clamped_value = clamp_param(float(value), lo, hi)
        if clamped_value != float(value):
            warnings.append(f"參數 '{param}' 值 {value} 超出邊界 [{lo}, {hi}]，已修正為 {clamped_value}")

        if param in ("num_grids", "leverage"):
            clamped[param] = int(clamped_value)
        else:
            clamped[param] = clamped_value

    return clamped, warnings


def get_strategies_for_prompt() -> dict:
    """Export strategy definitions in a format suitable for Gemini prompt injection."""
    result = {}
    for name, profile in STRATEGY_REGISTRY.items():
        b = profile.bounds
        result[name] = {
            "display_name": profile.display_name,
            "risk_level": profile.risk_level,
            "description": profile.description,
            "suitable_conditions": list(profile.suitable_conditions),
            "bounds": {
                "grid_spacing_pct": [b.grid_spacing_pct_min, b.grid_spacing_pct_max],
                "num_grids": [b.num_grids_min, b.num_grids_max],
                "price_range_width_pct": [b.price_range_width_pct_min, b.price_range_width_pct_max],
                "leverage": [b.leverage_min, b.leverage_max],
                "allowed_directions": list(b.allowed_directions),
            },
            "defaults": profile.defaults,
        }
    return result
