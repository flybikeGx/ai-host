"""MCP tool layer: shape, budget, error codes and grant-derived author."""
import json

import pytest

from first_party.info_board.backend.mcp import MAX_RESPONSE_CHARS, call
from first_party.info_board.backend.store import InfoBoard, InfoBoardError


@pytest.fixture
def board(tmp_path):
    return InfoBoard(tmp_path / "data")


GRANT = {"actor": "repo:fixture", "kind": "agent"}


def test_all_five_tools_roundtrip(board):
    created = call(board, GRANT, "info_create", {"title": "工具", "body_markdown": "# 正文", "tags": ["a"]})
    assert created["author"] == "repo:fixture" and created["author_kind"] == "agent"
    listing = call(board, GRANT, "info_list", {"query": "工具"})
    assert listing["items"][0]["id"] == created["id"]
    assert "body_markdown" not in listing["items"][0]
    assert call(board, GRANT, "info_get", {"id": created["id"]})["body_markdown"] == "# 正文"
    updated = call(board, GRANT, "info_update", {"id": created["id"], "title": "工具 2",
                                                 "expected_updated": created["updated"]})
    assert updated["title"] == "工具 2"
    assert call(board, GRANT, "info_delete", {"id": created["id"]}) == {"deleted": True,
                                                                        "id": created["id"]}


def test_list_response_stays_under_budget(board):
    for index in range(30):
        board.create(f"长信息 {index}", "y" * 900, ["budget"])
    result = call(board, GRANT, "info_list", {"limit": 50})
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= MAX_RESPONSE_CHARS
    assert result["next_offset"] is not None
    follow = call(board, GRANT, "info_list", {"offset": result["next_offset"], "limit": 50})
    assert follow["items"]


def test_error_codes_are_structured(board):
    with pytest.raises(InfoBoardError) as error:
        call(board, GRANT, "info_missing", {})
    assert error.value.status == 404 and error.value.code == "unknown_tool"
    with pytest.raises(InfoBoardError) as error:
        call(board, GRANT, "info_create", {"title": "x"})
    assert error.value.status == 400 and error.value.code == "invalid_arguments"
    with pytest.raises(InfoBoardError) as error:
        call(board, GRANT, "info_get", {"id": "nope"})
    assert error.value.status == 404 and error.value.code == "not_found"


def test_tool_update_conflict(board):
    created = call(board, GRANT, "info_create", {"title": "并发", "body_markdown": "a"})
    call(board, GRANT, "info_update", {"id": created["id"], "body_markdown": "b"})
    with pytest.raises(InfoBoardError) as error:
        call(board, GRANT, "info_update", {"id": created["id"], "body_markdown": "c",
                                           "expected_updated": created["updated"]})
    assert error.value.status == 409
