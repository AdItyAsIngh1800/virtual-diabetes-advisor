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


class ModelMetadataError(DiabetesAdvisorError):
    """Raised when ``risk_model.meta.json`` exists but is not valid metadata (R5).

    The model card answers 500 with a logged error rather than showing numbers it
    could not validate.
    """


class TrainingDataMissingError(DiabetesAdvisorError):
    """Raised when a retrain starts without the BRFSS CSVs in the data folder (R5)."""


class ConfigurationError(DiabetesAdvisorError):
    """Raised when required settings are missing or invalid.

    The message names the offending variables only, never their values, so it is
    safe to log even when the variable holds a secret.
    """


class ClinicalLimitError(DiabetesAdvisorError):
    """Raised when a clinical limit is requested that has no cited source."""


class AuditLogImmutableError(DiabetesAdvisorError):
    """Raised when code tries to update or delete an append-only audit row (R5)."""


class TurnNotReviewableError(DiabetesAdvisorError):
    """Raised when a review names a turn the reviewer may not review (R5).

    The turn does not exist, is not an advisor reply, or belongs to a patient the
    reviewer cannot see. The API answers 404 in every case, so ids cannot be probed.
    """


class ReviewNoteRequiredError(DiabetesAdvisorError):
    """Raised when a review verdict that needs an explanation has no note (R5)."""


class KbEntryNotFoundError(DiabetesAdvisorError):
    """Raised when a knowledge-base entry id does not exist (R5; the API answers 404)."""


class KbEntryStateError(DiabetesAdvisorError):
    """Raised when a KB action does not fit the entry's status (R5; the API answers 409).

    Only drafts may be edited, approved or rejected, only approved entries may be
    superseded, and a draft cannot be approved once the entry it supersedes is no
    longer the approved version.
    """


class KbApprovalIncompleteError(DiabetesAdvisorError):
    """Raised when a draft is approved without a body or a source citation (R5; 422)."""


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


class AudioTooLongError(SpeechError):
    """Raised when a voice message is longer than ``CHAT_AUDIO_MAX_SECONDS`` (R4 chat)."""


class AudioStorageError(DiabetesAdvisorError):
    """Raised when voice audio cannot be uploaded to, or signed from, Supabase Storage."""
