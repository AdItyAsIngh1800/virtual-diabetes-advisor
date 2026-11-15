"""Advisory dispatcher (R3): voice audio, Web Push and Realtime for queued advisories.

Runs right after every batch update and on its own schedule
(``DISPATCH_INTERVAL_MINUTES``, default 2). One run::

    fail claims left by a crashed run (never re-sent: that could push twice)
    for each queued advisory, oldest reading first:
        reading older than ADVISORY_MAX_AGE_MINUTES  -> expired     (never sent)
        recipient guard refuses (unlinked demo user) -> failed      (recipient_blocked)
        same limit+state pushed within the cooldown  -> suppressed  (except < 54 mg/dL)
        claim it (conditional UPDATE, committed)     -> lost? another run has it: skip
        gTTS -> mp3 -> private Storage bucket         (failure is non-fatal)
        Web Push, urgency high for breach / < 54     -> sent | failed (+ error)
        Realtime broadcast to the patient's open app

**At most once:** every transition is a conditional UPDATE on ``delivery_status =
'queued' AND dispatch_claimed_at IS NULL``, and the claim is committed before the push
is sent. Running the dispatcher twice, even concurrently, pushes each advisory once.

Entry points: :func:`scheduled_dispatch` (APScheduler, after each batch run),
:func:`run_dispatch` (admin API, demo script) and ``uv run python -m
app.jobs.dispatch_advisories``.
"""

from __future__ import annotations

import argparse
import logging
import threading
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.clinical_limits import HYPO_LEVEL_2_MG_DL, GlucoseLimitKind
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AudioStorageError,
    ConfigurationError,
    NotificationDeliveryError,
    RecipientBlockedError,
    SpeechError,
)
from app.core.logging import configure_logging
from app.db.advisory_repository import QueuedAdvisory, SqlDispatchStore
from app.db.models import AdvisoryState
from app.db.push_repository import SqlSubscriptionStore
from app.services.advisory import notification_text, spoken_text
from app.services.notifications import (
    DEFAULT_TTL_SECONDS,
    DeliveryReport,
    Notification,
    NotificationChannel,
    Recipient,
    check_recipient,
    is_expired,
    urgency_for,
)
from app.services.realtime import AdvisoryPublisher, HttpRealtimePublisher, advisory_page_url
from app.services.voice import (
    AudioStore,
    GttsSynthesizer,
    SpeechSynthesizer,
    SupabaseAudioStore,
    advisory_audio_path,
)
from app.services.webpush import PywebpushSender, WebPushChannel

logger = logging.getLogger(__name__)

# One advisory takes seconds (gTTS ~1-2 s, at most 3 push attempts of <= 10 s each plus
# backoff). A claim still unfinished after 10 min belongs to a run that died.
STALE_CLAIM_AFTER: Final = timedelta(minutes=10)
# Push services drop a message whose TTL ran out; never ask for less than a minute so
# a phone that is briefly offline still gets an alert that is about to expire.
_MIN_TTL_SECONDS: Final = 60


class DispatchStore(Protocol):
    """Advisory persistence the dispatcher needs (:class:`SqlDispatchStore` in production)."""

    def fail_stale_claims(self, *, claimed_before: datetime, at: datetime) -> int:
        """Fail claims older than ``claimed_before``; return how many."""
        ...

    def queued(self, user_ids: Collection[uuid.UUID] | None) -> list[QueuedAdvisory]:
        """Queued, unclaimed advisories, oldest reading first."""
        ...

    def expire(self, advisory_id: uuid.UUID) -> bool:
        """Mark expired if still queued and unclaimed."""
        ...

    def suppress(self, advisory_id: uuid.UUID) -> bool:
        """Mark suppressed if still queued and unclaimed."""
        ...

    def block(self, advisory_id: uuid.UUID, at: datetime, reason: str) -> bool:
        """Mark failed (recipient_blocked) if still queued and unclaimed."""
        ...

    def claim(self, advisory_id: uuid.UUID, at: datetime) -> bool:
        """Claim for sending; ``True`` for exactly one caller."""
        ...

    def last_push_at(
        self,
        *,
        user_id: uuid.UUID,
        limit_kind: GlucoseLimitKind,
        state: AdvisoryState,
        exclude_id: uuid.UUID,
    ) -> datetime | None:
        """Latest push for this user, limit and state."""
        ...

    def record_sent(
        self,
        advisory_id: uuid.UUID,
        *,
        at: datetime,
        message_id: str | None,
        audio_path: str | None,
    ) -> None:
        """Record an accepted push."""
        ...

    def record_failed(
        self,
        advisory_id: uuid.UUID,
        *,
        at: datetime,
        code: str,
        error: str,
        audio_path: str | None,
    ) -> None:
        """Record a failed push."""
        ...


