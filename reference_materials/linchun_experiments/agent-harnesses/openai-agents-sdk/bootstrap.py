"""把 OpenAI Agents SDK 接到仓库 .env 里的 Humanlaya 代理。

已验证通路：/v1/responses HTTP（不是 websocket，也先不必切 chat completions）。
不把密钥写进代码。必须关掉 tracing，避免 SDK 再打 api.openai.com。
"""
from __future__ import annotations

import os
from pathlib import Path

from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


_FORCE_ENV_KEYS = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_CHAT_COMPLETIONS_URL",
}


def load_repo_env(path: Path = ENV_PATH) -> None:
    """读取仓库根目录 .env。OPENAI_* / MODEL 覆盖进程里的旧值，避免过期 key 卡住。"""
    if os.environ.get("HARNESS_ENV_LOCKED") == "1":
        return
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key in _FORCE_ENV_KEYS or key not in os.environ:
            os.environ[key] = value


def require_api_config() -> tuple[str, str, str]:
    load_repo_env()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    model = os.environ.get("MODEL", "gpt-5.6-sol-r1-staff").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY 为空：先填 /home/linchun/agent-harnesses/.env")
    if not base_url:
        raise SystemExit("OPENAI_BASE_URL 为空")
    return api_key, base_url, model


def make_client() -> AsyncOpenAI:
    api_key, base_url, _ = require_api_config()
    return AsyncOpenAI(api_key=api_key, base_url=base_url)


def configure_sdk(*, use_responses: bool = True):
    """配置默认 client / API，并关闭 tracing。返回 (client, model, provider)。"""
    from agents import (
        set_default_openai_api,
        set_default_openai_client,
        set_default_openai_responses_transport,
        set_tracing_disabled,
    )
    from agents.models.openai_provider import OpenAIProvider

    set_tracing_disabled(True)
    _, _, model = require_api_config()
    client = make_client()
    set_default_openai_client(client, use_for_tracing=False)
    set_default_openai_api("responses" if use_responses else "chat_completions")
    if use_responses:
        set_default_openai_responses_transport("http")
    provider = OpenAIProvider(
        openai_client=client,
        use_responses=use_responses,
        use_responses_websocket=False,
    )
    return client, model, provider
