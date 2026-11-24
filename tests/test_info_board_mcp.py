"""The five Info Board MCP tools: schemas, errors, budget and real HTTP."""
import asyncio
import json
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_base.info_board import InfoBoard, InfoBoardError
from agent_base.info_board_mcp import TOOLS, call
from agent_base.info_board_service import InfoBoardService


@pytest.fixture
def board(config):
    return InfoBoard(config)


@pytest.fixture
def grant(board):
    return board.grant(board.issue({'author': 'repo:fixture', 'author_kind': 'agent'}, 60))


def test_tool_surface_and_crud(board, grant):
    assert [tool['name'] for tool in TOOLS] == [
        'info_create', 'info_list', 'info_get', 'info_update', 'info_delete']
    created = call(board, grant, 'info_create', {
        'title': 'MCP 发布', 'body_markdown': '# 标题\n正文 **加粗**', 'tags': ['mcp']})
    assert created['author'] == 'repo:fixture' and created['body_markdown'].startswith('# 标题')
    listing = call(board, grant, 'info_list', {'query': 'MCP'})
    assert listing['items'][0]['id'] == created['id'] and 'body_markdown' not in listing['items'][0]
    full = call(board, grant, 'info_get', {'id': created['id']})
    assert full['body_markdown'] == '# 标题\n正文 **加粗**'
    changed = call(board, grant, 'info_update', {
        'id': created['id'], 'body_markdown': '修改后', 'expected_updated': created['updated']})
    assert changed['body_markdown'] == '修改后' and changed['created'] == created['created']
    assert call(board, grant, 'info_delete', {'id': created['id']})['deleted']
    with pytest.raises(InfoBoardError) as error:
        call(board, grant, 'info_get', {'id': created['id']})
    assert error.value.status == 404


def test_tool_errors_use_structured_statuses(board, grant):
    with pytest.raises(InfoBoardError) as error:
        call(board, grant, 'info_delete', {'id': 'missing'})
    assert error.value.status == 404 and error.value.code == 'not_found'
    with pytest.raises(InfoBoardError):
        call(board, grant, 'info_create', {'title': '', 'body_markdown': 'x'})
    with pytest.raises(InfoBoardError):
        call(board, grant, 'info_create', {'title': 'x', 'body_markdown': 'x', 'unknown': 1})
    with pytest.raises(InfoBoardError) as error:
        call(board, grant, 'not_a_tool', {})
    assert error.value.status == 404


def test_list_response_stays_within_8000_characters(board, grant):
    for index in range(30):
        call(board, grant, 'info_create', {
            'title': f'第 {index} 条很长的信息标题',
            'body_markdown': ('长正文内容 ' * 80) + f' 唯一编号 {index}'})
    result = call(board, grant, 'info_list', {'limit': 50})
    encoded = json.dumps(result, ensure_ascii=False)
    assert len(encoded) <= 8000
    assert result['next_offset'] is not None
    continuation = call(board, grant, 'info_list', {'offset': result['next_offset'], 'limit': 50})
    assert len(json.dumps(continuation, ensure_ascii=False)) <= 8000
    assert {item['id'] for item in result['items']} & {item['id'] for item in continuation['items']} == set()


def test_real_streamable_http_mcp_and_authentication(config, board):
    service = InfoBoardService(config)
    first = service.ensure()
    assert service.ensure()['pid'] == first['pid']
    token = board.issue({'author': 'admin', 'author_kind': 'agent'}, 60)

    async def exercise():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + token},
                                     trust_env=False) as client:
            async with streamable_http_client(first['endpoint'], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    assert tools == {'info_create', 'info_list', 'info_get', 'info_update', 'info_delete'}
                    created = await session.call_tool('info_create', {
                        'title': '真实 MCP', 'body_markdown': '# 真实\n通过 HTTP 工具写入。'})
                    assert not created.isError, created
                    post = json.loads(created.content[0].text)
                    listed = await session.call_tool('info_list', {})
                    assert not listed.isError
                    assert json.loads(listed.content[0].text)['items'][0]['id'] == post['id']
                    read = await session.call_tool('info_get', {'id': post['id']})
                    assert json.loads(read.content[0].text)['body_markdown'].startswith('# 真实')
                    updated = await session.call_tool('info_update', {
                        'id': post['id'], 'title': '真实更新',
                        'expected_updated': post['updated']})
                    assert not updated.isError
                    stale = await session.call_tool('info_update', {
                        'id': post['id'], 'title': '覆盖', 'expected_updated': post['updated']})
                    assert stale.isError
                    payload = json.loads(stale.content[0].text)
                    assert payload['status'] == 409 and payload['code'] == 'conflict'
                    deleted = await session.call_tool('info_delete', {'id': post['id']})
                    assert not deleted.isError
                    missing = await session.call_tool('info_get', {'id': post['id']})
                    assert missing.isError and json.loads(missing.content[0].text)['status'] == 404
    asyncio.run(exercise())
    assert service.status()['pid'] == first['pid']

    response = httpx.post(first['endpoint'], headers={'Authorization': 'Bearer wrong'},
                          json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
                          trust_env=False)
    assert response.status_code == 401
    assert response.json()['code'] == 'unauthorized'
    health = httpx.get(first['endpoint'].removesuffix('/mcp') + '/health', trust_env=False)
    assert health.status_code == 401
    assert service.restart()['state'] == 'healthy'
    assert board.stats()['posts'] == 0
