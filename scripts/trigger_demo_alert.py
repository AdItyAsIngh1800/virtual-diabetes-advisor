"""Make a real advisory reach your phone: fresh reading -> batch run -> dispatch (R1-R3 demo).

Usage::

    uv run python -m scripts.trigger_demo_alert                        # pre_breach_high
    uv run python -m scripts.trigger_demo_alert --level breach_high
    uv run python -m scripts.trigger_demo_alert --level below_54

Targets the one demo patient linked to your login (``seed_demo --my-email``; Meera
in the seeded cohort). Steps:

1. Dispatch first, so her old queued seed advisories expire (an expired, never-sent
   advisory does not start a cooldown).
2. Insert a fasting reading measured *now*, placed against her current personal
   limits: mid pre-breach band, 10 mg/dL over the upper limit, or 50 mg/dL (< 54,
   ADA Level 2 hypoglycaemia).
3. Run the batch update (all patients - a single-patient run would move every
   patient's batch window past their unprocessed readings).
4. Dispatch her new advisory: gTTS voice -> Storage, Web Push, Realtime.

The R3 cooldown still applies: a second ``pre_breach_high`` (or ``breach_high``)
within ``ADVISORY_COOLDOWN_HOURS`` is suppressed and the script says so. ``below_54``
always goes through. Exit code 0 only if the advisory was pushed to a device.
"""

from __future__ import annotations

import argparse
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, Literal, get_args

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clinical_limits import GlucoseLimitKind
from app.core.config import Settings, get_settings
from app.core.exceptions import BatchAlreadyRunningError, ConfigurationError
from app.core.logging import configure_logging
from app.db.models import Advisory, Profile, Reading, User
from app.db.threshold_repository import SqlThresholdRepository
from app.jobs.batch_update import BatchRunSummary, load_risk_scorer, run_batch_update
from app.jobs.dispatch_advisories import DispatchSummary, run_dispatch

logger = logging.getLogger(__name__)

Level = Literal["pre_breach_high", "breach_high", "below_54"]
LEVELS: Final[tuple[Level, ...]] = get_args(Level)

# Demo values, not clinical guidance. breach_high: comfortably over the limit so a trend
# revision during the batch run cannot pull it back under. below_54: under ADA 2026
# Level 2 hypoglycaemia (clinical_limits.HYPO_LEVEL_2_MG_DL = 54).
_BREACH_MARGIN_MG_DL: Final = Decimal("10")
_BELOW_54_MG_DL: Final = Decimal("50.0")
_TENTH: Final = Decimal("0.1")
# A scheduled batch run may hold the run lock; wait for it rather than give up.
_BATCH_RETRIES: Final = 12
_BATCH_RETRY_WAIT_S: Final = 5.0


class DemoAlertError(Exception):
    """Raised when the demo alert cannot be set up (with the fix in the message)."""


def reading_value_for(
    level: Level, thresholds: Mapping[GlucoseLimitKind, Decimal], band: float
) -> Decimal:
    """Pick a fasting reading that lands in the requested band for these limits.

    Args:
        level: Requested advisory level.
        thresholds: Current personal limits (needs ``fasting_high`` for high levels).
        band: ``PRE_BREACH_BAND`` fraction.

    Returns:
        The reading in mg/dL, one decimal place.

    Raises:
        DemoAlertError: If the needed limit has not been computed yet.
    """
    if level == "below_54":
        return _BELOW_54_MG_DL
    upper = thresholds.get("fasting_high")
    if upper is None:
        raise DemoAlertError("no fasting limit yet: run uv run python -m app.jobs.batch_update")
    if level == "breach_high":
        return (upper + _BREACH_MARGIN_MG_DL).quantize(_TENTH, ROUND_HALF_EVEN)
    # Middle of the pre-breach band [(1 - b) * U, U): the safest spot inside it.
    middle = upper * (Decimal(1) - Decimal(str(band)) / 2)
    return middle.quantize(_TENTH, ROUND_HALF_EVEN)


def find_linked_demo_patient(session: Session) -> tuple[User, str | None]:
    """Return the demo patient linked to a login, and their name.

    Args:
        session: Open session.

    Returns:
        ``(user, full_name)``.

    Raises:
        DemoAlertError: If none or several demo patients are linked.
    """
    rows = session.execute(
        select(User, Profile.full_name)
        .join(Profile, Profile.user_id == User.id)
        .where(User.is_demo, User.auth_user_id.is_not(None))
    ).all()
    if not rows:
        raise DemoAlertError(
            "no demo patient is linked to your login: run "
            "uv run python -m scripts.seed_demo --my-email <your email>"
        )
    if len(rows) > 1:
        raise DemoAlertError("several demo patients are linked to logins; re-run seed_demo")
    user, name = rows[0]
    return user, name


def current_limits(session: Session, user_id: uuid.UUID) -> dict[GlucoseLimitKind, Decimal]:
    """Read the patient's current personal limits.

    Args:
        session: Open session.
        user_id: Patient.

    Returns:
        Limit value per kind (empty before the first batch run).
    """
    stored = SqlThresholdRepository(session).current_thresholds(user_id)
    return {kind: t.threshold_mg_dl for kind, t in stored.items()}


