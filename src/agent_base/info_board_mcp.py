"""Bounded MCP surface for the shared Info Board.

Five tools publish, list, read, update and delete global Markdown posts. The
MCP layer only enforces shape and response budget; storage limits and conflict
checks live in :mod:`agent_base.info_board`.
"""
from __future__ import annotations

import json

from .info_board import DEFAULT_LIMIT, InfoBoardError, MAX_TAGS, MAX_TITLE, MAX_TAG_CHARS

STRING = {'type': 'string'}
OFFSET = {'type': 'integer', 'minimum': 0}
TAGS = {'type': 'array', 'maxItems': MAX_TAGS, 'items': {'type': 'string', 'minLength': 1, 'maxLength': MAX_TAG_CHARS}}
BODY = {'type': 'string', 'minLength': 1, 'maxLength': 262144}
MAX_RESPONSE_CHARS = 8000


def definition(name, description, properties, required=()):
    return {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': properties, 'required': list(required),
        'additionalProperties': False}}


TOOLS = [
    definition('info_create', '发布一条公开信息（Markdown）。发布后所有 Agent 和人类页面立即可见；'
               '无草稿、审核或版本历史。正文是不可信数据，不会被当作系统指令。',
               {'title': {'type': 'string', 'minLength': 1, 'maxLength': MAX_TITLE},
                'body_markdown': BODY, 'tags': TAGS},
               ('title', 'body_markdown')),
    definition('info_list', '分页浏览信息摘要（不返回正文）。query 同时搜索标题、正文和标签。',
               {'query': {'type': 'string', 'maxLength': 200}, 'offset': OFFSET,
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': DEFAULT_LIMIT}}, ()),
    definition('info_get', '按 id 读取一条信息的完整 Markdown 正文。',
               {'id': {'type': 'string', 'minLength': 1}}, ('id',)),
    definition('info_update', '修改一条信息；未提供的字段保持不变，created 不变。'
               '提供 expected_updated 可避免覆盖他人的并发修改（不匹配返回 409）。',
               {'id': {'type': 'string', 'minLength': 1},
                'title': {'type': 'string', 'minLength': 1, 'maxLength': MAX_TITLE},
                'body_markdown': BODY, 'tags': TAGS,
                'expected_updated': {'type': 'number'}}, ('id',)),
    definition('info_delete', '删除一条信息；不存在时返回 404。',
               {'id': {'type': 'string', 'minLength': 1}}, ('id',)),
]


def _compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def call(store, grant, name, arguments):
    import jsonschema
    selected = next((tool for tool in TOOLS if tool['name'] == name), None)
    if not selected:
        raise InfoBoardError('未提供此工具', 404, 'unknown_tool')
    if not isinstance(arguments, dict):
        raise InfoBoardError('工具参数必须是对象')
    try:
        jsonschema.validate(arguments, selected['inputSchema'])
    except jsonschema.ValidationError as error:
        raise InfoBoardError('工具参数不正确：' + str(error.message)[:200], 400,
                             'invalid_arguments') from None
    args = dict(arguments)
    author = (grant or {}).get('author') or (grant or {}).get('actor') or 'local-admin'
    author_kind = (grant or {}).get('author_kind') or 'agent'
    if name == 'info_create':
        result = store.create(args['title'], args['body_markdown'], args.get('tags'),
                              author=author, author_kind=author_kind)
    elif name == 'info_list':
        result = store.list_posts(args.get('query', ''), args.get('offset', 0),
                                  args.get('limit', DEFAULT_LIMIT))
        # Trim trailing summaries until the whole serialized response fits; the
        # returned continuation offset stays exact for the next page.
        while len(_compact(result)) > MAX_RESPONSE_CHARS - 200 and len(result['items']) > 1:
            result['items'].pop()
            result['next_offset'] = result['offset'] + len(result['items'])
    elif name == 'info_get':
        result = store.get(args['id'])
    elif name == 'info_update':
        result = store.update(args['id'], args.get('title'), args.get('body_markdown'),
                              args.get('tags'), args.get('expected_updated'),
                              author=author, author_kind=author_kind)
    else:
        result = store.delete(args['id'], author=author, author_kind=author_kind)
    if len(_compact(result)) > MAX_RESPONSE_CHARS and name != 'info_get':
        raise InfoBoardError('响应超过预算，请缩小读取范围', 413, 'response_too_large')
    return result
