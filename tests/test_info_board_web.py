"""Human read-only Info Board API: auth, summary/detail, pages, disabled 404."""
import pytest
from fastapi.testclient import TestClient

from agent_base.mcp_packages import Installations, load_package
from agent_base.web import create_app
from first_party.info_board.backend.store import InfoBoard

PACKAGE = load_package("first_party/info_board")
TOKEN = {"Authorization": "Bearer fixture-info-token"}


@pytest.fixture
def info_app(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_WEB_TOKEN", "fixture-info-token")
    Installations(config.data / "mcp").install(PACKAGE)
    app = create_app(config, background=False)
    board = InfoBoard(config.data / PACKAGE.data_dir)
    with TestClient(app) as client:
        yield client, app, board, config


def test_unauthenticated_is_401(info_app):
    client, _, _, _ = info_app
    assert client.get("/api/info/posts").status_code == 401
    assert client.get("/api/info/posts/anything").status_code == 401
    assert client.get("/api/info/posts", headers=TOKEN).status_code == 200


def test_summary_and_detail(info_app):
    client, _, board, _ = info_app
    created = board.create("公告", "# 标题\n\n正文内容", ["运维"], author="repo:web")
    listing = client.get("/api/info/posts", headers=TOKEN).json()
    item = listing["items"][0]
    assert item["id"] == created["id"] and item["summary"] and "body_markdown" not in item
    detail = client.get(f"/api/info/posts/{created['id']}", headers=TOKEN).json()
    assert detail["body_markdown"] == "# 标题\n\n正文内容"
    assert client.get("/api/info/posts/missing", headers=TOKEN).status_code == 404


def test_pagination_and_search(info_app):
    client, _, board, _ = info_app
    for index in range(25):
        board.create(f"网页信息 {index}", f"内容 {index} " + "x" * 100, ["page"])
    first = client.get("/api/info/posts?offset=0&limit=20", headers=TOKEN).json()
    assert len(first["items"]) == 20 and first["next_offset"] == 20
    second = client.get(f"/api/info/posts?offset={first['next_offset']}&limit=20",
                        headers=TOKEN).json()
    assert len(second["items"]) == 5 and second["next_offset"] is None
    hits = client.get("/api/info/posts?query=网页信息 7", headers=TOKEN).json()["items"]
    assert [item["title"] for item in hits] == ["网页信息 7"]


def test_navigation_and_page(info_app):
    client, _, _, _ = info_app
    rows = client.get("/api/mcp/packages", headers=TOKEN).json()
    assert [row["id"] for row in rows] == ["info-board"]
    assert rows[0]["route"] == "/info" and rows[0]["label"] == "信息台"
    page = client.get("/info")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]


def test_disabled_package_disappears_and_returns_404(info_app):
    client, _, _, config = info_app
    Installations(config.data / "mcp").disable(PACKAGE)
    assert client.get("/api/mcp/packages", headers=TOKEN).json() == []
    assert client.get("/api/info/posts", headers=TOKEN).status_code == 404
    assert client.get("/info").status_code == 404
