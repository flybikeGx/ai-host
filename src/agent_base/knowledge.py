"""Versioned project knowledge, publication transactions and scoped agent access."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager


class KnowledgeError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def candidate(value):
    if not isinstance(value, dict) or set(value) - {'title', 'summary', 'body_markdown', 'tags', 'topic_path', 'slug'}:
        raise KnowledgeError('候选文档字段不正确')
    result = {key: value.get(key, '') for key in ('title', 'summary', 'body_markdown', 'topic_path', 'slug')}
    for key, limit in (('title', 120), ('summary', 300), ('topic_path', 160), ('slug', 100)):
        if not isinstance(result[key], str) or len(result[key]) > limit:
            raise KnowledgeError(f'{key} 超过限制或格式错误')
        result[key] = result[key].strip()
    if not result['title'] or not result['summary'] or not isinstance(result['body_markdown'], str) or not result['body_markdown'].strip():
        raise KnowledgeError('标题、摘要与正文不能为空')
    if len(result['body_markdown'].encode()) > 131072:
        raise KnowledgeError('正文超过 128 KiB', 413)
    if result['slug'] and not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}', result['slug']):
        raise KnowledgeError('slug 只支持小写字母、数字、短横线和下划线')
    tags = value.get('tags', [])
    if not isinstance(tags, list) or len(tags) > 10 or any(not isinstance(t, str) or not t.strip() or len(t) > 32 for t in tags):
        raise KnowledgeError('标签最多 10 个，每个最多 32 字符')
    result['tags'] = list(dict.fromkeys(t.strip() for t in tags))
    return result


def sections(body):
    # Fenced code headings are content, not section delimiters.
    starts, offset, fence = [(0, '开头', 0)], 0, None
    for line in body.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            kind = marker[1][0]
            if fence is None:
                fence = (kind, len(marker[1]))
            elif kind == fence[0] and len(marker[1]) >= fence[1]:
                fence = None
        heading = re.match(r'^(#{1,6})\s+(.+?)\s*#*\s*$', line)
        if heading and fence is None:
            if offset == 0:
                starts = []
            starts.append((offset, heading[2], len(heading[1])))
        offset += len(line)
    return [{'id': f's{i}', 'title': title, 'level': level, 'start': start,
             'end': starts[i+1][0] if i+1 < len(starts) else len(body)}
            for i, (start, title, level) in enumerate(starts)]


class Knowledge:
    def __init__(self, config):
        self.config = config
        self.root = config.data / 'knowledge'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / 'knowledge.sqlite'
        with self.connection() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_version VALUES(1);
                CREATE TABLE IF NOT EXISTS namespaces(id TEXT PRIMARY KEY,slug TEXT UNIQUE NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS bindings(actor TEXT PRIMARY KEY,definition TEXT NOT NULL,revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,namespace_id TEXT NOT NULL REFERENCES namespaces(id),slug TEXT NOT NULL,current_version_id TEXT,status TEXT NOT NULL,updated REAL NOT NULL,UNIQUE(namespace_id,slug));
                CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES documents(id),number INTEGER NOT NULL,parent_id TEXT,candidate TEXT NOT NULL,sections TEXT NOT NULL,source TEXT NOT NULL,created REAL NOT NULL,UNIQUE(document_id,number));
                CREATE TABLE IF NOT EXISTS submissions(id TEXT PRIMARY KEY,namespace_id TEXT NOT NULL REFERENCES namespaces(id),operation TEXT NOT NULL,document_id TEXT,base_version_id TEXT,candidate TEXT NOT NULL,content_hash TEXT NOT NULL,reason TEXT NOT NULL,evidence TEXT NOT NULL,actor TEXT NOT NULL,source TEXT NOT NULL,status TEXT NOT NULL,review_note TEXT,version_id TEXT,supersedes TEXT,created REAL NOT NULL,updated REAL NOT NULL,idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,UNIQUE(actor,namespace_id,idempotency_key));
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at REAL NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,object_id TEXT NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS grants(hash TEXT PRIMARY KEY,kind TEXT NOT NULL,definition TEXT NOT NULL,expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS documents_namespace ON documents(namespace_id,status,updated);
                CREATE INDEX IF NOT EXISTS submissions_namespace ON submissions(namespace_id,status,created);
            ''')
            try:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(document_id UNINDEXED,text,tokenize='trigram')")
                self.search_mode = 'fts5-trigram'
            except sqlite3.OperationalError:
                self.search_mode = 'literal'

    @contextmanager
    def connection(self, write=False):
        c = sqlite3.connect(self.db, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
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

    def audit(self, c, action, object_id, data, actor='local-admin'):
        c.execute('INSERT INTO audit(at,action,actor,object_id,data) VALUES(?,?,?,?,?)', (time.time(), action, actor, object_id, encoded(data)))

    def namespace(self, value, c=None, active=False):
        if c is None:
            with self.connection() as conn:
                return self.namespace(value, conn, active)
        row = c.execute('SELECT * FROM namespaces WHERE id=? OR slug=?', (value, value)).fetchone()
        if not row or (active and not row['enabled']):
            raise KnowledgeError('项目不存在或已停用', 404)
        return dict(row)

    def namespaces(self):
        with self.connection() as c:
            return [dict(r) for r in c.execute('SELECT n.*, (SELECT count(*) FROM documents d WHERE d.namespace_id=n.id AND d.status=\'published\') AS documents,(SELECT count(*) FROM submissions s WHERE s.namespace_id=n.id AND s.status=\'pending\') AS pending FROM namespaces n ORDER BY slug')]

    def save_namespace(self, value, namespace_id=None):
        if set(value) - {'slug', 'name', 'description', 'enabled'}:
            raise KnowledgeError('项目字段不正确')
        name, description = value.get('name', ''), value.get('description', '')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or not isinstance(description, str) or len(description) > 1000:
            raise KnowledgeError('项目名称/说明格式不正确')
        with self.connection(True) as c:
            if namespace_id:
                old = self.namespace(namespace_id, c)
                if value.get('slug', old['slug']) != old['slug']:
                    raise KnowledgeError('项目 slug 创建后不可更改')
                c.execute('UPDATE namespaces SET name=?,description=?,enabled=? WHERE id=?', (name.strip(), description, int(bool(value.get('enabled', True))), old['id']))
                namespace_id = old['id']
            else:
                slug = value.get('slug', '')
                if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', slug):
                    raise KnowledgeError('项目 slug 格式不正确')
                namespace_id = uuid.uuid4().hex
                try:
                    c.execute('INSERT INTO namespaces(id,slug,name,description) VALUES(?,?,?,?)', (namespace_id, slug, name.strip(), description))
                except sqlite3.IntegrityError:
                    raise KnowledgeError('项目 slug 已存在', 409)
            self.audit(c, 'namespace.save', namespace_id, value)
        return self.namespace(namespace_id)

    def binding(self, actor):
        with self.connection() as c:
            row = c.execute('SELECT * FROM bindings WHERE actor=?', (actor,)).fetchone()
            return {**json.loads(row['definition']), 'revision': row['revision'], 'actor': actor} if row else None

    def bindings(self):
        with self.connection() as c:
            return [{**json.loads(r['definition']), 'actor': r['actor'], 'revision': r['revision']} for r in c.execute('SELECT * FROM bindings ORDER BY actor')]

    def bind(self, actor, definition):
        if not isinstance(actor, str) or not re.fullmatch(r'admin|repo:[a-f0-9]{16}', actor):
            raise KnowledgeError('绑定主体必须是 Admin 或已安装 repo')
        if set(definition) - {'primary_namespace_id', 'read_namespace_ids', 'submit_namespace_ids', 'required', 'enabled', 'expected_revision'}:
            raise KnowledgeError('绑定字段不正确')
        reads, writes = definition.get('read_namespace_ids', []), definition.get('submit_namespace_ids', [])
        if not isinstance(reads, list) or not isinstance(writes, list) or len(reads)+len(writes) > 40 or any(not isinstance(n,str) for n in reads+writes):
            raise KnowledgeError('项目范围格式不正确')
        reads = list(dict.fromkeys(self.namespace(n, active=True)['id'] for n in reads))
        writes = list(dict.fromkeys(self.namespace(n, active=True)['id'] for n in writes))
        primary = self.namespace(definition['primary_namespace_id'], active=True)['id'] if definition.get('primary_namespace_id') else None
        if not set(writes) <= set(reads) or (primary and primary not in reads):
            raise KnowledgeError('投稿与主项目必须在可读范围内')
        value = dict(primary_namespace_id=primary, read_namespace_ids=reads, submit_namespace_ids=writes, required=bool(definition.get('required')), enabled=bool(definition.get('enabled', True)))
        with self.connection(True) as c:
            row = c.execute('SELECT revision FROM bindings WHERE actor=?', (actor,)).fetchone()
            revision = row[0] if row else 0
            if definition.get('expected_revision', 0) != revision:
                raise KnowledgeError('绑定已变化，请刷新', 409)
            c.execute('INSERT OR REPLACE INTO bindings VALUES(?,?,?)', (actor, encoded(value), revision+1))
            self.audit(c, 'binding.save', actor, value)
        return self.binding(actor)

    def issue(self, kind, definition, seconds):
        token = secrets.token_urlsafe(32)
        with self.connection(True) as c:
            c.execute('DELETE FROM grants WHERE expires<?', (time.time(),))
            c.execute('INSERT INTO grants VALUES(?,?,?,?)', (digest(token), kind, encoded(definition), time.time()+seconds))
        return token

    def grant(self, token, kind='agent', consume=False):
        with self.connection(consume) as c:
            row = c.execute('SELECT * FROM grants WHERE hash=? AND kind=? AND expires>?', (digest(token or ''), kind, time.time())).fetchone()
            if not row:
                raise KnowledgeError('授权无效或已过期', 401)
            value = json.loads(row['definition'])
            if consume:
                c.execute('DELETE FROM grants WHERE hash=?', (digest(token),))
        if kind == 'agent':
            binding = self.binding(value['actor'])
            if not binding or not binding['enabled'] or binding['revision'] != value['revision']:
                raise KnowledgeError('项目绑定已撤销或更改', 403)
            if value.get('pid'):
                from .processes import alive
                if not alive(value['pid'], value['birth']):
                    raise KnowledgeError('所属运行已退出', 403)
            if value.get('run_id'):
                from .store import Store
                job=Store(self.config.data).get(value['run_id'])
                if job['status'] not in {'preparing','running','verifying'} or job['cancel_requested']:
                    raise KnowledgeError('所属任务已结束或取消',403)
            if value.get('turn_id') and not value.get('admin_session'):
                from .store import Store
                with Store(self.config.data).connection() as c:
                    turn=c.execute('SELECT status,cancel_requested FROM conversation_turns WHERE id=?',(value['turn_id'],)).fetchone()
                if not turn or turn['status']!='running' or turn['cancel_requested']:
                    raise KnowledgeError('所属对话轮次已结束或取消',403)
        return value

    def scope(self, grant, namespace, submit=False):
        ns = self.namespace(namespace, active=grant is not None or submit)
        if grant is not None and ns['id'] not in grant['submit_namespace_ids' if submit else 'read_namespace_ids']:
            raise KnowledgeError('项目或对象不可见', 404)
        return ns['id']

    def document(self, document_id, version_id=None, grant=None):
        with self.connection() as c:
            row = c.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if not row:
                raise KnowledgeError('知识不存在', 404)
            self.scope(grant, row['namespace_id'])
            version = c.execute('SELECT * FROM versions WHERE id=? AND document_id=?', (version_id or row['current_version_id'], document_id)).fetchone()
            if not version:
                raise KnowledgeError('知识版本不存在', 404)
            return {**dict(row), 'version_id': version['id'], 'version_number': version['number'], 'is_current': version['id'] == row['current_version_id'],
                    'candidate': json.loads(version['candidate']), 'sections': json.loads(version['sections']), 'source': json.loads(version['source']), 'published_at': version['created']}

    def list_documents(self, namespace, query='', offset=0, limit=20, archived=False, grant=None):
        ns = self.scope(grant, namespace)
        limit, offset = max(1, min(int(limit), 50)), max(0, int(offset))
        query = str(query).strip()[:200]
        with self.connection() as c:
            where, args = ['d.namespace_id=?', 'd.status=?'], [ns, 'archived' if archived and grant is None else 'published']
            if query:
                if self.search_mode == 'fts5-trigram' and len(query) >= 3:
                    where.append('d.id IN (SELECT document_id FROM search WHERE search MATCH ?)')
                    args.append('"'+query.replace('"', '""')+'"')
                else:
                    where.append("(json_extract(v.candidate,'$.title') LIKE ? ESCAPE '\\' OR json_extract(v.candidate,'$.summary') LIKE ? ESCAPE '\\' OR json_extract(v.candidate,'$.tags') LIKE ? ESCAPE '\\')")
                    value = '%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                    args.extend([value]*3)
            rows = c.execute('SELECT d.*,v.number,v.candidate FROM documents d JOIN versions v ON v.id=d.current_version_id WHERE '+' AND '.join(where)+' ORDER BY d.updated DESC,d.id LIMIT ? OFFSET ?', (*args, limit+1, offset)).fetchall()
        items = []
        for r in rows[:limit]:
            value = json.loads(r['candidate'])
            items.append({'id': r['id'], 'namespace_id': ns, 'version_id': r['current_version_id'], 'version_number': r['number'], 'status': r['status'], 'updated': r['updated'],
                          **{k: value[k] for k in ('title','summary','tags','topic_path','slug')}})
        return {'items': items, 'next_offset': offset+limit if len(rows)>limit else None, 'search_mode': self.search_mode}

    def _publish(self, c, namespace, value, document_id, expected, source):
        self.namespace(namespace, c, active=True)
        now, version_id = time.time(), uuid.uuid4().hex
        if document_id:
            row = c.execute('SELECT * FROM documents WHERE id=? AND namespace_id=?', (document_id, namespace)).fetchone()
            if not row:
                raise KnowledgeError('知识不存在', 404)
            if not expected or row['current_version_id'] != expected:
                raise KnowledgeError('正式版本已变化，请比较当前内容后重新提交', 409)
            number = c.execute('SELECT number FROM versions WHERE id=?', (expected,)).fetchone()[0]+1
            if value['slug'] and value['slug'] != row['slug']:
                raise KnowledgeError('文档 slug 不可更改')
            value['slug'] = row['slug']
        else:
            if expected:
                raise KnowledgeError('新建知识不能携带基线版本')
            document_id, number = uuid.uuid4().hex, 1
            value['slug'] = value['slug'] or 'doc-'+document_id[:12]
            try:
                c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?)', (document_id, namespace, value['slug'], None, 'published', now))
            except sqlite3.IntegrityError:
                raise KnowledgeError('知识 slug 已存在', 409)
        c.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?,?)', (version_id, document_id, number, expected, encoded(value), encoded(sections(value['body_markdown'])), encoded(source), now))
        c.execute("UPDATE documents SET current_version_id=?,status='published',updated=? WHERE id=?", (version_id, now, document_id))
        if self.search_mode == 'fts5-trigram':
            c.execute('DELETE FROM search WHERE document_id=?', (document_id,))
            c.execute('INSERT INTO search VALUES(?,?)', (document_id, '\n'.join([value['title'],value['summary'],' '.join(value['tags']),value['body_markdown']])))
        self.audit(c, 'document.publish', document_id, {'version_id': version_id, 'parent_id': expected, 'source': source})
        return document_id, version_id

    def publish(self, namespace, value, document_id=None, expected=None):
        value, ns = candidate(value), self.scope(None, namespace)
        with self.connection(True) as c:
            document_id, _ = self._publish(c, ns, value, document_id, expected, {'actor':'local-admin'})
        return self.document(document_id)

    def archive(self, document_id, expected):
        with self.connection(True) as c:
            row = c.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if not row or row['current_version_id'] != expected:
                raise KnowledgeError('知识不存在或版本已变化', 409)
            c.execute("UPDATE documents SET status='archived',updated=? WHERE id=?", (time.time(),document_id))
            if self.search_mode == 'fts5-trigram':
                c.execute('DELETE FROM search WHERE document_id=?', (document_id,))
            self.audit(c, 'document.archive', document_id, {'version_id':expected})
        return self.document(document_id)

    def history(self, document_id):
        self.document(document_id)
        with self.connection() as c:
            return [dict(r) | {'source':json.loads(r['source'])} for r in c.execute('SELECT id,number,parent_id,created,source FROM versions WHERE document_id=? ORDER BY number DESC LIMIT 100', (document_id,))]

    def submit(self, grant, namespace, operation, value, reason, idempotency_key, document_id=None, base_version_id=None, evidence=None):
        ns, value = self.scope(grant, namespace, True), candidate(value)
        if operation not in {'create','update'} or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise KnowledgeError('投稿操作或理由不正确')
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 100:
            raise KnowledgeError('需要有效幂等键')
        evidence = evidence or []
        if not isinstance(evidence, list) or len(encoded(evidence)) > 8000:
            raise KnowledgeError('依据格式不正确或超过限制')
        if operation == 'update':
            doc = self.document(document_id, base_version_id, grant)
            if not base_version_id or doc['namespace_id'] != ns:
                raise KnowledgeError('修改投稿必须提供同项目文档和基线版本')
        elif document_id or base_version_id:
            raise KnowledgeError('新建投稿不能指定正式知识或基线')
        request_hash = digest(encoded([operation,value,reason,evidence,document_id,base_version_id]))
        with self.connection(True) as c:
            old = c.execute('SELECT id,request_hash FROM submissions WHERE actor=? AND namespace_id=? AND idempotency_key=?', (grant['actor'],ns,idempotency_key)).fetchone()
            if old:
                if old['request_hash'] != request_hash:
                    raise KnowledgeError('相同幂等键的投稿内容不同', 409)
                submission_id = old['id']
            else:
                submission_id, now = uuid.uuid4().hex, time.time()
                source = {k:v for k,v in grant.items() if k in {'actor','run_id','turn_id','installation_id','commit','admin_session'}}
                c.execute('INSERT INTO submissions(id,namespace_id,operation,document_id,base_version_id,candidate,content_hash,reason,evidence,actor,source,status,created,updated,idempotency_key,request_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,\'pending\',?,?,?,?)',
                          (submission_id,ns,operation,document_id,base_version_id,encoded(value),digest(encoded(value)),reason,encoded(evidence),grant['actor'],encoded(source),now,now,idempotency_key,request_hash))
                self.audit(c, 'submission.create', submission_id, {'namespace_id':ns}, grant['actor'])
        return self.submission(submission_id, grant)

    def _submission(self, row):
        result = dict(row)
        for key in ('candidate','evidence','source'):
            result[key] = json.loads(result[key])
        result.pop('request_hash', None)
        return result

    def submission(self, submission_id, grant=None):
        with self.connection() as c:
            row = c.execute('SELECT s.*,d.current_version_id FROM submissions s LEFT JOIN documents d ON d.id=s.document_id WHERE s.id=?', (submission_id,)).fetchone()
        if not row or (grant is not None and row['actor'] != grant['actor']):
            raise KnowledgeError('投稿不可见', 404)
        self.scope(grant, row['namespace_id'])
        result = self._submission(row)
        result['stale'] = result['operation']=='update' and result['base_version_id'] != result['current_version_id']
        return result

    def submissions(self, namespace, status='pending', grant=None, offset=0, limit=20):
        ns = self.scope(grant, namespace)
        if status not in {'pending','approved','rejected','superseded','all'}:
            raise KnowledgeError('投稿状态不正确')
        where, args = ['namespace_id=?'], [ns]
        if status != 'all':where.append('status=?');args.append(status)
        if grant is not None:where.append('actor=?');args.append(grant['actor'])
        limit,offset=max(1,min(int(limit),20)),max(0,int(offset))
        with self.connection() as c:
            rows=c.execute('SELECT id FROM submissions WHERE '+' AND '.join(where)+' ORDER BY created DESC,id LIMIT ? OFFSET ?',(*args,limit+1,offset)).fetchall()
        items=[]
        for row in rows[:limit]:
            item=self.submission(row['id'],grant)
            items.append({k:v for k,v in item.items() if k not in {'candidate','evidence'}} | {'title':item['candidate']['title']})
        return {'items':items,'next_offset':offset+limit if len(rows)>limit else None}

    def review(self, submission_id, action, expected_hash, expected_version, note='', amended=None):
        if action not in {'approve','reject','amend'} or not isinstance(note,str) or len(note)>2000:
            raise KnowledgeError('审核操作不正确')
        if action=='reject' and not note.strip():raise KnowledgeError('拒绝需填写原因')
        revised = candidate(amended) if action=='amend' else None
        with self.connection(True) as c:
            row=c.execute('SELECT * FROM submissions WHERE id=?',(submission_id,)).fetchone()
            if not row:raise KnowledgeError('投稿不存在',404)
            if row['status']!='pending' or row['content_hash']!=expected_hash:raise KnowledgeError('投稿已处理或内容变化，请刷新',409)
            self.namespace(row['namespace_id'],c,True)
            if action=='reject':
                c.execute("UPDATE submissions SET status='rejected',review_note=?,updated=? WHERE id=?",(note,time.time(),submission_id))
            else:
                # Plain approve can only publish the original, unchanged base.
                if action=='approve' and expected_version!=row['base_version_id']:raise KnowledgeError('审批基线与原投稿不一致',409)
                source=json.loads(row['source']) | {'submission_id':submission_id,'reviewer':'local-admin','amended':action=='amend'}
                document_id,version_id=self._publish(c,row['namespace_id'],revised or json.loads(row['candidate']),row['document_id'],expected_version,source)
                if revised:
                    # Preserve the original immutable candidate. The published
                    # admin revision is itself immutable and linked in source.
                    c.execute("UPDATE submissions SET status='superseded',review_note=?,version_id=?,document_id=?,updated=? WHERE id=?",(note,version_id,document_id,time.time(),submission_id))
                else:
                    c.execute("UPDATE submissions SET status='approved',review_note=?,version_id=?,document_id=?,updated=? WHERE id=?",(note,version_id,document_id,time.time(),submission_id))
            self.audit(c,'submission.'+action,submission_id,{'note':note})
        return self.submission(submission_id)

    def stats(self):
        with self.connection() as c:
            return {'namespaces':c.execute('SELECT count(*) FROM namespaces WHERE enabled=1').fetchone()[0],
                    'documents':c.execute("SELECT count(*) FROM documents WHERE status='published'").fetchone()[0],
                    'pending':c.execute("SELECT count(*) FROM submissions WHERE status='pending'").fetchone()[0], 'search_mode':self.search_mode}
