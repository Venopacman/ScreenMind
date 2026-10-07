"""Shared pytest fixtures for ScreenMind tests."""

import tempfile
from unittest.mock import MagicMock, patch

import pytest



from screenmind.config import settings


@pytest.fixture(autouse=True, scope="session")
def _isolate_data_dir(tmp_path_factory):
    """Keep every test out of the real data dir (~/.screenmind by default).

    Without this, tests write into the user's live data: CaptureWorker.pause()
    persists capture_paused=true to settings.json (so the next app start is
    paused).
    Session-scoped because some modules cache connections to files there.
    """
    data_dir = tmp_path_factory.mktemp("screenmind-data")
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "data_dir", str(data_dir))
    yield data_dir
    mp.undo()


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Provide a temporary data directory for tests."""
    return tmp_path


@pytest.fixture
def mock_settings(tmp_path):
    """Patch settings to use temp directories."""
    with patch("screenmind.config.settings") as mock:
        mock.data_path = tmp_path
        mock.screenshots_dir = tmp_path / "screenshots"
        mock.db_path = tmp_path / "test.db"
        mock.screenshots_dir.mkdir(parents=True, exist_ok=True)
        mock.capture_interval = 30
        mock.screenshot_quality = 70
        mock.blocked_apps_list = []
        mock.heavy_apps_list = []
        mock.auto_pause_heavy_apps = False
        mock.sensitive_filter_enabled = False
        mock.retention_days = 7
        mock.encryption_enabled = False
        mock.api_host = "127.0.0.1"
        mock.api_port = 7777
        yield mock


@pytest.fixture
def db(tmp_path):
    """Create a fresh test database."""
    from screenmind.storage.database import Database
    test_db = Database(db_path=tmp_path / "test.db")
    yield test_db
    test_db.close()


@pytest.fixture
def sample_image():
    """Create a simple test image."""
    from PIL import Image
    img = Image.new("RGB", (1920, 1080), color=(50, 50, 80))
    return img


@pytest.fixture
def app(db):
    """Create a test FastAPI app."""
    import screenmind.api.dependencies as deps
    from screenmind.api.server import create_app

    application = create_app(database=db)

    # Also patch the module-level db references in route modules
    # since they may have been imported with a previous db value
    import screenmind.api.routes.timeline as _tl
    import screenmind.api.routes.stats as _st
    import screenmind.api.routes.data as _dt
    import screenmind.api.routes.screenshots as _ss
    import screenmind.api.routes.meetings as _mt
    import screenmind.api.routes.search as _sr

    for mod in [_tl, _st, _dt, _ss, _mt, _sr]:
        mod.db = db

    yield application


@pytest.fixture
async def client(app):
    """Create a test HTTP client."""
    from httpx import AsyncClient, ASGITransport
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
