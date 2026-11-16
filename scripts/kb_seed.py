"""Seed knowledge base for the help chat: diabetes basics, each with its source (R4/R5).

:func:`seed_kb` writes :data:`KB_SEED` as **approved** ``kb_entries`` rows, recording the
approving practitioner (``approved_by``) and time. The database refuses an approved row
without them, so nothing reaches patients unless a named practitioner approved it.

The entries were drafted by the project team from the cited sources, in plain language,
and are approved by the demo practitioner whose login ``seed_demo --practitioner-email``
links. That person should read them before a real demo; a reviewed entry that needs
changes is edited here and re-seeded, which retires the old row and approves the new
text as a new ``kb_version`` (approved rows are never edited in place).

Idempotent: unchanged entries are left alone; only new or changed ones create a new
KB version.

Sources (abbreviations used below; ADA = American Diabetes Association):

    ADA-2/3/5/6/12: ADA Professional Practice Committee, Standards of Care in
        Diabetes-2026, Diabetes Care 2026;49(Suppl. 1), sections 2 (Diagnosis and
        Classification), 3 (Prevention or Delay of Diabetes), 5 (Facilitating Positive
        Health Behaviors), 6 (Glycemic Goals, Hypoglycemia, and Hyperglycemic Crises),
        12 (Retinopathy, Neuropathy, and Foot Care).
    ADA-hypo / ADA-hyper / ADA-plate / ADA-sick / ADA-foot: patient pages on
        diabetes.org ("Hypoglycemia (Low Blood Glucose)", "Hyperglycemia (High Blood
        Glucose)", "Diabetes Plate Method", "Sick Days", "Foot Complications").
    ADA-driving: ADA position statement "Diabetes and Driving", Diabetes Care
        2014;37(Suppl. 1):S97-S103.
    ICMR-NIN: ICMR-National Institute of Nutrition, "Dietary Guidelines for Indians",
        Hyderabad, 2024 (whole grains and millets over refined cereals; limit added
        sugar, including jaggery and honey). Used for the Indian foods in the
        healthy-eating entry.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import KbEntry

# Seeded entries share this key prefix, so a re-seed only ever retires its own rows.
SEED_PREFIX: Final = "seed:"


class KbSeedEntry(BaseModel):
    """One seed entry.

    Attributes:
        key: Stable key (``entry_key`` without the prefix).
        title: Title, cited in chat replies.
        body: Plain-language text a patient reads (or hears).
        source: Citation stored in ``source_citation``.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=5, max_length=120)
    body: str = Field(min_length=80, max_length=1200)
    source: str = Field(min_length=10)


