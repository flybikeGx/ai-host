"""Storage for the shared Info Board: global, flat Markdown posts + audit.

There is deliberately no agent or domain isolation in this first version: every
installable execution entry sees and may change every post. The author is
recorded for display and audit only, never as an access-control boundary.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_TITLE = 200
MAX_BODY_BYTES = 256 * 1024
MAX_TAGS = 10
MAX_TAG_CHARS = 32
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
SUMMARY_CHARS = 240


class InfoBoardError(ValueError):
    def __init__(self, message, status=400, code='invalid'):
        super().__init__(message)
        self.status = status
        self.code = code


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))



def _title(value):
    if not isinstance(value, str):
        raise InfoBoardError('标题必须是文本')
    value = value.strip()
    if not value:
        raise InfoBoardError('标题不能为空')
    if len(value) > MAX_TITLE:
        raise InfoBoardError(f'标题最多 {MAX_TITLE} 个字符', 413, 'title_too_long')
    return value


def _body(value):
    if not isinstance(value, str):
        raise InfoBoardError('正文必须是 Markdown 文本')
    if not value.strip():
        raise InfoBoardError('正文不能为空')
    size = len(value.encode('utf-8'))
    if size > MAX_BODY_BYTES:
        raise InfoBoardError('正文超过 256 KiB', 413, 'body_too_large')
    return value


def _tags(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise InfoBoardError('标签必须是文本数组')
    if len(value) > MAX_TAGS:
        raise InfoBoardError(f'标签最多 {MAX_TAGS} 个', 413, 'too_many_tags')
    result = []
    for item in value:
        if not isinstance(item, str):
            raise InfoBoardError('标签必须是文本')
        item = item.strip()
        if not item or len(item) > MAX_TAG_CHARS:
            raise InfoBoardError(f'每个标签必须为 1–{MAX_TAG_CHARS} 个字符', 413, 'invalid_tag')
        if item not in result:
            result.append(item)
    return result


def summary(body, limit=SUMMARY_CHARS):
    """A plain-text preview; the stored Markdown body is never altered."""
    text = re.sub(r'```.*?```', ' ', body, flags=re.S)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'^\s{0,3}#{1,6}\s*', '', text, flags=re.M)
    text = re.sub(r'[*_~>#-]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit]


class InfoBoard:
    """One SQLite database per package data root; every entry shares it."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / 'info-board.sqlite'
        with self.connection() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS posts(
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    body_markdown TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    author TEXT NOT NULL,
                    author_kind TEXT NOT NULL,
                    created REAL NOT NULL,
                    updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY,
                    at REAL NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    actor_kind TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS posts_updated ON posts(updated DESC, id);
            ''')

    @contextmanager
    def connection(self, write=False):
        c = sqlite3.connect(self.db, timeout=10)
        c.row_factory = sqlite3.Row
        if write:
            c.execute('BEGIN IMMEDIATE')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    # -- identity -------------------------------------------------------------
    def audit(self, c, action, object_id, data, actor='local-admin', actor_kind='agent'):
        # Only identity-level metadata may be stored here. Bodies stay in posts
        # so the audit log cannot become a second copy of published content.
        c.execute('INSERT INTO audit(at,action,actor,actor_kind,object_id,data) VALUES(?,?,?,?,?,?)',
                  (time.time(), action, actor, actor_kind, object_id, encoded(data)))

    # -- posts ----------------------------------------------------------------
    def _post(self, row):
        return {'id': row['id'], 'title': row['title'], 'body_markdown': row['body_markdown'],
                'tags': json.loads(row['tags']), 'author': row['author'],
                'author_kind': row['author_kind'], 'created': row['created'], 'updated': row['updated']}

    def get(self, post_id):
        with self.connection() as c:
            row = c.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
        if not row:
            raise InfoBoardError('信息不存在或已删除', 404, 'not_found')
        return self._post(row)

    def create(self, title, body_markdown, tags=None, author='local-admin', author_kind='agent'):
        title, body, tags = _title(title), _body(body_markdown), _tags(tags)
        if not isinstance(author, str) or not author.strip():
            raise InfoBoardError('缺少发布者标识')
        post_id, now = uuid.uuid4().hex, time.time()
        with self.connection(True) as c:
            c.execute('''INSERT INTO posts(id,title,body_markdown,tags,author,author_kind,created,updated)
                         VALUES(?,?,?,?,?,?,?,?)''',
                      (post_id, title, body, encoded(tags), author.strip(), author_kind, now, now))
            self.audit(c, 'info.create', post_id,
                       {'id': post_id, 'title': title, 'size': len(body.encode('utf-8'))},
                       author.strip(), author_kind)
        return self.get(post_id)

    def update(self, post_id, title=None, body_markdown=None, tags=None,
               expected_updated=None, author='local-admin', author_kind='agent'):
        with self.connection(True) as c:
            row = c.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
            if not row:
                raise InfoBoardError('信息不存在或已删除', 404, 'not_found')
            if expected_updated is not None:
                if isinstance(expected_updated, bool) or not isinstance(expected_updated, (int, float)):
                    raise InfoBoardError('expected_updated 必须是时间戳')
                if abs(float(expected_updated) - row['updated']) > 1e-6:
                    raise InfoBoardError('信息已被更新，请重新读取后再修改', 409, 'conflict')
            new_title = row['title'] if title is None else _title(title)
            new_body = row['body_markdown'] if body_markdown is None else _body(body_markdown)
            new_tags = json.loads(row['tags']) if tags is None else _tags(tags)
            now = time.time()
            # Keep updates strictly ordered even on coarse clocks so a stale
            # expected_updated always loses the race.
            if now <= row['updated']:
                now = row['updated'] + 1e-6
            c.execute('UPDATE posts SET title=?,body_markdown=?,tags=?,updated=? WHERE id=?',
                      (new_title, new_body, encoded(new_tags), now, post_id))
            self.audit(c, 'info.update', post_id,
                       {'id': post_id, 'title': new_title, 'size': len(new_body.encode('utf-8'))},
                       author, author_kind)
        return self.get(post_id)

    def delete(self, post_id, author='local-admin', author_kind='agent'):
        with self.connection(True) as c:
            row = c.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
            if not row:
                raise InfoBoardError('信息不存在或已删除', 404, 'not_found')
            c.execute('DELETE FROM posts WHERE id=?', (post_id,))
            self.audit(c, 'info.delete', post_id,
                       {'id': post_id, 'title': row['title'],
                        'size': len(row['body_markdown'].encode('utf-8'))},
                       author, author_kind)
        return {'deleted': True, 'id': post_id}

    def list_posts(self, query='', offset=0, limit=DEFAULT_LIMIT):
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            raise InfoBoardError('offset 必须是非负整数')
        try:
            limit = max(1, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError):
            raise InfoBoardError('limit 必须是正整数')
        query = '' if query is None else str(query).strip()[:200]
        where, args = ['1=1'], []
        if query:
            value = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            where.append("(title LIKE ? ESCAPE '\\' OR body_markdown LIKE ? ESCAPE '\\'"
                         " OR tags LIKE ? ESCAPE '\\')")
            args.extend([value, value, value])
        with self.connection() as c:
            rows = c.execute('SELECT * FROM posts WHERE ' + ' AND '.join(where)
                             + ' ORDER BY updated DESC,id LIMIT ? OFFSET ?',
                             (*args, limit + 1, offset)).fetchall()
        items = [{'id': row['id'], 'title': row['title'], 'summary': summary(row['body_markdown']),
                  'tags': json.loads(row['tags']), 'author': row['author'],
                  'updated': row['updated']}
                 for row in rows[:limit]]
        return {'items': items, 'next_offset': offset + limit if len(rows) > limit else None,
                'offset': offset, 'limit': limit}

    def audit_rows(self, limit=100):
        limit = max(1, min(int(limit), 1000))
        with self.connection() as c:
            return [dict(row) | {'data': json.loads(row['data'])}
                    for row in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,))]

    def stats(self):
        with self.connection() as c:
            posts = c.execute('SELECT count(*) FROM posts').fetchone()[0]
            updated = c.execute('SELECT max(updated) FROM posts').fetchone()[0]
        return {'posts': posts, 'updated': updated}
