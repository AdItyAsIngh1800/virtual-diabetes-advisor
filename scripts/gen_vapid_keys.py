"""Generate a VAPID key pair for Web Push (RFC 8292) and print it as ``.env`` lines.

Usage::

    uv run python -m scripts.gen_vapid_keys --subject mailto:you@example.com
    uv run python -m scripts.gen_vapid_keys --check   # verify the keys in .env pair up

Paste the three printed lines into ``.env`` (never commit them) and restart the API.
Generating new keys invalidates every existing browser subscription: each device must
press "Enable alerts" again (the patient page does this automatically).
"""

from __future__ import annotations

import argparse
import base64
import logging
import re
import sys
from typing import NamedTuple

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core.config import VAPID_SUBJECT_PATTERN, get_settings
from app.core.exceptions import ConfigurationError
from app.core.logging import configure_logging

logger = logging.getLogger(__name__)

# P-256 private scalars are 32 bytes (RFC 8292 section 3.2 uses ECDSA P-256).
_PRIVATE_LEN = 32


class VapidKeyPair(NamedTuple):
    """Base64url (unpadded) VAPID keys.

    Attributes:
        public_key: Uncompressed P-256 point, 65 bytes (browser applicationServerKey).
        private_key: Raw 32-byte private scalar.
    """

    public_key: str
    private_key: str


def _b64url(data: bytes) -> str:
    """Encode bytes as unpadded base64url.

    Args:
        data: Raw bytes.

    Returns:
        The encoded text.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _public_from_private(private_key: ec.EllipticCurvePrivateKey) -> str:
    """Encode the public half of a key the way browsers expect it.

    Args:
        private_key: P-256 private key.

    Returns:
        Base64url uncompressed point.
    """
    point = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return _b64url(point)


def generate_vapid_keys() -> VapidKeyPair:
    """Create a fresh P-256 key pair.

    Returns:
        The keys, base64url encoded.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    scalar = private_key.private_numbers().private_value.to_bytes(_PRIVATE_LEN, "big")
    return VapidKeyPair(public_key=_public_from_private(private_key), private_key=_b64url(scalar))


def public_key_for(private_key_b64: str) -> str:
    """Derive the public key from a base64url raw private key.

    Args:
        private_key_b64: ``VAPID_PRIVATE_KEY`` value.

    Returns:
        The matching ``VAPID_PUBLIC_KEY``.
    """
    raw = base64.urlsafe_b64decode(private_key_b64 + "=" * (-len(private_key_b64) % 4))
    key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    return _public_from_private(key)


def vapid_subject(value: str) -> str:
    """Validate a ``--subject`` the way py_vapid will (same rule as ``VAPID_SUBJECT``).

    Args:
        value: The ``--subject`` argument.

    Returns:
        The subject.

    Raises:
        argparse.ArgumentTypeError: If it is not ``mailto:user@domain`` or ``https://host``.
    """
    if re.fullmatch(VAPID_SUBJECT_PATTERN, value, re.IGNORECASE) is None:
        raise argparse.ArgumentTypeError(
            "must be mailto:user@domain or https://host (no path or port)"
        )
    return value


def main(argv: list[str] | None = None) -> int:
    """Print new keys, or check the configured ones.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument(
        "--subject",
        default="mailto:you@example.com",
        type=vapid_subject,
        help="VAPID contact sent to push services: mailto:user@domain or https://host",
    )
    parser.add_argument(
        "--check", action="store_true", help="verify the VAPID_* keys in .env form a pair"
    )
    args = parser.parse_args(argv)

    if args.check:
        settings = get_settings()
        configure_logging(settings.log_level)
        try:
            vapid = settings.require_vapid()
        except ConfigurationError as exc:
            logger.error("%s", exc)
            return 1
        if public_key_for(vapid.private_key.get_secret_value()) != vapid.public_key:
            logger.error("VAPID_PUBLIC_KEY does not belong to VAPID_PRIVATE_KEY")
            return 1
        logger.info("VAPID keys are valid and match (subject %s)", vapid.subject)
        return 0

    keys = generate_vapid_keys()
    # Printed, not written to .env: this script never edits files holding secrets.
    sys.stdout.write(
        f"VAPID_PUBLIC_KEY={keys.public_key}\n"
        f"VAPID_PRIVATE_KEY={keys.private_key}\n"
        f"VAPID_SUBJECT={args.subject}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
