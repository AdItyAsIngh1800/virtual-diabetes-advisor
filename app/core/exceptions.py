"""Project-wide exception types.

Every module raises one of these instead of a bare ``Exception`` so callers
(API routes, batch jobs) can catch exactly what they know how to handle.
"""

from __future__ import annotations


class DiabetesAdvisorError(Exception):
    """Base class for all errors raised by this project."""


class DataValidationError(DiabetesAdvisorError):
    """Raised when an input dataset is missing columns or has out-of-range values."""


class ModelNotTrainedError(DiabetesAdvisorError):
    """Raised when the risk model artifact is missing and inference is requested."""
