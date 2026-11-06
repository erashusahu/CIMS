"""Export the FastAPI contract locally without starting a server or querying PostgreSQL."""

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "backend"))

from app.main import app

destination = root / "frontend" / "src" / "api" / "openapi.json"
destination.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
print(f"Exported API contract to {destination.name}")
