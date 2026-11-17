"""A count shown with its denominator and rate (R5 audit summary and model card).

The Audit tab never shows a rate on its own: ``0.5`` could be 1 of 2 or 500 of 1000.
Every rate the API returns is a :class:`CountRate`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CountRate(BaseModel):
    """A count, what it is out of, and the rate.

    Attributes:
        count: Numerator.
        denominator: What it is out of.
        rate: ``count / denominator``, or ``None`` when the denominator is 0.
    """

    model_config = ConfigDict(frozen=True)

    count: int
    denominator: int
    rate: float | None

    @classmethod
    def of(cls, count: int, denominator: int) -> CountRate:
        """Build one, guarding against a zero denominator.

        Args:
            count: Numerator.
            denominator: Denominator.

        Returns:
            The count, denominator and rate (``None`` when ``denominator`` is 0).
        """
        return cls(
            count=count,
            denominator=denominator,
            rate=count / denominator if denominator else None,
        )
