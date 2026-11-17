"""Load, validate and split the BRFSS 2015 diabetes CSVs.

Which file is used for what (see the ``brfss-model`` project skill):
    * ``binary_5050``  - balanced, ~70k rows: quick experiments and notebook demos.
    * ``binary``       - real prevalence (~14 % positive), ~254k rows: final training,
                         calibration and honest evaluation.
    * ``three_class``  - none / prediabetes / diabetes: multi-class demo in the notebook.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final, NamedTuple

import pandas as pd
from sklearn.model_selection import train_test_split

from app.core.exceptions import DataValidationError
from app.ml.schema import (
    BINARY_TARGET,
    FEATURE_RANGES,
    FEATURES,
    THREE_CLASS_TARGET,
    DatasetKind,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR: Final = PROJECT_ROOT / "data" / "raw"
RANDOM_STATE: Final = 42  # fixed seed so every run and every teammate gets the same split

_FILENAMES: Final[dict[DatasetKind, str]] = {
    "binary": "diabetes_binary_health_indicators_BRFSS2015.csv",
    "binary_5050": "diabetes_binary_5050split_health_indicators_BRFSS2015.csv",
    "three_class": "diabetes_012_health_indicators_BRFSS2015.csv",
}


def dataset_path(kind: DatasetKind, data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    """Return where a BRFSS file is expected.

    Args:
        kind: Which BRFSS file.
        data_dir: Folder holding the CSVs.

    Returns:
        The CSV path (it may not exist).
    """
    return data_dir / _FILENAMES[kind]


def missing_datasets(data_dir: Path = DEFAULT_DATA_DIR) -> list[Path]:
    """List the BRFSS CSVs that are not in ``data_dir``.

    Args:
        data_dir: Folder holding the CSVs.

    Returns:
        Paths of the missing files (empty when all three are present).
    """
    return [
        dataset_path(kind, data_dir)
        for kind in _FILENAMES
        if not dataset_path(kind, data_dir).exists()
    ]


def target_column(kind: DatasetKind) -> str:
    """Return the name of the label column for a dataset kind.

    Args:
        kind: Which BRFSS file.

    Returns:
        ``"Diabetes_012"`` for the three-class file, otherwise ``"Diabetes_binary"``.
    """
    return THREE_CLASS_TARGET if kind == "three_class" else BINARY_TARGET


def validate_frame(df: pd.DataFrame, target: str) -> None:
    """Check that a BRFSS frame has every column, no nulls and in-range values.

    Args:
        df: Raw frame read from a CSV.
        target: Name of the label column expected in ``df``.

    Raises:
        DataValidationError: On missing columns, null values or out-of-range values.
    """
    missing = [col for col in (target, *FEATURES) if col not in df.columns]
    if missing:
        raise DataValidationError(f"Missing columns: {missing}")

    null_counts = df[[target, *FEATURES]].isna().sum()
    if int(null_counts.sum()) > 0:
        raise DataValidationError(f"Null values found: {null_counts[null_counts > 0].to_dict()}")

    for col, (low, high) in FEATURE_RANGES.items():
        out_of_range = int(((df[col] < low) | (df[col] > high)).sum())
        if out_of_range:
            raise DataValidationError(f"{col}: {out_of_range} values outside [{low}, {high}]")


def load_brfss(
    kind: DatasetKind,
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    drop_duplicates: bool = True,
) -> pd.DataFrame:
    """Read one BRFSS CSV, validate it and optionally drop exact duplicate rows.

    Duplicates are dropped by default: identical survey answers would otherwise land in
    both train and test splits and inflate test scores (~24k such rows in the full file).

    Args:
        kind: Which of the three files to load.
        data_dir: Folder holding the CSVs.
        drop_duplicates: Whether to remove exact duplicate rows.

    Returns:
        A DataFrame with the label column followed by :data:`FEATURES`, as ``float64``.

    Raises:
        FileNotFoundError: If the CSV is not in ``data_dir``.
        DataValidationError: If the file fails :func:`validate_frame`.
    """
    path = dataset_path(kind, data_dir)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - put the BRFSS CSVs in {data_dir}")

    target = target_column(kind)
    df = pd.read_csv(path)
    validate_frame(df, target)
    df = df[[target, *FEATURES]].astype("float64")

    if drop_duplicates:
        before = len(df)
        df = df.drop_duplicates().reset_index(drop=True)
        logger.info("%s: dropped %d duplicate rows (%d left)", kind, before - len(df), len(df))
    return df


class DataSplits(NamedTuple):
    """Stratified train / validation / test split of features and labels."""

    x_train: pd.DataFrame
    x_val: pd.DataFrame
    x_test: pd.DataFrame
    y_train: pd.Series
    y_val: pd.Series
    y_test: pd.Series


def split_data(
    df: pd.DataFrame,
    target: str,
    *,
    val_size: float = 0.15,
    test_size: float = 0.15,
    random_state: int = RANDOM_STATE,
) -> DataSplits:
    """Split a frame into stratified train / validation / test sets.

    Stratifying keeps the diabetic share identical in every split, which matters when
    only ~14 % of rows are positive.

    Args:
        df: Frame containing ``target`` and :data:`FEATURES`.
        target: Label column name.
        val_size: Fraction of all rows used for validation (model and threshold choice).
        test_size: Fraction of all rows held out for the final, untouched evaluation.
        random_state: Seed for reproducibility.

    Returns:
        The six arrays as a :class:`DataSplits`.

    Raises:
        ValueError: If ``val_size + test_size`` is not strictly between 0 and 1.
    """
    if not 0 < val_size + test_size < 1:
        raise ValueError("val_size + test_size must be between 0 and 1")

    x = df[list(FEATURES)]
    y = df[target].astype(int)
    x_rest, x_test, y_rest, y_test = train_test_split(
        x, y, test_size=test_size, stratify=y, random_state=random_state
    )
    # val_size is a share of the whole dataset, so rescale it to a share of what is left.
    relative_val = val_size / (1 - test_size)
    x_train, x_val, y_train, y_val = train_test_split(
        x_rest, y_rest, test_size=relative_val, stratify=y_rest, random_state=random_state
    )
    return DataSplits(x_train, x_val, x_test, y_train, y_val, y_test)
