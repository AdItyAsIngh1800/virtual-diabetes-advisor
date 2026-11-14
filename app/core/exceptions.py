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


class ConfigurationError(DiabetesAdvisorError):
    """Raised when required settings are missing or invalid.

    The message names the offending variables only, never their values, so it is
    safe to log even when the variable holds a secret.
    """


class ClinicalLimitError(DiabetesAdvisorError):
    """Raised when a clinical limit is requested that has no cited source."""


class AuditLogImmutableError(DiabetesAdvisorError):
    """Raised when code tries to update or delete an append-only audit row (R5)."""


class ThresholdComputationError(DiabetesAdvisorError):
    """Raised when a personal threshold cannot be computed or applied (R2/R3).

    Examples: a reading needs a limit the user has no threshold for yet, or the
    inputs to a revision are inconsistent (a previous threshold for another limit).
    """
