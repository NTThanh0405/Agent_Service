"""Lớp model trung lập: một chuỗi "provider:model" -> chat model của LangChain.

Ví dụ trong agents.yaml:
    default_model: anthropic:claude-sonnet-5-5
    models:
      fast: anthropic:claude-haiku-5-5
      gpt: openai:<ten-model>            # hoặc chatgpt:<ten-model>
      gemini: google_genai:<ten-model>   # hoặc gemini:<ten-model>
      ds: deepseek:<ten-model>
      local:                             # API tương thích OpenAI (vLLM, LM Studio, OpenRouter...)
        provider: openai
        model: qwen2.5-14b-instruct
        base_url: http://localhost:1234/v1
        api_key_env: LOCAL_LLM_KEY

Graph không viết `if claude ...`: những khác biệt giữa các model nằm trong bảng CAPABILITIES.
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, field
from typing import Any

from bizagent.workspace import AgentsConfig, AgentSettings, ModelSpec


class LLMConfigError(Exception):
    pass


# Tên thân thiện -> tên provider của LangChain (init_chat_model)
PROVIDER_ALIASES: dict[str, str] = {
    "claude": "anthropic",
    "anthropic": "anthropic",
    "chatgpt": "openai",
    "gpt": "openai",
    "openai": "openai",
    "azure": "azure_openai",
    "azure_openai": "azure_openai",
    "gemini": "google_genai",
    "google": "google_genai",
    "google_genai": "google_genai",
    "vertex": "google_vertexai",
    "google_vertexai": "google_vertexai",
    "deepseek": "deepseek",
    "ollama": "ollama",
    "groq": "groq",
    "mistral": "mistralai",
    "mistralai": "mistralai",
    "bedrock": "bedrock_converse",
}

# provider -> (module cần có, gói pip, biến môi trường chứa API key mặc định)
PROVIDER_PACKAGES: dict[str, tuple[str, str, str | None]] = {
    "anthropic": ("langchain_anthropic", "langchain-anthropic", "ANTHROPIC_API_KEY"),
    "openai": ("langchain_openai", "langchain-openai", "OPENAI_API_KEY"),
    "azure_openai": ("langchain_openai", "langchain-openai", "AZURE_OPENAI_API_KEY"),
    "google_genai": ("langchain_google_genai", "langchain-google-genai", "GOOGLE_API_KEY"),
    "google_vertexai": ("langchain_google_vertexai", "langchain-google-vertexai", None),
    "deepseek": ("langchain_deepseek", "langchain-deepseek", "DEEPSEEK_API_KEY"),
    "ollama": ("langchain_ollama", "langchain-ollama", None),
    "groq": ("langchain_groq", "langchain-groq", "GROQ_API_KEY"),
    "mistralai": ("langchain_mistralai", "langchain-mistralai", "MISTRAL_API_KEY"),
    "bedrock_converse": ("langchain_aws", "langchain-aws", None),
}


@dataclass(frozen=True)
class ModelCapabilities:
    tool_calling: bool = True          # model gọi được tool (bắt buộc với agent truy vấn dữ liệu)
    prompt_caching: bool = False       # đánh dấu cache cho system prompt dài (giảm chi phí)
    accepts_temperature: bool = True   # False: không bao giờ gửi temperature


CAPABILITIES: dict[str, ModelCapabilities] = {
    "anthropic": ModelCapabilities(prompt_caching=True, accepts_temperature=False),
    "bedrock_converse": ModelCapabilities(),
}
DEFAULT_CAPABILITIES = ModelCapabilities()


def capabilities_for(provider: str) -> ModelCapabilities:
    return CAPABILITIES.get(provider, DEFAULT_CAPABILITIES)


@dataclass
class ResolvedModel:
    provider: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def capabilities(self) -> ModelCapabilities:
        return capabilities_for(self.provider)


def _parse(ref: str) -> ResolvedModel:
    if ":" not in ref:
        raise LLMConfigError(f"Model '{ref}' phải có dạng provider:model (vd anthropic:claude-sonnet-5-5)")
    prov, model = ref.split(":", 1)
    provider = PROVIDER_ALIASES.get(prov.strip().lower())
    if provider is None:
        raise LLMConfigError(f"Nhà cung cấp '{prov}' chưa hỗ trợ. Có: {sorted(set(PROVIDER_ALIASES))}")
    if not model.strip():
        raise LLMConfigError(f"Model '{ref}' thiếu tên model sau dấu ':'")
    return ResolvedModel(provider=provider, model=model.strip())


def resolve_model(cfg: AgentsConfig, ref: str | None = None) -> ResolvedModel:
    """Đổi tên model trong agents.yaml ("default", alias, hoặc "provider:model") thành ResolvedModel."""
    ref = ref or cfg.agent.model
    if ref == "default":
        ref = cfg.default_model
    seen: set[str] = set()
    while ref in cfg.models:                    # alias có thể trỏ tới alias khác
        if ref in seen:
            raise LLMConfigError(f"Alias model bị vòng lặp: {ref}")
        seen.add(ref)
        target = cfg.models[ref]
        if isinstance(target, ModelSpec):
            provider = PROVIDER_ALIASES.get(target.provider.lower())
            if provider is None:
                raise LLMConfigError(f"Nhà cung cấp '{target.provider}' chưa hỗ trợ")
            return ResolvedModel(provider, target.model, target.base_url, target.api_key_env, dict(target.params))
        ref = target
        if ref == "default":
            ref = cfg.default_model
    return _parse(ref)


def check_provider_ready(rm: ResolvedModel) -> list[str]:
    """Danh sách vấn đề (thiếu gói, thiếu API key). Rỗng = sẵn sàng."""
    problems: list[str] = []
    module, package, key_env = PROVIDER_PACKAGES.get(rm.provider, (None, None, None))
    if module and importlib.util.find_spec(module) is None:
        problems.append(f"chưa cài gói {package}: pip install {package}")
    key_env = rm.api_key_env or key_env
    if key_env and not os.environ.get(key_env):
        problems.append(f"chưa đặt biến môi trường {key_env}")
    return problems


def build_chat_model(rm: ResolvedModel, settings: AgentSettings):
    """Tạo chat model LangChain cho mọi nhà cung cấp qua init_chat_model."""
    module, package, _ = PROVIDER_PACKAGES.get(rm.provider, (None, None, None))
    if module and importlib.util.find_spec(module) is None:
        raise LLMConfigError(f"Model {rm.label} cần gói {package}. Cài: pip install {package}")

    from langchain.chat_models import init_chat_model

    kwargs: dict[str, Any] = {"max_tokens": settings.max_tokens, "timeout": settings.timeout_seconds}
    if settings.temperature is not None and rm.capabilities.accepts_temperature:
        kwargs["temperature"] = settings.temperature
    if rm.base_url:
        kwargs["base_url"] = rm.base_url
    if rm.api_key_env:
        key = os.environ.get(rm.api_key_env)
        if not key:
            raise LLMConfigError(f"Model {rm.label}: chưa đặt biến môi trường {rm.api_key_env}")
        kwargs["api_key"] = key
    kwargs.update(rm.params)
    return init_chat_model(rm.model, model_provider=rm.provider, **kwargs)
