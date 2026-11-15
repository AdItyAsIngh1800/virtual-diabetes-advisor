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


class BatchAlreadyRunningError(DiabetesAdvisorError):
    """Raised when a batch job starts while another run of the same job is in progress.

    The ``batch_runs_one_running_idx`` unique index (0005) allows one ``running`` row
    per job; the second starter gets this instead of processing the window twice.
    """


class WhatsAppDeliveryError(DiabetesAdvisorError):
    """Raised when a WhatsApp Cloud API call fails or is not allowed (R3/R4).

    Graph API errors are parsed into the attributes below so callers can record
    ``code`` on the advisory row and decide whether a later retry makes sense.

    Attributes:
        http_status: HTTP status of the last response, or ``None`` if no response
            arrived (network error) or no request was made (guard block).
        code: Graph ``error.code`` (e.g. ``131047`` re-engagement required).
        subcode: Graph ``error.error_subcode``, if any.
        fbtrace_id: Meta's trace id, quoted when asking Meta support.
        retryable: ``True`` for transient failures (5xx, rate limits, connect errors).
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        code: int | None = None,
        subcode: int | None = None,
        fbtrace_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id
        self.retryable = retryable


class RecipientBlockedError(WhatsAppDeliveryError):
    """Raised by the send guard before any HTTP call, when a recipient is not allowed.

    Examples: a demo patient still on a fictional 555 number, or a number outside
    ``ALLOWED_TEST_NUMBERS`` while WhatsApp runs on Meta's test number. Never retryable.
    """


class ThresholdComputationError(DiabetesAdvisorError):
    """Raised when a personal threshold cannot be computed or applied (R2/R3).

    Examples: a reading needs a limit the user has no threshold for yet, or the
    inputs to a revision are inconsistent (a previous threshold for another limit).
    """