KB_SEED: Final[tuple[KbSeedEntry, ...]] = (
    KbSeedEntry(
        key="pre-breach-alert",
        title="What a pre-breach alert means",
        body=(
            "A pre-breach alert means your glucose reading is close to your personal limit "
            "(within 5 percent of it) but has not crossed it. It is an early warning, not an "
            "emergency. Keep to your usual meals and activity and check again at your next "
            "planned time. If your readings keep moving toward or past your limit, contact "
            "your doctor."
        ),
        source="Virtual Diabetes Advisor alert design (R3 pre-breach band); limits from ADA-6",
    ),
    KbSeedEntry(
        key="personal-limits",
        title="Your personal glucose limits",
        body=(
            "Your limits start from the ADA targets for many adults with diabetes: 80 to "
            "130 mg/dL before meals (fasting) and below 180 mg/dL after meals. The app "
            "adjusts them a little for your age, health and recent trend, always within safe "
            "bounds taken from the same guidelines. If your doctor has set different goals "
            "for you, follow your doctor's goals."
        ),
        source="ADA Standards of Care in Diabetes-2026, Section 6, Table 6.3",
    ),
    KbSeedEntry(
        key="low-glucose-signs",
        title="Signs of low blood glucose (hypoglycaemia)",
        body=(
            "Low glucose is a reading below 70 mg/dL. Common signs are shakiness, sweating, "
            "a fast heartbeat, hunger, feeling anxious or irritable, dizziness, headache, "
            "blurred vision and confusion. Some people do not feel any signs, which is why "
            "checking matters. A reading below 54 mg/dL is a serious low that needs action "
            "straight away."
        ),
        source=(
            "ADA Standards of Care in Diabetes-2026, Section 6, Table 6.4; ADA, "
            "'Hypoglycemia (Low Blood Glucose)', diabetes.org"
        ),
    ),
    KbSeedEntry(
        key="rule-15-15",
        title="Treating low glucose (hypoglycaemia): the 15-15 rule",
        body=(
            "The American Diabetes Association advises the 15-15 rule when your glucose is "
            "below 70 mg/dL and you are awake and able to swallow: have about 15 grams of "
            "fast-acting sugar. Glucose tablets or gel are best; half a cup of fruit juice "
            "or regular (not diet) soft drink, or one tablespoon of sugar or honey, also "
            "work. Check again after 15 minutes. If it is still below 70 mg/dL, repeat. If "
            "someone is confused or cannot swallow safely, call emergency services."
        ),
        source=(
            "ADA Standards of Care in Diabetes-2026, Section 6, Recommendation 6.15; ADA, "
            "'Hypoglycemia (Low Blood Glucose)' (the 15-15 rule), diabetes.org"
        ),
    ),
    KbSeedEntry(
        key="high-glucose-signs",
        title="Signs of high blood glucose (hyperglycaemia)",
        body=(
            "Signs of high glucose include feeling very thirsty, passing urine often, "
            "tiredness, blurred vision and headaches. Readings above 250 mg/dL are very "
            "high. If you are vomiting, breathing fast, very drowsy or confused, get "
            "emergency help: these can be signs of a hyperglycaemic crisis such as diabetic "
            "ketoacidosis. Otherwise, contact your doctor if your readings stay high."
        ),
        source=(
            "ADA, 'Hyperglycemia (High Blood Glucose)', diabetes.org; ADA Standards of Care "
            "in Diabetes-2026, Section 6 (Table 6.2, hyperglycemic crises)"
        ),
    ),
    KbSeedEntry(
        key="when-to-see-doctor",
        title="When to contact your doctor",
        body=(
            "Contact your doctor if your readings keep rising or keep falling over several "
            "days, if you have lows often or without a clear reason, if you are ill and "
            "your readings stay high, or if you notice a new wound or colour change on your "
            "feet or a change in your eyesight. Get emergency help straight away for chest "
            "pain, trouble breathing, confusion, fainting, or vomiting with very high "
            "readings."
        ),
        source=(
            "ADA Standards of Care in Diabetes-2026, Sections 6 and 12; reviewed by the "
            "approving practitioner"
        ),
    ),
    KbSeedEntry(
        key="physical-activity",
        title="Physical activity and exercise",
        body=(
            "Most adults with diabetes should aim for at least 150 minutes a week of "
            "moderate activity such as brisk walking, spread over at least 3 days, with no "
            "more than 2 days in a row without activity. Add 2 to 3 sessions a week of "
            "strength exercise on days that are not back to back, and break up long periods "
            "of sitting about every 30 minutes. If your medicines can cause lows, check your "
            "glucose before and after exercise and carry a fast-acting snack."
        ),
        source="ADA Standards of Care in Diabetes-2026, Section 5 (physical activity)",
    ),
    KbSeedEntry(
        key="healthy-eating",
        title="Healthy eating with diabetes",
        body=(
            "A simple guide is the Diabetes Plate Method: fill half your plate with "
            "non-starchy vegetables, a quarter with lean protein such as dal, eggs, fish or "
            "chicken, and a quarter with carbohydrate foods, preferably whole grains. Drink "
            "water instead of sugary drinks, eat fewer sweets and refined foods, and try to "
            "eat at regular times. Indian meals fit the same plate: rice, roti, chapati, "
            "idli or dosa go in the carbohydrate quarter, and whole wheat, millets or "
            "brown rice are better than white rice or maida. Sweets, jaggery and honey "
            "raise glucose like sugar, so keep them small and occasional. Eat whole fruit, "
            "including mango, in small portions rather than juice. A dietitian can help "
            "you make a plan that fits your food and culture."
        ),
        source=(
            "ADA, 'Diabetes Plate Method', diabetes.org; ADA Standards of Care in "
            "Diabetes-2026, Section 5 (nutrition therapy); ICMR-NIN, Dietary Guidelines "
            "for Indians, 2024"
        ),
    ),
    KbSeedEntry(
        key="foot-care",
        title="Looking after your feet",
        body=(
            "Diabetes can reduce feeling in the feet, so small injuries go unnoticed. Check "
            "your feet every day for cuts, blisters, redness or swelling. Wash and dry them "
            "well, especially between the toes. Wear well-fitting shoes and socks and do not "
            "walk barefoot. Have your feet examined by a doctor at least once a year, and "
            "see a doctor the same day for a wound that is not healing, a colour change, or "
            "a foot that is hot and swollen."
        ),
        source=(
            "ADA Standards of Care in Diabetes-2026, Section 12 (foot care); ADA, 'Foot "
            "Complications', diabetes.org"
        ),
    ),
    KbSeedEntry(
        key="checking-glucose",
        title="How to check your glucose and report it",
        body=(
            "A fasting reading is taken after at least 8 hours without food, usually before "
            "breakfast. An after-meal reading is taken 1 to 2 hours after you start eating. "
            "Wash and dry your hands before using your meter. You can report a reading here "
            "by typing or saying it with its timing, for example 'fasting 132' or '190 after "
            "lunch', and the app will compare it with your personal limits."
        ),
        source="ADA Standards of Care in Diabetes-2026, Sections 2 and 6",
    ),
    KbSeedEntry(
        key="sick-days",
        title="Managing diabetes on sick days",
        body=(
            "Illness can make glucose go up, even if you eat less. On sick days, keep "
            "drinking fluids, check your glucose more often, and try to eat small amounts. "
            "Do not stop or change your diabetes medicines on your own: ask your doctor what "
            "to do when you are ill. Contact your doctor if you cannot keep food or fluids "
            "down, or if your readings stay high."
        ),
        source="ADA, 'Sick Days', diabetes.org; ADA Standards of Care in Diabetes-2026",
    ),
    KbSeedEntry(
        key="a1c",
        title="What A1C means",
        body=(
            "A1C is a blood test that shows your average glucose over about the past 3 "
            "months. For many adults with diabetes the goal is below 7 percent, but your "
            "doctor may set a different goal for you. It is usually checked at least twice "
            "a year when you are meeting your goals, and more often when treatment changes "
            "or you are not."
        ),
        source="ADA Standards of Care in Diabetes-2026, Section 6 (A1C goals and testing)",
    ),
    KbSeedEntry(
        key="alcohol",
        title="Alcohol and diabetes",
        body=(
            "Alcohol can make glucose drop, sometimes hours after drinking, especially if "
            "you take medicines that can cause lows. If you drink, do so in moderation and "
            "with food, never on an empty stomach, and check your glucose before bed. Signs "
            "of a low can look like being drunk, so tell the people you are with that you "
            "have diabetes."
        ),
        source="ADA Standards of Care in Diabetes-2026, Section 5 (alcohol)",
    ),
    KbSeedEntry(
        key="prediabetes",
        title="What prediabetes means",
        body=(
            "Prediabetes means glucose is higher than normal but not yet in the diabetes "
            "range: a fasting glucose of 100 to 125 mg/dL, or an A1C of 5.7 to 6.4 percent. "
            "Many people can delay or prevent type 2 diabetes by losing about 7 percent of "
            "their body weight if overweight and being active for at least 150 minutes a "
            "week."
        ),
        source=(
            "ADA Standards of Care in Diabetes-2026, Section 2 (Table 2.2) and Section 3 "
            "(prevention)"
        ),
    ),
    KbSeedEntry(
        key="driving",
        title="Driving and low glucose",
        body=(
            "If your medicines can cause lows, check your glucose before you drive. If it is "
            "below 70 mg/dL, do not drive: treat the low first and wait until you feel well "
            "and your reading is back in your range. Keep a fast-acting snack in the car, and "
            "stop driving at once if you feel any signs of a low."
        ),
        source=(
            "ADA position statement 'Diabetes and Driving', Diabetes Care 2014;37(Suppl. "
            "1):S97-S103"
        ),
    ),
    KbSeedEntry(
        key="using-this-chat",
        title="How this chat can help",
        body=(
            "You can type or say a glucose reading with its timing and the app compares it "
            "with your personal limits. You can ask questions about diabetes, and the app "
            "answers only from information approved by your care team. You can type 'talk "
            "to a doctor' to reach a practitioner. This chat is not for emergencies: for "
            "chest pain, trouble breathing, confusion or fainting, call 112."
        ),
        source="Virtual Diabetes Advisor (this app)",
    ),
)


