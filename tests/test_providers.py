import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_base.config import Config, validate_provider_selection
from agent_base.engines import PatchLoader, dsh_patch


def write_config(tmp_path, *, providers="", provider="deepseek-official", model="fixture-model",
                 effort="high", command=None):
    file = tmp_path / "config.toml"
    file.write_text(
        '[storage]\nroot = "data"\n[queue]\nmax_concurrent = 2\n[runtime]\nengine = "dsh"\ncommand = '
        + json.dumps(command or [sys.executable, "fixture"])
        + f'\nprovider = "{provider}"\nmodel = "{model}"\neffort = "{effort}"\ntimeout_seconds = 15\n' + providers)
    return file


GATEWAY = '''
[providers.internal-gateway]
api = "openai-completions"
base_url = "https://llm.internal.example/v1"
api_key_env = "FAKE_GATEWAY_API_KEY"

[[providers.internal-gateway.models]]
id = "internal-flash"
context_window = 131072

[providers.internal-gateway.models.reasoning_efforts]
high = "high"

[[providers.internal-gateway.models]]
id = "internal-lite"
reasoning_efforts = false
'''


def test_official_config_without_providers_stays_compatible(tmp_path):
    configuration = Config.load(write_config(tmp_path))
    assert configuration.runtime["provider"] == "deepseek-official"
    assert configuration.runtime["providers"] == {}


def test_builtin_routes_stay_usable_beside_custom_providers(tmp_path):
    # `deepseek-account` is a built-in dsh route resolved by the DeepSeek
    # adapter; it must keep working with and without custom providers defined.
    configuration = Config.load(write_config(tmp_path, provider="deepseek-account", model="deepseek-flash"))
    assert configuration.runtime["provider"] == "deepseek-account"
    configuration = Config.load(write_config(tmp_path, providers=GATEWAY, provider="deepseek-account",
                                             model="deepseek-flash"))
    validate_provider_selection(configuration.runtime)
    assert "internal-gateway" in configuration.runtime["providers"]


def test_custom_provider_loads_and_snapshots_into_runtime(tmp_path):
    configuration = Config.load(write_config(tmp_path, providers=GATEWAY,
                                             provider="internal-gateway", model="internal-flash"))
    definition = configuration.runtime["providers"]["internal-gateway"]
    assert definition["api"] == "openai-completions"
    assert [m["id"] for m in definition["models"]] == ["internal-flash", "internal-lite"]
    assert definition["models"][0]["reasoning_efforts"] == {"high": "high"}
    assert definition["models"][1]["reasoning_efforts"] is False


@pytest.mark.parametrize("providers,provider,model,effort,message", [
    ("[providers.deepseek-official]\napi='openai-completions'\nbase_url='https://x.example/v1'\napi_key_env='K'\n"
     "[[providers.deepseek-official.models]]\nid='m'\n", "deepseek-official", "m", "high", "内置路由冲突"),
    ("[providers.gw]\napi='bad-protocol'\nbase_url='https://x.example/v1'\n[[providers.gw.models]]\nid='m'\n",
     "gw", "m", None, "api 必须"),
    ("[providers.gw]\napi='openai-completions'\nbase_url='ftp://x.example/v1'\n[[providers.gw.models]]\nid='m'\n",
     "gw", "m", None, "base_url"),
    ("[providers.gw]\napi='openai-completions'\nbase_url='https://user:pw@x.example/v1'\n[[providers.gw.models]]\nid='m'\n",
     "gw", "m", None, "不得包含凭据"),
    ("[providers.gw]\napi='openai-completions'\nbase_url='https://x.example/v1'\n",
     "gw", "m", None, "非空模型目录"),
    ("[providers.gw]\napi='openai-completions'\nbase_url='https://x.example/v1'\nunknown=true\n[[providers.gw.models]]\nid='m'\n",
     "gw", "m", None, "不支持的字段"),
    ("[providers.gw]\napi='openai-completions'\nbase_url='https://x.example/v1'\n[[providers.gw.models]]\nid='m'\nbogus=1\n",
     "gw", "m", None, "不支持的字段"),
    ("[providers.gw]\napi='openai-completions'\nbase_url='https://x.example/v1'\n[[providers.gw.models]]\nid='m'\n[[providers.gw.models]]\nid='m'\n",
     "gw", "m", None, "唯一非空 id"),
    ("[providers.gw]\napi='openai-completions'\nbase_url='https://x.example/v1'\n[[providers.gw.models]]\nid='m'\nreasoning_efforts=true\n",
     "gw", "m", None, "reasoning_efforts"),
    ("[providers.gw]\napi='openai-completions'\nbase_url='https://x.example/v1'\napi_key_env='not a name'\n[[providers.gw.models]]\nid='m'\n",
     "gw", "m", None, "api_key_env"),
    ("[providers.gw]\napi='openai-completions'\nbase_url='https://x.example/v1'\nadapter='other'\n[[providers.gw.models]]\nid='m'\n",
     "gw", "m", None, "pi-ai"),
    ("[providers.gw]\napi='openai-completions'\nbase_url='https://x.example/v1'\n[[providers.gw.models]]\nid='m'\ninput=['video']\n",
     "gw", "m", None, "input"),
    (GATEWAY, "missing-gateway", "internal-flash", "high", "未定义 provider"),
    (GATEWAY, "internal-gateway", "not-in-catalog", "high", "不在.*模型目录"),
    (GATEWAY, "internal-gateway", "internal-flash", "medium", "未声明推理档位"),
    (GATEWAY, "internal-gateway", "internal-lite", "high", "未声明推理档位"),
])
def test_invalid_provider_configuration_fails_closed(tmp_path, providers, provider, model, effort, message):
    file = write_config(tmp_path, providers=providers, provider=provider, model=model,
                        effort=effort if effort else "")
    text = file.read_text()
    if not effort:
        text = text.replace('\neffort = ""\n', "\n")
    file.write_text(text)
    with pytest.raises(ValueError, match=message):
        Config.load(file)


