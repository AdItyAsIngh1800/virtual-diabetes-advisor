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


class AuthenticationError(DiabetesAdvisorError):
    """Raised when a Supabase access token is missing, expired or invalid."""


class NotificationDeliveryError(DiabetesAdvisorError):
    """Raised when a notification (Web Push, R3) cannot be delivered or is not allowed.

    Attributes:
        status_code: HTTP status from the push service, or ``None`` when no response
            arrived (network error) or no request was made (guard block).
        retryable: ``True`` for transient failures (5xx, 429, connection errors).
    """

    def __init__(
        self, message: str, *, status_code: int | None = None, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class RecipientBlockedError(NotificationDeliveryError):
    """Raised by the recipient guard before anything is sent.

    Example: a demo patient that is not linked to the developer's own login
    (``seed_demo --my-email``). Never retryable.
    """


class ThresholdComputationError(DiabetesAdvisorError):
    """Raised when a personal threshold cannot be computed or applied (R2/R3).

    Examples: a reading needs a limit the user has no threshold for yet, or the
    inputs to a revision are inconsistent (a previous threshold for another limit).
    """


class SpeechError(DiabetesAdvisorError):
    """Raised when text-to-speech or speech-to-text fails (R3 voice advisory, R4 chat).

    Callers treat it as non-fatal for advisories: the page falls back to the browser's
    ``speechSynthesis`` when no audio file exists.
    """


class AudioStorageError(DiabetesAdvisorError):
    """Raised when voice audio cannot be uploaded to, or signed from, Supabase Storage."""