class KbSeedReport(BaseModel):
    """What :func:`seed_kb` did.

    Attributes:
        kb_version: KB version the new rows were approved into (``None`` if none).
        inserted: New or changed entries approved now.
        unchanged: Entries already approved with the same text.
        retired: Earlier rows retired (superseded or removed from the seed).
    """

    kb_version: int | None
    inserted: int
    unchanged: int
    retired: int


def seed_kb(
    session: Session,
    *,
    approver_id: uuid.UUID,
    now: datetime,
    entries: tuple[KbSeedEntry, ...] = KB_SEED,
) -> KbSeedReport:
    """Approve the seed entries, versioning only what changed (caller commits).

    Args:
        session: Open session.
        approver_id: ``practitioners.id`` recorded as ``approved_by``.
        now: ``approved_at``.
        entries: Entries to seed.

    Returns:
        Counts and the new KB version.
    """
    approved = {
        row.entry_key: row
        for row in session.scalars(
            select(KbEntry).where(
                KbEntry.status == "approved", KbEntry.entry_key.startswith(SEED_PREFIX)
            )
        )
    }
    wanted = {SEED_PREFIX + e.key: e for e in entries}
    changed = [
        (key, entry)
        for key, entry in wanted.items()
        if key not in approved
        or (approved[key].title, approved[key].body, approved[key].source_citation)
        != (entry.title, entry.body, entry.source)
    ]
    removed = [row for key, row in approved.items() if key not in wanted]
    if not changed and not removed:
        return KbSeedReport(kb_version=None, inserted=0, unchanged=len(wanted), retired=0)

    latest = session.scalar(select(func.max(KbEntry.kb_version))) or 0
    version = latest + 1
    retired = 0
    for row in removed:
        row.status = "retired"
        retired += 1
    for key, entry in changed:
        old = approved.get(key)
        if old is not None:
            # Approved rows are never edited in place: retire, then approve the new text.
            old.status = "retired"
            retired += 1
        session.flush()
        session.add(
            KbEntry(
                id=uuid.uuid4(),
                entry_key=key,
                title=entry.title,
                body=entry.body,
                language="en",
                status="approved",
                kb_version=version,
                source_citation=entry.source,
                origin="manual",
                supersedes_id=old.id if old is not None else None,
                approved_by=approver_id,
                approved_at=now,
            )
        )
    session.flush()
    return KbSeedReport(
        kb_version=version,
        inserted=len(changed),
        unchanged=len(wanted) - len(changed),
        retired=retired,
    )
