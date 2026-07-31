"""SCSS → CSS compilation helpers.

The stylesheet is generated, not checked in: the Docker build compiles it with
the standalone dart-sass binary (see docker/Dockerfile), and bare-metal dev
setups compile it here via libsass (`pip install -e .[dev]`) — automatically on
first start, or manually with scripts/build_css.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
SCSS_DIR = WEB_DIR / "scss"
CSS_OUT = WEB_DIR / "static" / "style.css"
HEADER = "/* GENERATED FILE - do not edit. Sources: src/memehog/web/scss/ */\n"


def build_css() -> str:
    import sass  # libsass, dev extra — imported lazily, absent in prod images

    css = sass.compile(
        filename=str(SCSS_DIR / "style.scss"),
        output_style="expanded",
        include_paths=[str(SCSS_DIR)],
    )
    return HEADER + css


def write_css() -> Path:
    CSS_OUT.parent.mkdir(parents=True, exist_ok=True)
    CSS_OUT.write_text(build_css(), encoding="utf-8", newline="\n")
    return CSS_OUT


def ensure_css() -> None:
    """Dev fallback: the Docker image ships a precompiled stylesheet, but a
    fresh bare-metal checkout doesn't — compile it on first start."""
    CSS_OUT.parent.mkdir(parents=True, exist_ok=True)
    if CSS_OUT.exists():
        return
    try:
        write_css()
        log.info("Compiled SCSS to %s", CSS_OUT)
    except ImportError:
        log.warning(
            "style.css is missing and libsass is not installed — the UI will "
            "be unstyled. Run: pip install -e .[dev] && python scripts/build_css.py"
        )
