import pytest

pytest.importorskip("sass")

from memehog.web.styles import build_css  # noqa: E402


def test_scss_compiles():
    """The stylesheet is generated at build time (dart-sass in Docker, libsass
    in dev) — catch SCSS errors here instead of at image build."""
    css = build_css()
    assert "--accent" in css
    assert ".topbar" in css
    assert ".bench-grid" in css
    assert len(css) > 5000
