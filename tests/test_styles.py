import sys
from pathlib import Path

import pytest

pytest.importorskip("sass")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_css  # noqa: E402


def test_compiled_css_is_up_to_date():
    """style.css is generated from scss/ but checked in (no build step in
    Docker) — fail loudly when someone edits scss without recompiling."""
    expected = build_css.build()
    actual = build_css.OUT.read_text(encoding="utf-8")
    assert actual == expected, (
        "static/style.css is stale — run: python scripts/build_css.py"
    )
