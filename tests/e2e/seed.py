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
for name, role, domain in [('viewer','viewer',domains[0]), ('developer','developer',domains[0]), ('peer','developer',domains[0]), ('foreign','developer',domains[1])]:
    user = gov.create_user(name, password='fixture-member-password', display_name='测试成员', home_domain_id=domain['id'])
    gov.add_membership(user['id'], domain['id'])
    gov.grant_role(user['id'], gov.store.get_role_by_name(role)['id'], domain_id=domain['id'])
# Password-enabled service fixture avoids exporting any generated API key.
from agent_base.governance.auth import hash_password
service = gov.store.create_user('service', password=hash_password('fixture-member-password'), user_type='service', home_domain_id=domains[0]['id'])
gov.add_membership(service['id'], domains[0]['id'])
gov.grant_role(service['id'], gov.store.get_role_by_name('platform_admin')['id'])
# Real resources with independently assigned owners/domains for read isolation.
import json, time
for username, domain in [('developer',domains[0]),('peer',domains[0]),('foreign',domains[1])]:
    user=gov.store.get_user_by_username(username)
    # Columns are deliberately absent until the real startup migration runs;
    # assignments are applied by the runner after first boot.
    with store.connection() as conn:
        conn.execute('INSERT INTO conversations(id,title,target,spec,created,updated) VALUES(?,?,?,?,?,?)',
                     (username+'-chat',username+' private','admin','{}',time.time(),time.time()))
    workspace=config.data/'conversations'/(username+'-chat')/'workspace/artifacts'
    workspace.mkdir(parents=True)
    (workspace/'report.txt').write_text(username+' isolated artifact\n')
