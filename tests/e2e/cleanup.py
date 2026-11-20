"""Cancel only turns in this disposable config; stop only its knowledge service."""
import sys,time
from pathlib import Path
from agent_base.config import Config
from agent_base.conversations import Conversations
from agent_base.knowledge_service import KnowledgeService
config=Config.load(Path(sys.argv[1]))
chats=Conversations(config)
with chats.store.connection() as c:
    ids=[r[0] for r in c.execute("SELECT DISTINCT conversation_id FROM conversation_turns WHERE status IN ('queued','running')")]
for chat in ids: chats.cancel(chat)
for _ in range(100):
    chats.reconcile()
    with chats.store.connection() as c:
        if not c.execute("SELECT 1 FROM conversation_turns WHERE status IN ('queued','running')").fetchone(): break
    time.sleep(.1)
else: raise AssertionError('test turns still running; retain temp resources for investigation')
if (config.data/'knowledge/service.json').exists(): KnowledgeService(config).stop()
