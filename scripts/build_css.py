"""Compile the SCSS sources into the checked-in static stylesheet.

Usage:  python scripts/build_css.py
Needs:  pip install -e .[dev]   (libsass)
"""

from __future__ import annotations

from pathlib import Path

import sass

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "memehog" / "web" / "scss"
OUT = ROOT / "src" / "memehog" / "web" / "static" / "style.css"

HEADER = (
    "/* GENERATED FILE - do not edit. "
    "Sources: src/memehog/web/scss/, build: scripts/build_css.py */\n"
)


def build() -> str:
    css = sass.compile(
        filename=str(SRC / "style.scss"),
        output_style="expanded",
        include_paths=[str(SRC)],
    )
    return HEADER + css


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes)")
