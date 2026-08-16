"""Frontend smoke: generated API types stay in sync; production build works."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
TYPES_PATH = FRONTEND / "src" / "lib" / "api" / "types.ts"


def _normalize(text: str) -> str:
    """Normalize newlines so Windows vs Unix checkouts compare cleanly."""
    return text.replace("\r\n", "\n").strip() + "\n"


def test_generated_api_types_match_committed() -> None:
    """Fail when app.models drift from frontend/src/lib/api/types.ts."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_types  # noqa: E402

    rendered = _normalize(generate_types.render_types())
    committed = _normalize(TYPES_PATH.read_text(encoding="utf-8"))
    assert rendered == committed, (
        "frontend/src/lib/api/types.ts is out of sync with app models. "
        "Run: uv run python scripts/generate_types.py"
    )


@pytest.mark.slow
def test_frontend_npm_build() -> None:
    """Smoke-check that the Vite production build succeeds."""
    if not (FRONTEND / "node_modules").is_dir():
        pytest.skip("frontend/node_modules missing; run npm install in frontend/")

    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    result = subprocess.run(
        [npm, "run", "build"],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=300,
        shell=False,
    )
    assert result.returncode == 0, (
        f"npm run build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