def test_selection_validation_uses_embedded_provider_definitions(tmp_path):
    configuration = Config.load(write_config(tmp_path, providers=GATEWAY,
                                             provider="internal-gateway", model="internal-flash"))
    runtime = {**configuration.runtime}
    runtime["model"] = "internal-lite"
    runtime["effort"] = "high"
    with pytest.raises(ValueError, match="未声明推理档位"):
        validate_provider_selection(runtime)
    runtime["effort"] = None
    validate_provider_selection(runtime)


def test_run_and_conversation_snapshots_carry_provider_definitions(tmp_path):
    from agent_base.service import Service
    from agent_base.conversations import Conversations
    configuration = Config.load(write_config(tmp_path, providers=GATEWAY,
                                             provider="internal-gateway", model="internal-flash"))
    spec = Service(configuration).specification("https://example.invalid/repo.git")
    assert spec["runtime"]["providers"]["internal-gateway"]["base_url"] == "https://llm.internal.example/v1"
    chat = Conversations(configuration).create("admin")
    assert chat["spec"]["runtime"]["providers"]["internal-gateway"]["api"] == "openai-completions"


def test_dsh_patch_inserts_pi_ai_route_and_credentials_by_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_GATEWAY_API_KEY", "secret-value-never-in-patch")
    configuration = Config.load(write_config(tmp_path, providers=GATEWAY,
                                             provider="internal-gateway", model="internal-flash"))
    patch = dsh_patch(configuration.runtime, tmp_path, admin_config=configuration.path)
    text = patch.read_text()
    assert "secret-value-never-in-patch" not in text
    rows = yaml.load(text, Loader=PatchLoader)
    default = next(row for row in rows if row.get("id") == "agent-default-model")["config"]
    assert default["provider"] == "internal-gateway" and default["model"] == "internal-flash"
    # The dormant dsh-base `llm-pi-ai` entry is configured in place, not
    # duplicated by a second inserted instance.
    pi_ai = next(row for row in rows if row.get("id") == "llm-pi-ai")
    profile = pi_ai["config"]["providers"]["internal-gateway"]
    assert profile["apiKeyEnv"] == "FAKE_GATEWAY_API_KEY"
    assert profile["baseURL"] == "https://llm.internal.example/v1"
    assert profile["models"][0]["contextWindow"] == 131072
    assert profile["models"][0]["reasoningEfforts"] == {"high": "high"}
    assert not any(item.get("id") == "agent-base-llm-pi-ai"
                   for row in rows if "insert" in row for item in row["insert"])
    admin = next(item for row in rows if "insert" in row for item in row["insert"]
                 if item["id"] == "agent-base-admin")
    assert "FAKE_GATEWAY_API_KEY" in admin["config"]["env"]


def test_dsh_patch_fails_fast_when_credential_env_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_GATEWAY_API_KEY", raising=False)
    configuration = Config.load(write_config(tmp_path, providers=GATEWAY,
                                             provider="internal-gateway", model="internal-flash"))
    with pytest.raises(ValueError, match="缺少 provider 凭据环境变量"):
        dsh_patch(configuration.runtime, tmp_path)


def test_doctor_reports_selected_provider_status(tmp_path, monkeypatch):
    fake = Path(__file__).parent / "fixtures" / "fake_dsh.py"
    configuration = Config.load(write_config(tmp_path, providers=GATEWAY, provider="internal-gateway",
                                             model="internal-flash", command=[sys.executable, str(fake)]))
    monkeypatch.setenv("FAKE_GATEWAY_API_KEY", "set")
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run([sys.executable, "-m", "agent_base", "--config", str(configuration.path), "doctor"],
                            capture_output=True, text=True, env=environment, timeout=60)
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["provider"] == "internal-gateway"
    assert report["credential_env"] == "FAKE_GATEWAY_API_KEY" and report["credential_configured"] is True
    monkeypatch.delenv("FAKE_GATEWAY_API_KEY")
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run([sys.executable, "-m", "agent_base", "--config", str(configuration.path), "doctor"],
                            capture_output=True, text=True, env=environment, timeout=60)
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["credential_configured"] is False