@dataclass(frozen=True)
class DispatchDeps:
    """Everything a dispatch run talks to (fakes in tests).

    Attributes:
        store: Advisory persistence.
        channel: Notification channel (Web Push).
        synthesizer: Text-to-speech.
        audio_store: Private audio storage.
        publisher: Realtime broadcaster.
    """

    store: DispatchStore
    channel: NotificationChannel
    synthesizer: SpeechSynthesizer
    audio_store: AudioStore
    publisher: AdvisoryPublisher


class DispatchSummary(BaseModel):
    """What one dispatch run did.

    Attributes:
        considered: Queued advisories looked at.
        sent: Pushed to at least one device.
        failed: Claimed but no device accepted the push.
        expired: Reading too old; never sent.
        suppressed: Same alert pushed within the cooldown.
        blocked: Refused by the recipient guard.
        skipped: Already taken by another, overlapping run.
        stale_claims_failed: Claims from a crashed run, failed instead of re-sent.
        audio_missing: Sent or failed without a voice file (page uses speechSynthesis).
        sent_ids: Ids of the advisories pushed.
    """

    considered: int = 0
    sent: int = 0
    failed: int = 0
    expired: int = 0
    suppressed: int = 0
    blocked: int = 0
    skipped: int = 0
    stale_claims_failed: int = 0
    audio_missing: int = 0
    sent_ids: list[uuid.UUID] = Field(default_factory=list)


def is_urgent_low(value_mg_dl: Decimal) -> bool:
    """Tell whether a reading is Level 2 hypoglycaemia.

    Args:
        value_mg_dl: The reading.

    Returns:
        ``True`` below 54 mg/dL (ADA 2026 Table 6.4, ``clinical_limits.HYPO_LEVEL_2_MG_DL``).
    """
    return float(value_mg_dl) < HYPO_LEVEL_2_MG_DL


def push_ttl_seconds(measured_at: datetime, now: datetime, settings: Settings) -> int:
    """TTL for the push: the time left until the advisory would have expired.

    An alert that is still undelivered when its reading turns stale is useless, so the
    push service may drop it then. Never more than the default hour.

    Args:
        measured_at: When the reading was taken.
        now: Current time.
        settings: Supplies ``ADVISORY_MAX_AGE_MINUTES``.

    Returns:
        Seconds, between 60 and 3600.
    """
    remaining = timedelta(minutes=settings.advisory_max_age_minutes) - (now - measured_at)
    return int(min(max(remaining.total_seconds(), _MIN_TTL_SECONDS), DEFAULT_TTL_SECONDS))


def build_notification(advisory: QueuedAdvisory, *, ttl_seconds: int) -> Notification:
    """Build the Web Push notification for an advisory.

    Args:
        advisory: The advisory.
        ttl_seconds: Push TTL.

    Returns:
        The notification: reading and limit only, opening the advisory page.
    """
    urgent = is_urgent_low(advisory.reading_value_mg_dl)
    title, body = notification_text(
        state=advisory.state,
        context=advisory.context,
        value_mg_dl=advisory.reading_value_mg_dl,
        threshold_mg_dl=advisory.threshold_mg_dl,
        urgent_low=urgent,
    )
    return Notification(
        title=title,
        body=body,
        url=advisory_page_url(advisory.id),
        advisory_id=advisory.id,
        urgency=urgency_for(advisory.state, advisory.reading_value_mg_dl),
        ttl_seconds=ttl_seconds,
    )


def _make_audio(deps: DispatchDeps, advisory: QueuedAdvisory) -> str | None:
    """Synthesise and upload the voice advisory.

    Args:
        deps: Dependencies.
        advisory: The advisory.

    Returns:
        The storage path, or ``None`` if TTS or the upload failed (logged; the
        advisory page then speaks the text in the browser instead).
    """
    path = advisory_audio_path(advisory.user_id, advisory.id)
    try:
        audio = deps.synthesizer.synthesize(spoken_text(advisory.message_text), advisory.language)
        deps.audio_store.upload(path, audio)
    except (SpeechError, AudioStorageError) as exc:
        logger.warning("advisory %s: no voice audio (%s)", advisory.id, exc)
        return None
    return path