def insert_reading(session: Session, user_id: uuid.UUID, value: Decimal) -> uuid.UUID:
    """Store a fasting reading measured now (database clock).

    Args:
        session: Open session (caller commits).
        user_id: Patient.
        value: Reading in mg/dL.

    Returns:
        The reading id.
    """
    now = session.scalar(select(func.now()))
    if not isinstance(now, datetime):  # pragma: no cover - now() is never NULL
        raise DemoAlertError("database returned no now()")
    reading = Reading(
        user_id=user_id, context="fasting", value_mg_dl=value, measured_at=now, source="api"
    )
    session.add(reading)
    session.flush()
    return reading.id


def run_batch_with_retry(
    factory: Callable[[], Session],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> BatchRunSummary:
    """Run the batch update, waiting while a scheduled run holds the lock.

    Args:
        factory: Session factory.
        sleep: Sleep function (tests pass a no-op).

    Returns:
        The run summary.

    Raises:
        DemoAlertError: If another run kept the lock for the whole wait.
    """
    for _ in range(_BATCH_RETRIES):
        try:
            return run_batch_update(factory, scorer=load_risk_scorer(), triggered_by="manual")
        except BatchAlreadyRunningError:
            logger.info("a batch run is in progress; waiting %.0fs", _BATCH_RETRY_WAIT_S)
            sleep(_BATCH_RETRY_WAIT_S)
    raise DemoAlertError("another batch run kept running; try again in a minute")


def _dispatch(user_id: uuid.UUID) -> DispatchSummary:
    """Dispatch the patient's queued advisories.

    Args:
        user_id: Patient.

    Returns:
        The summary.

    Raises:
        DemoAlertError: If a dispatch is already running in this process (never here).
    """
    summary = run_dispatch(user_ids=[user_id])
    if summary is None:  # pragma: no cover - fresh process, the lock is free
        raise DemoAlertError("a dispatch is already running")
    return summary


def _advisory_page(settings: Settings, advisory_id: uuid.UUID) -> str:
    """URL of the advisory page, absolute when PUBLIC_BASE_URL is set.

    Args:
        settings: Settings.
        advisory_id: Advisory.

    Returns:
        The URL or path.
    """
    path = f"/app/advisory/{advisory_id}"
    try:
        return settings.require_public_base_url() + path
    except ConfigurationError:
        return path


def trigger(level: Level, factory: Callable[[], Session], settings: Settings) -> int:
    """Run the whole demo (steps in the module docstring) and print the outcome.

    Args:
        level: Advisory level to provoke.
        factory: Session factory.
        settings: Settings.

    Returns:
        0 if the advisory was pushed, 1 otherwise.

    Raises:
        DemoAlertError: On a setup problem (no linked patient, no limits, busy batch).
        ConfigurationError: If the VAPID keys are missing.
    """
    with factory() as session:
        patient, name = find_linked_demo_patient(session)
    who = f"{name or 'demo patient'} ({patient.id})"
    expired = _dispatch(patient.id).expired
    if expired:
        print(f"Expired {expired} old queued advisories for {who}.")

    with factory() as session:
        limits = current_limits(session, patient.id)
    if not limits:
        run_batch_with_retry(factory)
        with factory() as session:
            limits = current_limits(session, patient.id)
    value = reading_value_for(level, limits, settings.pre_breach_band)
    with factory() as session:
        reading_id = insert_reading(session, patient.id, value)
        session.commit()
    print(
        f"Inserted fasting reading {value} mg/dL for {who} "
        f"(fasting limit {limits.get('fasting_high')}, low limit {limits.get('low')})."
    )

    batch = run_batch_with_retry(factory)
    with factory() as session:
        created = session.scalar(select(Advisory).where(Advisory.reading_id == reading_id))
    if created is None:
        print(
            f"Batch run {batch.run_id}: no advisory for this reading. The R3 cooldown "
            f"({settings.advisory_cooldown_hours} h) suppressed it, or the refreshed limit "
            "moved. Try --level below_54 (never suppressed) or another level."
        )
        return 1
    print(f"Batch run {batch.run_id}: advisory {created.id} ({created.state}) queued.")

    summary = _dispatch(patient.id)
    with factory() as session:
        final = session.get(Advisory, created.id)
    status = final.delivery_status if final else "unknown"
    print(f"Dispatch: advisory is '{status}'", end="")
    if final is not None and final.delivery_error:
        print(f" - {final.delivery_error_code}: {final.delivery_error}", end="")
    print(
        f". Voice file: {'yes' if final and final.audio_path else 'no (browser voice fallback)'}."
    )
    print(f"Open: {_advisory_page(settings, created.id)}")
    return 0 if created.id in summary.sent_ids else 1


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--level", choices=LEVELS, default="pre_breach_high")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    from app.db.session import get_session_factory  # noqa: PLC0415 - CLI-only import

    try:
        return trigger(args.level, get_session_factory(), settings)
    except (DemoAlertError, ConfigurationError) as exc:
        logger.error("demo alert failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
