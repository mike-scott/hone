# tests/conftest.py — test-session setup.
#
#  * Put the repo root on sys.path so `core` and `node` import as packages.
#  * Pin the test interpreter to the Python version the container images run:
#    the 3.13-vs-3.14 skew that once let a Python-3.13-only crash through the
#    suite must not recur.
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _dockerfile_python(path):
    """The (major, minor) Python version a Dockerfile pins via `FROM python:`,
       or None if the file has no such line."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*FROM\s+python:(\d+)\.(\d+)", line)
                if m:
                    return int(m.group(1)), int(m.group(2))
    except OSError:
        pass
    return None


@pytest.fixture
def fake_admin_session():
    """An installer that bypasses the operator UI's session-auth dependencies
       on a test's FastAPI app, so handler tests don't have to wire up
       SessionMiddleware + a real login. Use it from a ctx-style fixture:

           @pytest.fixture
           def ctx(tmp_path, fake_admin_session):
               app = FastAPI()
               app.include_router(ui.router)
               fake_admin_session(app)
               ...

       Returns the SessionUser used (a config-admin); tests that need a
       non-admin can call again with is_config_admin=False."""
    from core import auth

    def _install(app, *, is_config_admin=True):
        user = auth.SessionUser(
            id=None if is_config_admin else 1,
            email="test@local",
            display_name="Test Admin" if is_config_admin else "Test User",
            is_config_admin=is_config_admin)
        app.dependency_overrides[auth.require_session]      = lambda: user
        app.dependency_overrides[auth.require_config_admin] = lambda: user
        # Handler-focused tests POST without a real session/CSRF token; bypass
        # the CSRF gate too so they exercise the route, not the gate. A
        # dedicated test file covers CSRF enforcement against the real
        # dependency.
        app.dependency_overrides[auth.require_csrf]         = lambda: None
        return user
    return _install


def pytest_configure(config):
    """Fail fast unless the tests run on the Python version the runtime images
       pin — the test environment must match the runtime environment."""
    pinned = {img: v for img in ("core", "node")
              if (v := _dockerfile_python(
                  os.path.join(_ROOT, img, "Dockerfile"))) is not None}
    if not pinned:
        return
    if len(set(pinned.values())) > 1:
        raise pytest.UsageError(
            "the core/ and node/ Dockerfiles pin different Python versions: "
            + ", ".join(f"{img}=python:{v[0]}.{v[1]}"
                        for img, v in pinned.items()))
    want = next(iter(pinned.values()))
    running = sys.version_info[:2]
    if running != want:
        raise pytest.UsageError(
            f"tests are running on Python {running[0]}.{running[1]}, but the "
            f"container images pin python:{want[0]}.{want[1]} — re-create the "
            f"venv with python{want[0]}.{want[1]} so the test environment "
            f"matches the runtime.")
