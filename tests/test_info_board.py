"""Storage, limits, conflict handling and audit privacy for the Info Board."""
import json

import pytest

from agent_base.config import Config
from agent_base.info_board import DEFAULT_LIMIT, InfoBoard, InfoBoardError


@pytest.fixture
def board(config):
    return InfoBoard(config)


def make(board, title='标题', body='正文', tags=None, author='repo:fixture'):
    return board.create(title, body, tags, author=author)


def test_create_get_list_search_and_persistence(config, board):
    first = make(board, '第一条', '# 正文\n这是第一条信息。', ['发布', '规则'])
    second = make(board, '第二条', '第二条正文包含独特关键词：蓝鲸。', ['日志'], author='admin')
    assert first['author'] == 'repo:fixture' and second['author'] == 'admin'
    assert first['tags'] == ['发布', '规则'] and first['created'] == first['updated']

    page = board.list_posts()
    assert {item['id'] for item in page['items']} == {first['id'], second['id']}
    assert all('body_markdown' not in item for item in page['items'])
    assert any('这是第一条信息' in item['summary'] for item in page['items'])
    assert page['next_offset'] is None

    assert board.list_posts('蓝鲸')['items'][0]['id'] == second['id']
    assert board.list_posts('规则')['items'][0]['id'] == first['id']
    assert board.list_posts('不存在')['items'] == []

    full = board.get(first['id'])
    assert full['body_markdown'] == '# 正文\n这是第一条信息。'
    reopened = InfoBoard(config)
    assert reopened.get(first['id']) == full


def test_pagination_keeps_a_stable_page_size(board):
    created = [make(board, f'信息 {index:02d}', f'正文 {index}') for index in range(25)]
    first = board.list_posts(limit=DEFAULT_LIMIT)
    assert len(first['items']) == 20 and first['next_offset'] == 20
    second = board.list_posts(offset=first['next_offset'], limit=DEFAULT_LIMIT)
    assert len(second['items']) == 5 and second['next_offset'] is None
    assert {item['id'] for item in first['items'] + second['items']} == {item['id'] for item in created}
    assert board.list_posts(limit=0)['limit'] == 1
    assert board.list_posts(limit=500)['limit'] == 50


def test_update_preserves_created_and_rejects_stale_expected_updated(board):
    original = make(board, '原名', '原正文', ['旧'])
    updated = board.update(original['id'], title='新名', expected_updated=original['updated'],
                           author='admin')
    assert updated['title'] == '新名' and updated['body_markdown'] == '原正文'
    assert updated['tags'] == ['旧'] and updated['created'] == original['created']
    assert updated['updated'] > original['updated']
    with pytest.raises(InfoBoardError) as error:
        board.update(original['id'], body_markdown='并发覆盖', expected_updated=original['updated'])
    assert error.value.status == 409 and error.value.code == 'conflict'
    assert board.get(original['id'])['body_markdown'] == '原正文'
    # Omitting expected_updated is the explicit last-writer-wins mode.
    assert board.update(original['id'], body_markdown='最后一次')['body_markdown'] == '最后一次'


def test_missing_objects_are_404_and_delete_is_idempotent_observable(board):
    with pytest.raises(InfoBoardError) as error:
        board.get('missing')
    assert error.value.status == 404 and error.value.code == 'not_found'
    with pytest.raises(InfoBoardError):
        board.update('missing', title='x')
    with pytest.raises(InfoBoardError):
        board.delete('missing')
    post = make(board)
    assert board.delete(post['id']) == {'deleted': True, 'id': post['id']}
    with pytest.raises(InfoBoardError):
        board.get(post['id'])
    assert board.stats()['posts'] == 0


def test_title_body_and_tag_limits(board):
    with pytest.raises(InfoBoardError):
        board.create('', '正文')
    with pytest.raises(InfoBoardError) as error:
        board.create('标' * 201, '正文')
    assert error.value.status == 413
    with pytest.raises(InfoBoardError) as error:
        board.create('标题', 'x' * (256 * 1024 + 1))
    assert error.value.status == 413 and error.value.code == 'body_too_large'
    with pytest.raises(InfoBoardError):
        board.create('标题', '正文', [f't{index}' for index in range(11)])
    with pytest.raises(InfoBoardError):
        board.create('标题', '正文', ['x' * 33])
    with pytest.raises(InfoBoardError):
        board.create('标题', '正文', ['ok', '   '])
    assert board.create(' 标题 ', ' 正文 ', [' a ', 'a', 'b'])['tags'] == ['a', 'b']
    assert board.create('标题', 'x' * (256 * 1024))['id']


def test_audit_records_identity_and_size_but_never_the_body(board):
    secret = '审计不能写入的正文内容'
    post = make(board, '审计标题', '# ' + secret, ['标签'])
    board.update(post['id'], body_markdown=secret + '（修改）', author='admin')
    board.delete(post['id'], author='admin')
    rows = board.audit_rows()
    assert [row['action'] for row in rows] == ['info.delete', 'info.update', 'info.create']
    assert all(row['actor'] == 'admin' for row in rows[:2])
    assert rows[-1]['actor'] == 'repo:fixture'
    for row in rows:
        assert secret not in json.dumps(row, ensure_ascii=False)
        assert set(row['data']) == {'id', 'title', 'size'}
        assert row['data']['size'] > 0


def test_service_disabled_in_configuration(tmp_path):
    config_file = tmp_path / 'config.toml'
    config_file.write_text('[storage]\nroot="data"\n[queue]\nmax_concurrent=2\n[runtime]\n'
                           'engine="dsh"\ncommand=["python3","fake"]\nmodel="fixture"\n'
                           '[info_board]\nenabled=false\n', encoding='utf-8')
    assert Config.load(config_file).info_board == {'enabled': False}
    config_file.write_text(config_file.read_text().replace('enabled=false', 'enabled="yes"'),
                           encoding='utf-8')
    with pytest.raises(ValueError, match='info_board.enabled'):
        Config.load(config_file)
