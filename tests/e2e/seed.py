import sys
from pathlib import Path
from agent_base.config import Config
from agent_base.store import Store
from agent_base.governance.service import Governance

config = Config.load(Path(sys.argv[1]))
store = Store(config.data)
with store.connection() as conn:
    conn.execute("INSERT INTO conversations(id,title,target,spec,created,updated) VALUES('legacy','legacy','admin','{}',1,1)")
gov = Governance(config)
gov.bootstrap(admin_password='fixture-browser-password')
domains = [gov.store.list_domains()[0], gov.create_domain('other', 'Other')]
admin = gov.store.get_user_by_username('admin')
# The page surface resolves the current domain from home_domain_id (§7.2).
gov.store.update_user(admin['id'], home_domain_id=domains[0]['id'])
for name, role, domain in [('viewer','viewer',domains[0]), ('developer','developer',domains[0]), ('peer','developer',domains[0]), ('foreign','developer',domains[1])]:
    user = gov.create_user(name, password='fixture-member-password', display_name='测试成员', home_domain_id=domain['id'])
    gov.add_membership(user['id'], domain['id'])
    gov.grant_role(user['id'], gov.store.get_role_by_name(role)['id'], domain_id=domain['id'])
# A reader with task.read but no file.read: the artifact catalog must stay hidden.
task_reader = gov.create_role('task-reader', permissions=['agent.read', 'task.read'])
reader = gov.create_user('reader', password='fixture-member-password', display_name='只读成员', home_domain_id=domains[0]['id'])
gov.add_membership(reader['id'], domains[0]['id'])
gov.grant_role(reader['id'], task_reader['id'], domain_id=domains[0]['id'])
# Dedicated stream-revocation account: disabling it must end an open SSE stream.
revoked = gov.create_user('revoked', password='fixture-member-password', display_name='撤权成员', home_domain_id=domains[0]['id'])
gov.add_membership(revoked['id'], domains[0]['id'])
gov.grant_role(revoked['id'], gov.store.get_role_by_name('developer')['id'], domain_id=domains[0]['id'])
# Password-enabled service fixture avoids exporting any generated API key.
from agent_base.governance.auth import hash_password
service = gov.store.create_user('service', password=hash_password('fixture-member-password'), user_type='service', home_domain_id=domains[0]['id'])
gov.add_membership(service['id'], domains[0]['id'])
gov.grant_role(service['id'], gov.store.get_role_by_name('platform_admin')['id'])
# Real resources with independently assigned owners/domains for read isolation.
import json, time
for username, domain in [('developer',domains[0]),('peer',domains[0]),('foreign',domains[1]),
                         ('reader',domains[0]),('revoked',domains[0])]:
    user=gov.store.get_user_by_username(username)
    # Columns are deliberately absent until the real startup migration runs;
    # assignments are applied by the runner after first boot.
    with store.connection() as conn:
        conn.execute('INSERT INTO conversations(id,title,target,spec,created,updated) VALUES(?,?,?,?,?,?)',
                     (username+'-chat',username+' private','admin','{}',time.time(),time.time()))
    workspace=config.data/'conversations'/(username+'-chat')/'workspace/artifacts'
    workspace.mkdir(parents=True)
    (workspace/'report.txt').write_text(username+' isolated artifact\n')
# Two installed Agents in different domains; the runner assigns domains after migration.
for install_id, name in [('default-agent','Default Agent'),('other-agent','Other Agent')]:
    root = config.data / 'repos' / install_id
    root.mkdir(parents=True)
    manifest = {'schema_version': 1, 'package': {'name': name}, 'task': {'default': name + ' task'},
                'skills': [], 'mcp': {'servers': []}}
    with store.connection() as conn:
        conn.execute('INSERT INTO repo_installations(id,repo_id,origin,commit_sha,root,manifest,status,created,enabled)'
                     ' VALUES(?,?,?,?,?,?,?,?,?)',
                     (install_id, install_id, 'fixture', '0'*40, str(root), json.dumps(manifest),
                      'ready', time.time(), 1))
