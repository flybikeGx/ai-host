"""Test-only process entrypoint; no fixture HTTP routes are installed."""
import os
from pathlib import Path
import uvicorn
from agent_base.config import Config
from agent_base.web import create_app

config = Config.load(Path(os.environ['E2E_CONFIG']))
uvicorn.run(create_app(config, background=False), host='127.0.0.1', port=int(os.environ['E2E_PORT']), log_level='warning')
