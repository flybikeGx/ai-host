"""Info Board storage: CRUD, pagination, conflicts, limits, audit, restart."""
import json

import pytest

from first_party.info_board.backend.store import (MAX_BODY_BYTES, MAX_TAGS,
                                                  InfoBoard, InfoBoardError)


@pytest.fixture
def board(tmp_path):
    return InfoBoard(tmp_path / "data")


def test_create_get_and_author(board):
    post = board.create("第一条", "# 标题\n\n正文", ["公告", "公告", "运维"], author="repo:abc")
    assert post["author"] == "repo:abc" and post["author_kind"] == "agent"
    assert post["tags"] == ["公告", "运维"]
    assert post["created"] == post["updated"]
    again = board.get(post["id"])
    assert again == post


def test_update_keeps_created_and_advances_updated(board):
    post = board.create("标题", "正文")
    updated = board.update(post["id"], body_markdown="新正文", expected_updated=post["updated"])
    assert updated["created"] == post["created"]
    assert updated["updated"] > post["updated"]
    assert updated["title"] == "标题" and updated["body_markdown"] == "新正文"


def test_update_conflict_is_409(board):
    post = board.create("标题", "正文")
    board.update(post["id"], title="第二版")
    with pytest.raises(InfoBoardError) as error:
        board.update(post["id"], title="第三版", expected_updated=post["updated"])
    assert error.value.status == 409 and error.value.code == "conflict"


def test_missing_post_is_404(board):
    for action in (board.get, board.delete):
        with pytest.raises(InfoBoardError) as error:
            action("missing")
        assert error.value.status == 404
    with pytest.raises(InfoBoardError) as error:
        board.update("missing", title="x")
    assert error.value.status == 404


def test_limits(board):
    with pytest.raises(InfoBoardError):
        board.create("", "正文")
    with pytest.raises(InfoBoardError) as error:
        board.create("x" * 201, "正文")
    assert error.value.status == 413 and error.value.code == "title_too_long"
    with pytest.raises(InfoBoardError) as error:
        board.create("标题", "x" * (MAX_BODY_BYTES + 1))
    assert error.value.code == "body_too_large"
    with pytest.raises(InfoBoardError) as error:
        board.create("标题", "正文", [f"t{i}" for i in range(MAX_TAGS + 1)])
    assert error.value.code == "too_many_tags"
    with pytest.raises(InfoBoardError) as error:
        board.create("标题", "正文", ["x" * 33])
    assert error.value.code == "invalid_tag"


def test_pagination_search_and_summary(board):
    for index in range(25):
        board.create(f"信息 {index}", f"正文 {index} " + "x" * 300, ["tag" + str(index % 3)])
    first = board.list_posts(offset=0, limit=20)
    assert len(first["items"]) == 20 and first["next_offset"] == 20
    assert all("body_markdown" not in item for item in first["items"])
    assert len(first["items"][0]["summary"]) <= 240
    second = board.list_posts(offset=20, limit=20)
    assert len(second["items"]) == 5 and second["next_offset"] is None
    hits = board.list_posts(query="信息 7")
    assert [item["title"] for item in hits["items"]] == ["信息 7"]
    assert board.list_posts(query="tag1")["items"]
    # limit is clamped, offset can never go backwards.
    assert board.list_posts(offset=-5, limit=999)["offset"] == 0
    assert board.list_posts(limit=999)["limit"] == 50


def test_audit_records_metadata_without_body(board):
    post = board.create("审计标题", "机密正文内容", ["t"])
    board.update(post["id"], body_markdown="更新后的机密正文")
    board.delete(post["id"])
    rows = board.audit_rows()
    assert [row["action"] for row in rows] == ["info.delete", "info.update", "info.create"]
    for row in rows:
        assert set(row["data"]) == {"id", "title", "size"}
        assert row["data"]["id"] == post["id"]
    serialized = json.dumps(rows, ensure_ascii=False)
    assert "机密正文" not in serialized


def test_data_survives_a_new_store_instance(tmp_path):
    root = tmp_path / "data"
    post = InfoBoard(root).create("持久化", "重启后仍在")
    assert InfoBoard(root).get(post["id"])["body_markdown"] == "重启后仍在"
