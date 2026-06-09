"""Tests for the timestamp renderers (core/ui.py _when / _when_text).

   Body-text timestamps are <time datetime="…Z"> elements whose text is
   the UTC fallback; the base.html localizer rewrites them to the
   browser's timezone on load and after HTMX swaps. Attribute contexts
   (tooltips) use _when_text — plain UTC, no markup."""
import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from markupsafe import escape

from core import core_db, ui

_TS = int(datetime.datetime(2025, 12, 9, 15, 51,
                            tzinfo=datetime.timezone.utc).timestamp())


def test_when_renders_a_time_element_with_utc_fallback():
    out = str(ui._when(_TS))
    assert 'datetime="2025-12-09T15:51:00Z"' in out
    assert ">2025-12-09 15:51 UTC</time>" in out
    assert 'title="2025-12-09 15:51 UTC"' in out


def test_when_survives_jinja_autoescape():
    """_when returns Markup, so autoescaping templates render the
       element rather than &lt;time&gt; text."""
    assert str(escape(ui._when(_TS))).startswith("<time")


def test_when_text_is_plain_utc():
    assert ui._when_text(_TS) == "2025-12-09 15:51 UTC"
    assert "<" not in ui._when_text(_TS)


def test_empty_timestamps_render_a_dash():
    assert ui._when(None) == "—"
    assert ui._when(0) == "—"
    assert ui._when_text(None) == "—"


def test_users_page_renders_time_elements(tmp_path, fake_admin_session):
    """End-to-end through Jinja: a page using when() carries the <time>
       element un-escaped, ready for the client-side localizer."""
    db = core_db.connect(str(tmp_path / "hone.db"))
    core_db.create_user(db, "a@x", "A", "local")
    app = FastAPI()
    app.include_router(ui.router)
    fake_admin_session(app)
    app.state.db = db
    body = TestClient(app).get("/users").text
    assert "<time datetime=" in body
    assert "&lt;time" not in body
