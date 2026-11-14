"""Tests for app.core.clinical_limits: cited values, clamps and safety invariants."""

from __future__ import annotations

import ast
import inspect
import itertools
from typing import get_args

import pytest
from pydantic import ValidationError

from app.core import clinical_limits as cl
from app.core.clinical_limits import (
    GLUCOSE_LIMITS,
    DiabetesStatus,
    GlucoseLimit,
    GlucoseLimitKind,
    get_glucose_limit,
)
from app.core.exceptions import ClinicalLimitError

STATUSES: tuple[DiabetesStatus, ...] = get_args(DiabetesStatus)
KINDS: tuple[GlucoseLimitKind, ...] = get_args(GlucoseLimitKind)
SOURCE_TAGS = ("ADA-2", "ADA-6", "ADA-13", "TIR", "IHSG")
DEFAULT_BAND = 0.05  # mirrors Settings.pre_breach_band default


# --- Published values: pinned so any change is a deliberate, reviewed edit -----


@pytest.mark.parametrize(
    ("status", "kind", "base", "hard_min", "hard_max"),
    [
        ("diabetes", "fasting_high", 130, 100, 180),
        ("diabetes", "post_meal_high", 180, 140, 250),
        ("diabetes", "low", 70, 70, 80),
        ("no_diabetes", "fasting_high", 100, 100, 125),
        ("no_diabetes", "post_meal_high", 140, 140, 199),
        ("no_diabetes", "low", 70, 70, 70),
    ],
)
def test_pinned_values(
    status: DiabetesStatus,
    kind: GlucoseLimitKind,
    base: float,
    hard_min: float,
    hard_max: float,
) -> None:
    limit = get_glucose_limit(status, kind)
    assert (limit.base_mg_dl, limit.hard_min_mg_dl, limit.hard_max_mg_dl) == (
        base,
        hard_min,
        hard_max,
    )


def test_hypo_level_2() -> None:
    assert cl.HYPO_LEVEL_2_MG_DL == 54
    assert cl.HYPO_LEVEL_2_MG_DL < cl.HYPO_LEVEL_1_MG_DL


# --- Coverage and shape ------------------------------------------------------


def test_every_status_kind_pair_has_a_limit() -> None:
    assert set(GLUCOSE_LIMITS) == set(itertools.product(STATUSES, KINDS))


@pytest.mark.parametrize(("status", "kind"), list(itertools.product(STATUSES, KINDS)))
def test_direction_matches_kind(status: DiabetesStatus, kind: GlucoseLimitKind) -> None:
    limit = get_glucose_limit(status, kind)
    assert limit.direction == ("lower" if kind == "low" else "upper")
    assert limit.source


def test_unknown_pair_raises() -> None:
    with pytest.raises(ClinicalLimitError):
        get_glucose_limit("diabetes", "hba1c")  # type: ignore[arg-type]


def test_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        GLUCOSE_LIMITS[("diabetes", "low")] = GLUCOSE_LIMITS[("diabetes", "low")]  # type: ignore[index]
    limit = get_glucose_limit("diabetes", "low")
    with pytest.raises(ValidationError):
        limit.base_mg_dl = 10  # type: ignore[misc]


def test_base_outside_clamp_rejected() -> None:
    with pytest.raises(ValidationError, match="outside"):
        GlucoseLimit(
            status="diabetes",
            kind="fasting_high",
            direction="upper",
            base_mg_dl=200,
            hard_min_mg_dl=100,
            hard_max_mg_dl=180,
            source="test",
        )


# --- Clamp boundaries --------------------------------------------------------


@pytest.mark.parametrize(
    ("proposed", "expected"),
    [
        (50.0, 100.0),  # far below -> hard min
        (99.99, 100.0),  # just below
        (100.0, 100.0),  # exactly hard min
        (130.0, 130.0),  # base untouched
        (180.0, 180.0),  # exactly hard max
        (180.01, 180.0),  # just above
        (400.0, 180.0),  # far above -> hard max
    ],
)
def test_clamp_boundaries(proposed: float, expected: float) -> None:
    assert get_glucose_limit("diabetes", "fasting_high").clamp(proposed) == expected


@pytest.mark.parametrize("status", STATUSES)
def test_low_limit_never_relaxed_into_hypoglycaemia(status: DiabetesStatus) -> None:
    low = get_glucose_limit(status, "low")
    assert low.clamp(0.0) == cl.HYPO_LEVEL_1_MG_DL
    assert low.clamp(cl.HYPO_LEVEL_2_MG_DL) == cl.HYPO_LEVEL_1_MG_DL


def test_no_diabetes_low_is_fixed() -> None:
    low = get_glucose_limit("no_diabetes", "low")
    assert low.clamp(60) == low.clamp(90) == 70


def test_upper_limits_never_reach_diagnostic_or_level_2() -> None:
    # ADA-2: FPG >= 126 and 2-h OGTT >= 200 are diagnostic for diabetes.
    assert get_glucose_limit("no_diabetes", "fasting_high").clamp(1000) < 126
    assert get_glucose_limit("no_diabetes", "post_meal_high").clamp(1000) < 200
    # ADA-6 / TIR: > 250 is Level 2 hyperglycaemia.
    assert get_glucose_limit("diabetes", "post_meal_high").clamp(1000) <= 250


# --- Cross-limit safety invariant --------------------------------------------


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("upper_kind", ["fasting_high", "post_meal_high"])
def test_low_and_high_bands_never_overlap(
    status: DiabetesStatus, upper_kind: GlucoseLimitKind
) -> None:
    """Even at the clamp extremes, a reading can't be pre-breach-low and pre-breach-high."""
    low = get_glucose_limit(status, "low")
    high = get_glucose_limit(status, upper_kind)
    highest_low_band = low.hard_max_mg_dl * (1 + DEFAULT_BAND)
    lowest_high_band = high.hard_min_mg_dl * (1 - DEFAULT_BAND)
    assert highest_low_band < lowest_high_band


# --- Citation enforcement ----------------------------------------------------


def _numeric_constants() -> list[tuple[str, int]]:
    """Return (name, line) for every module-level numeric constant in clinical_limits."""
    tree = ast.parse(inspect.getsource(cl))
    found: list[tuple[str, int]] = []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int | float)
            and not isinstance(node.value.value, bool)
        ):
            target = node.targets[0]
            assert isinstance(target, ast.Name)
            found.append((target.id, node.lineno))
    return found


def test_module_has_numeric_constants() -> None:
    assert len(_numeric_constants()) >= 10


@pytest.mark.parametrize(("name", "lineno"), _numeric_constants())
def test_every_number_is_cited(name: str, lineno: int) -> None:
    """Each constant must be directly preceded by a comment naming a source tag."""
    lines = inspect.getsource(cl).splitlines()
    comment: list[str] = []
    i = lineno - 2  # line above the assignment (0-indexed)
    while i >= 0 and lines[i].lstrip().startswith("#"):
        comment.append(lines[i])
        i -= 1
    text = " ".join(comment)
    assert any(tag in text for tag in SOURCE_TAGS), f"{name} has no cited source comment"


def test_no_inline_numbers_in_limit_table() -> None:
    """The limit table must use named, cited constants - never bare literals."""
    tree = ast.parse(inspect.getsource(cl))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "GlucoseLimit":
            for kw in node.keywords:
                if kw.arg and kw.arg.endswith("_mg_dl"):
                    assert isinstance(kw.value, ast.Name), f"inline number for {kw.arg}"
