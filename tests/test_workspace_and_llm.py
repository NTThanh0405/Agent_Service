import pytest

from bizagent.llm.registry import LLMConfigError, check_provider_ready, resolve_model
from bizagent.workspace import AgentsConfig, WorkspaceError, expand_env, load_workspace

from .conftest import WORKSPACES


def test_load_demo_and_template():
    demo = load_workspace(str(WORKSPACES / "demo"))
    assert demo.name == "demo"
    assert demo.sources_for_role("admin") == ["sales", "ke_hoach", "warehouse"]
    assert demo.sources_for_role("manager") == ["sales", "warehouse"]
    assert demo.sources_for_role("staff") == []
    assert demo.sources_for_role(None) == []
    tpl = load_workspace(str(WORKSPACES / "_template"))
    assert "sales" in tpl.sources


def test_expand_env(monkeypatch):
    monkeypatch.setenv("X_HOST", "db.local")
    monkeypatch.delenv("X_PORT", raising=False)
    assert expand_env("h=${X_HOST}:${X_PORT:-5432}") == "h=db.local:5432"
    assert expand_env({"a": ["${WORKSPACE_DIR}/x"]}, {"WORKSPACE_DIR": "/w"}) == {"a": ["/w/x"]}
    monkeypatch.delenv("X_MISSING", raising=False)
    with pytest.raises(WorkspaceError):
        expand_env("${X_MISSING}")


def test_unknown_role_source(tmp_path):
    (tmp_path / "workspace.yaml").write_text("name: t\nroles: {admin: [nope]}\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="không tồn tại"):
        load_workspace(str(tmp_path))


def test_resolve_model_aliases():
    cfg = AgentsConfig(
        default_model="claude:claude-sonnet-5-5",
        models={
            "fast": "anthropic:claude-haiku-5-5",
            "g": "gemini:some-model",
            "chain": "fast",
            "local": {"provider": "openai", "model": "qwen", "base_url": "http://localhost:1234/v1"},
        },
    )
    assert resolve_model(cfg).label == "anthropic:claude-sonnet-5-5"
    assert resolve_model(cfg, "chain").label == "anthropic:claude-haiku-5-5"
    assert resolve_model(cfg, "g").provider == "google_genai"
    assert resolve_model(cfg, "chatgpt:x").provider == "openai"
    local = resolve_model(cfg, "local")
    assert (local.provider, local.base_url) == ("openai", "http://localhost:1234/v1")
    assert resolve_model(cfg).capabilities.prompt_caching
    assert not local.capabilities.prompt_caching
    with pytest.raises(LLMConfigError):
        resolve_model(cfg, "unknown:model")
    with pytest.raises(LLMConfigError):
        resolve_model(cfg, "no-colon")


def test_provider_ready_reports_missing_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    problems = check_provider_ready(resolve_model(AgentsConfig(), "deepseek:x"))
    assert any("DEEPSEEK_API_KEY" in p for p in problems)