def _summarise_failure(report: DeliveryReport) -> tuple[str, str]:
    """Error code and text for a push no device accepted.

    Args:
        report: The channel's report.

    Returns:
        ``(code, text)``.
    """
    if report.attempted == 0:
        return "no_subscription", "patient has not enabled alerts on any device"
    text = "; ".join(report.errors) or "no device accepted the push"
    if report.revoked:
        text = f"{report.revoked} subscription(s) gone (revoked); {text}"
    return "push_failed", text


def _publish(deps: DispatchDeps, advisory: QueuedAdvisory) -> None:
    """Announce the advisory to the patient's open app, if they have a login.

    Args:
        deps: Dependencies.
        advisory: The advisory.
    """
    if advisory.auth_user_id is None:
        return
    try:
        deps.publisher.publish(
            auth_user_id=advisory.auth_user_id, advisory_id=advisory.id, state=advisory.state
        )
    except Exception:
        # Realtime is a convenience on top of the push: log, never fail the advisory.
        logger.exception("advisory %s: realtime broadcast failed", advisory.id)


def _send_claimed(
    deps: DispatchDeps,
    advisory: QueuedAdvisory,
    recipient: Recipient,
    *,
    now: datetime,
    clock: Callable[[], datetime],
    settings: Settings,
    summary: DispatchSummary,
) -> None:
    """Generate audio, push, record the outcome and broadcast, for a claimed advisory.

    Args:
        deps: Dependencies.
        advisory: The claimed advisory.
        recipient: Its recipient (already through the guard).
        now: Time of the freshness decision.
        clock: Current time, for the recorded timestamps.
        settings: Settings.
        summary: Updated in place.
    """
    audio_path = _make_audio(deps, advisory)
    if audio_path is None:
        summary.audio_missing += 1
    notification = build_notification(
        advisory, ttl_seconds=push_ttl_seconds(advisory.measured_at, now, settings)
    )
    try:
        report = deps.channel.send(recipient, notification)
    except NotificationDeliveryError as exc:
        code = "recipient_blocked" if isinstance(exc, RecipientBlockedError) else "push_error"
        deps.store.record_failed(
            advisory.id, at=clock(), code=code, error=str(exc), audio_path=audio_path
        )
        summary.failed += 1
        logger.warning("advisory %s: push failed (%s)", advisory.id, code)
    except Exception as exc:
        deps.store.record_failed(
            advisory.id,
            at=clock(),
            code="internal_error",
            error=type(exc).__name__,
            audio_path=audio_path,
        )
        summary.failed += 1
        logger.exception("advisory %s: unexpected error while pushing", advisory.id)
    else:
        if report.delivered:
            message_id = report.message_ids[0] if report.message_ids else None
            deps.store.record_sent(
                advisory.id, at=clock(), message_id=message_id, audio_path=audio_path
            )
            summary.sent += 1
            summary.sent_ids.append(advisory.id)
            logger.info(
                "advisory %s (%s) pushed to user %s, urgency %s",
                advisory.id,
                advisory.state,
                advisory.user_id,
                notification.urgency,
            )
        else:
            code, error = _summarise_failure(report)
            deps.store.record_failed(
                advisory.id, at=clock(), code=code, error=error, audio_path=audio_path
            )
            summary.failed += 1
            logger.warning("advisory %s: not delivered (%s)", advisory.id, code)
    _publish(deps, advisory)


