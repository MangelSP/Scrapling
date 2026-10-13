"""Genera rest_api/openapi.json (python scripts/export_openapi.py [--check])."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

OUT = ROOT / "openapi.json"


def render() -> str:
    app = create_app(Settings(enable_docs=True))
    return json.dumps(app.openapi(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        sys.exit(0 if OUT.read_text(encoding="utf-8") == text else "openapi.json desactualizado: ejecuta scripts/export_openapi.py")
    OUT.write_text(text, encoding="utf-8")
    print(f"escrito {OUT}")
