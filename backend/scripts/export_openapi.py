"""Write the backend's OpenAPI schema to frontend/openapi.json (input for openapi-typescript).

uv run python -m scripts.export_openapi
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.main import create_app

OUT = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"


def main() -> None:
    settings = Settings(
        _env_file=None, database_url="postgresql://unused/x", backend_api_tokens="x"
    )  # type: ignore[call-arg]
    schema = create_app(settings).openapi()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(schema['paths'])} paths)")


if __name__ == "__main__":
    main()