def dispatch_advisories(
    deps: DispatchDeps,
    *,
    settings: Settings,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    user_ids: Collection[uuid.UUID] | None = None,
) -> DispatchSummary:
    """Deliver every queued advisory once (rules in the module docstring).

    Args:
        deps: Store, channel, TTS, audio storage and Realtime.
        settings: Cooldown and expiry settings.
        clock: Current time (tests pass a fixed clock).
        user_ids: Only these patients (tests, the demo trigger), or everyone.

    Returns:
        Counts of what happened.
    """
    summary = DispatchSummary()
    now = clock()
    summary.stale_claims_failed = deps.store.fail_stale_claims(
        claimed_before=now - STALE_CLAIM_AFTER, at=now
    )
    if summary.stale_claims_failed:
        logger.error(
            "%d advisories left claimed by a crashed run: failed", summary.stale_claims_failed
        )
    cooldown = timedelta(hours=settings.advisory_cooldown_hours)

    for advisory in deps.store.queued(user_ids):
        summary.considered += 1
        now = clock()
        # 1. Freshness (R3): an alert about an old reading cannot help any more.
        if is_expired(advisory.measured_at, now, settings):
            summary.expired += deps.store.expire(advisory.id)
            continue
        # 2. Recipient guard before any TTS, storage or network work.
        recipient = Recipient(
            user_id=advisory.user_id,
            is_demo=advisory.is_demo,
            is_linked=advisory.auth_user_id is not None,
        )
        try:
            check_recipient(recipient)
        except RecipientBlockedError as exc:
            summary.blocked += deps.store.block(advisory.id, now, str(exc))
            continue
        # 3. Cooldown at send time (R3), except Level 2 hypoglycaemia (< 54 mg/dL),
        # which is always delivered: each one is clinically significant.
        if not is_urgent_low(advisory.reading_value_mg_dl):
            last = deps.store.last_push_at(
                user_id=advisory.user_id,
                limit_kind=advisory.limit_kind,
                state=advisory.state,
                exclude_id=advisory.id,
            )
            if last is not None and now - last < cooldown:
                summary.suppressed += deps.store.suppress(advisory.id)
                logger.info(
                    "advisory %s suppressed: %s already pushed at %s",
                    advisory.id,
                    advisory.state,
                    last.isoformat(),
                )
                continue
        # 4. Claim: exactly one run gets past this line for a given advisory.
        if not deps.store.claim(advisory.id, now):
            summary.skipped += 1
            continue
        _send_claimed(
            deps, advisory, recipient, now=now, clock=clock, settings=settings, summary=summary
        )

    logger.info(
        "dispatch: %d considered, %d sent, %d failed, %d expired, %d suppressed, "
        "%d blocked, %d skipped",
        summary.considered,
        summary.sent,
        summary.failed,
        summary.expired,
        summary.suppressed,
        summary.blocked,
        summary.skipped,
    )
    return summary


# ---------------------------------------------------------------------------
# Production wiring
# ---------------------------------------------------------------------------

# One dispatch at a time per process: the scheduler's own job and the post-batch call
# could otherwise overlap. Claims keep overlapping runs safe anyway; this just avoids
# the wasted work.
_RUN_LOCK = threading.Lock()


def build_dispatch_deps(session_factory: Callable[[], Session], settings: Settings) -> DispatchDeps:
    """Wire the real store, Web Push, gTTS, Supabase Storage and Realtime.

    Args:
        session_factory: Database sessions.
        settings: Settings (VAPID keys, Supabase URL and key, bucket).

    Returns:
        The dependencies.

    Raises:
        ConfigurationError: If the VAPID keys are not configured.
    """
    sender = PywebpushSender(settings.require_vapid())
    return DispatchDeps(
        store=SqlDispatchStore(session_factory),
        channel=WebPushChannel(SqlSubscriptionStore(session_factory), sender),
        synthesizer=GttsSynthesizer(),
        audio_store=SupabaseAudioStore(settings),
        publisher=HttpRealtimePublisher(settings),
    )


def run_dispatch(
    *,
    settings: Settings | None = None,
    session_factory: Callable[[], Session] | None = None,
    user_ids: Collection[uuid.UUID] | None = None,
) -> DispatchSummary | None:
    """Run one dispatch with the production dependencies.

    Args:
        settings: Defaults to :func:`get_settings`.
        session_factory: Defaults to the app's session factory.
        user_ids: Only these patients, or everyone.

    Returns:
        The summary, or ``None`` if another dispatch in this process is running.

    Raises:
        ConfigurationError: If the VAPID keys are not configured.
    """
    settings = settings or get_settings()
    if session_factory is None:
        from app.db.session import get_session_factory  # noqa: PLC0415 - lazy engine

        session_factory = get_session_factory()
    if not _RUN_LOCK.acquire(blocking=False):
        logger.info("dispatch skipped: another dispatch is running in this process")
        return None
    try:
        deps = build_dispatch_deps(session_factory, settings)
        return dispatch_advisories(deps, settings=settings, user_ids=user_ids)
    finally:
        _RUN_LOCK.release()


def scheduled_dispatch() -> None:
    """APScheduler / post-batch entry point: one dispatch, never raising."""
    try:
        run_dispatch()
    except ConfigurationError as exc:
        logger.warning("advisory dispatch skipped: %s", exc)
    except Exception:
        logger.exception("advisory dispatch failed")


def main(argv: list[str] | None = None) -> int:
    """Run one dispatch from the command line.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        0 on success, 1 if VAPID is not configured or a dispatch is already running.
    """
    parser = argparse.ArgumentParser(description="Deliver queued advisories once (R3).")
    parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    try:
        summary = run_dispatch()
    except ConfigurationError as exc:
        logger.error("dispatch failed: %s", exc)
        return 1
    if summary is None:
        return 1
    print(summary.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
