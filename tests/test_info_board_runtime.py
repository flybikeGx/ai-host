"""Info Board mounting for every execution entry plus a real MCP run."""
import json
import time
from pathlib import Path

import yaml

from agent_base.engines import dsh_patch
from agent_base.info_board import InfoBoard
from agent_base.info_board_service import InfoBoardService, attach
from agent_base.service_resolver import ServiceResolver


def turn_dir(tmp_path, name='turn'):
    path = tmp_path / name
    path.mkdir()
    return path


def mounted(patch):
    rows = yaml.load(Path(patch).read_text(encoding='utf-8'), Loader=yaml.BaseLoader) or []
    return [insert for row in rows for insert in row.get('insert', [])
            if insert.get('id') == 'info-board']


def test_attach_mounts_global_board_and_is_idempotent(config, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    patch = dsh_patch(config.runtime, turn_dir(tmp_path))
    env = {}
    instructions = attach(config, patch, env, 'repo:fixture', {'run_id': 'r1'},
                          time.time() + 120, workspace)
    assert all(name in instructions for name in
               ('info_create', 'info_list', 'info_get', 'info_update', 'info_delete'))
    rows = mounted(patch)
    assert len(rows) == 1 and rows[0]['config']['serverName'] == 'info_board'
    assert rows[0]['config']['url'].startswith('http://127.0.0.1:')
    assert 'process.env["AGENT_BASE_INFO_BOARD_AUTH"]' in patch.read_text()
    assert env['AGENT_BASE_INFO_BOARD_AUTH'].startswith('Bearer ')
    assert env['AGENT_BASE_INFO_BOARD_AUTH'] not in patch.read_text()
    grant = InfoBoard(config).grant(env['AGENT_BASE_INFO_BOARD_AUTH'].removeprefix('Bearer '))
    assert grant['author'] == 'repo:fixture' and grant['source'] == {'run_id': 'r1'}

    catalog = json.loads((workspace / '.agent/resources.json').read_text())
    assert catalog['info_board']['server'] == 'info_board'
    assert catalog['info_board']['tools'][0] == 'info_create'

    assert attach(config, patch, env, 'repo:fixture', {'run_id': 'r1'},
                  time.time() + 120, workspace) == ''
    assert len(mounted(patch)) == 1


def test_resolver_mounts_board_for_admin_and_v3_without_service_bindings(config, tmp_path):
    config.data.mkdir(parents=True, exist_ok=True)
    for manifest, installation in ((None, None), ({'schema_version': 3, 'services': []},
                                                  {'id': 'install-1', 'repo_id': 'abc123'})):
        workspace = tmp_path / ('ws-' + str(manifest is not None))
        workspace.mkdir()
        patch = dsh_patch(config.runtime, turn_dir(tmp_path, 'turn-' + str(manifest is not None)))
        env = {}
        instructions = ServiceResolver(config).attach(
            patch=patch, env=env, workspace=workspace, manifest=manifest,
            installation=installation, actor=None if installation else 'admin',
            deadline=time.time() + 120)
        assert '共享信息台 MCP' in instructions
        assert len(mounted(patch)) == 1
        assert 'AGENT_BASE_INFO_BOARD_AUTH' in env


def test_disabled_or_unavailable_board_never_blocks_a_run(config, tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    patch = dsh_patch(config.runtime, turn_dir(tmp_path))
    config.info_board = {'enabled': False}
    before = patch.read_text()
    assert attach(config, patch, {}, 'admin', {}, time.time() + 60, workspace) == ''
    assert patch.read_text() == before

    config.info_board = {'enabled': True}

    def down(self, explicit=False):
        raise ValueError('simulated startup failure')
    monkeypatch.setattr(InfoBoardService, 'ensure', down)
    env = {}
    note = attach(config, patch, env, 'admin', {}, time.time() + 60, workspace)
    assert '不可用' in note and 'AGENT_BASE_INFO_BOARD_AUTH' not in env
    assert mounted(patch) == []


def test_background_run_receives_and_uses_the_board(config, repo):
    from agent_base.service import Service
    service = Service(config)
    job = service.submit(str(repo), 'FIXTURE_INFO')
    result = service.wait(job['id'], 30)
    assert result['status'] == 'succeeded', result
    workspace = Path(result['workspace'])
    captured = json.loads((workspace / 'captured-info.json').read_text())
    assert captured['created'] == captured['listed']
    assert captured['body'].startswith('# 信息')
    assert captured['updated'] == '运行时信息（更新）'
    assert captured['author'] == 'local-admin'
    assert json.loads((workspace / '.agent/resources.json').read_text())['info_board']['server'] == 'info_board'
    events = [json.loads(line) for line in (Path(result['root']) / 'runtime.jsonl').read_text().splitlines()]
    tools = [event['tool'] for event in events if event.get('type') == 'tool_call']
    assert {'mcp__info_board__info_create', 'mcp__info_board__info_list',
            'mcp__info_board__info_get', 'mcp__info_board__info_update',
            'mcp__info_board__info_delete'} <= set(tools)


def test_web_conversation_and_admin_cli_receive_the_board(config):
    from agent_base.conversations import Conversations
    chats = Conversations(config)
    chat = chats.create('admin')
    turn = chats.send(chat['id'], 'FIXTURE_INFO', 'info-board-chat')
    deadline = time.time() + 30
    while time.time() < deadline and chats.turn(turn['id'])['status'] in {'queued', 'running'}:
        chats.reconcile()
        time.sleep(0.1)
    assert chats.turn(turn['id'])['status'] == 'succeeded'
    workspace = config.data / 'conversations' / chat['id'] / 'workspace'
    prompt = json.loads((workspace / 'captured-input.json').read_text())['prompt']
    assert '共享信息台 MCP' in prompt and 'info_board' in prompt
    assert json.loads((workspace / 'captured-info.json').read_text())['author'] == 'admin'

    from agent_base.admin import Admin
    admin = Admin(config, 'fixture')
    assert admin.turn('FIXTURE_INFO') == 'fixture completed'
    prompt = json.loads((admin.workspace / 'captured-input.json').read_text())['prompt']
    assert '共享信息台 MCP' in prompt
    assert json.loads((admin.workspace / 'captured-info.json').read_text())['author'] == 'admin'
