"""Read-only human API and page contract for the Info Board."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_base.info_board import InfoBoard
from agent_base.web import create_app


@pytest.fixture
def board(config):
    return InfoBoard(config)


@pytest.fixture
def seeded(config, board):
    for index in range(25):
        board.create(f'信息 {index:02d}', f'# 正文 {index}\n第 {index} 条正文。',
                     ['标签' if index % 2 else '偶数'], author='admin')
    return board


def test_page_route_and_markdown_safety_contract(config):
    with TestClient(create_app(config, background=False)) as client:
        assert client.get('/info').status_code == 200
    source = (Path(__file__).resolve().parents[1] / 'frontend/src/InfoPage.jsx').read_text()
    assert 'skipHtml' in source
    assert 'img:' in source and '图片未自动加载' in source
    assert 'target="_blank"' in source and 'rel="noreferrer"' in source
    assert 'rehype-raw' not in source and 'dangerouslySetInnerHTML' not in source


def test_api_requires_authentication_when_token_is_configured(config, seeded, monkeypatch):
    monkeypatch.setenv('AGENT_BASE_WEB_TOKEN', 'fixture-info-token')
    with TestClient(create_app(config, background=False)) as client:
        assert client.get('/api/info/posts').status_code == 401
        assert client.get('/api/info/posts').json()['detail']
        assert client.get('/api/info/status').status_code == 401
        headers = {'Authorization': 'Bearer fixture-info-token'}
        listing = client.get('/api/info/posts', headers=headers)
        assert listing.status_code == 200
        assert client.get('/api/info/status', headers=headers).status_code == 200


def test_summary_detail_search_and_pagination(config, seeded, monkeypatch):
    monkeypatch.setenv('AGENT_BASE_WEB_TOKEN', 'fixture-info-token')
    headers = {'Authorization': 'Bearer fixture-info-token'}
    with TestClient(create_app(config, background=False)) as client:
        first = client.get('/api/info/posts', headers=headers).json()
        assert len(first['items']) == 20 and first['next_offset'] == 20
        assert all('body_markdown' not in item for item in first['items'])
        assert all(set(item) == {'id', 'title', 'summary', 'tags', 'author', 'updated'}
                   for item in first['items'])
        second = client.get('/api/info/posts?offset=20', headers=headers).json()
        assert len(second['items']) == 5 and second['next_offset'] is None
        assert {item['id'] for item in first['items'] + second['items']} == {
            item['id'] for item in seeded.list_posts(limit=50)['items']}

        search = client.get('/api/info/posts?query=偶数', headers=headers).json()
        assert search['items'] and all('偶数' in item['tags'] for item in search['items'])

        post_id = first['items'][0]['id']
        detail = client.get('/api/info/posts/' + post_id, headers=headers)
        assert detail.status_code == 200
        assert detail.json()['body_markdown'].startswith('# 正文')
        missing = client.get('/api/info/posts/not-found', headers=headers)
        assert missing.status_code == 404
        assert missing.json() == {'error': '信息不存在或已删除', 'code': 'not_found', 'status': 404}

        # No human write route exists: publishing stays on the MCP surface.
        assert client.post('/api/info/posts', headers=headers, json={}).status_code in {404, 405}


def test_account_free_local_workspace_can_read(config, board):
    board.create('本地信息', '本地正文')
    with TestClient(create_app(config, background=False)) as client:
        assert client.get('/api/info/posts').status_code == 200
        assert client.get('/api/info/posts?query=本地').json()['items'][0]['title'] == '本地信息'
