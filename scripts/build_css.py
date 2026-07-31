"""Compile the SCSS sources into static/style.css (dev helper).

The Docker build does the same thing with the standalone dart-sass binary,
so the compiled file is NOT checked in.

Usage:  python scripts/build_css.py
Needs:  pip install -e .[dev]   (libsass)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memehog.web.styles import write_css  # noqa: E402

if __name__ == "__main__":
    out = write_css()
    print(f"wrote {out} ({out.stat().st_size} bytes)")
