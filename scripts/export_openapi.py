"""Write the API's OpenAPI schema to ``web/openapi.json`` for TypeScript type generation.

The public server keeps ``/openapi.json`` off (``EXPOSE_API_DOCS``), so the React apps
generate their types from this local file instead: ``npm run gen:api`` in ``web/``
runs this script and then ``openapi-typescript`` into ``web/src/shared/api/types.ts``.

Run with ``uv run python -m scripts.export_openapi``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final

from app.main import create_app

logger = logging.getLogger(__name__)

OUT: Final = Path(__file__).resolve().parents[1] / "web" / "openapi.json"


def export(out: Path = OUT) -> Path:
    """Build the app (docs routes off) and write its OpenAPI schema.

    Args:
        out: Destination file.

    Returns:
        The path written.
    """
    schema = create_app(expose_docs=False).openapi()
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("wrote %s", export())
