"""Unified runtime configuration, .env loader, multi-profile store, and environment doctor.

Precedence (highest to lowest):
1. Explicit environment variables in ``base_env`` / ``os.environ``
2. Local ``.env`` file in workspace root (gitignored)
3. Active named profile in ``.private/agent_profile.json`` (gitignored)
4. Built-in safe defaults (cloud egress disabled by default)

Security invariants:
- Never executes shell expansions in ``.env``.
- Never echoes or logs API keys in views, probes, or doctor reports.
- Atomic file writes with restrictive ``0o600`` permissions for local profiles.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pcb_assistant.catalog.common import CatalogError, DEFAULT_CATALOG, json_text
from pcb_assistant.paths import ROOT, python_executable


PROFILE_SCHEMA = "pcb-agent-profiles-v2"
DEFAULT_PROFILE_NAME = "default"
DEFAULT_PROFILE_PATH = ROOT / ".private" / "agent_profile.json"
DEFAULT_DOTENV_PATH = ROOT / ".env"
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
DOTENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

ALLOWED_PROFILE_ENV_KEYS = (
    "PCB_ALLOW_CLOUD_EGRESS",
    "PCB_EGRESS_SCOPE",
    "PCB_AGENT_LOOP",
    "PCB_AGENT_BACKEND",
    "PCB_AGENT_MAX_STEPS",
    "PCB_AGENT_MAX_TOOL_CALLS",
    "PCB_LLM_PROVIDER",
    "PCB_LLM_BASE_URL",
    "PCB_LLM_MODEL",
    "PCB_LLM_API_KEY_ENV",
    "PCB_LLM_TIMEOUT_SECONDS",
    "PCB_LLM_MAX_OUTPUT_TOKENS",
    "PCB_ALLOW_LOCALHOST_HTTP",
    "PCB_MAX_EVIDENCE_CHARS",
)

ALLOWED_DOTENV_KEYS = set(ALLOWED_PROFILE_ENV_KEYS) | {
    "PCB_CATALOG",
    "PCB_PROFILE",
    "PCB_WEB_HOST",
    "PCB_WEB_PORT",
    "PCB_WEB_API_TOKEN",
    "PCB_DISABLE_LOCAL_CONFIG",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "SILICONFLOW_API_KEY",
    "OLLAMA_API_KEY",
}

PRESET_TEMPLATES = (
    {
        "id": "deepseek",
        "label": "DeepSeek 官方 API",
        "PCB_ALLOW_CLOUD_EGRESS": True,
        "PCB_AGENT_LOOP": True,
        "PCB_EGRESS_SCOPE": "sanitized_catalog",
        "PCB_LLM_PROVIDER": "openai_chat_completions",
        "PCB_LLM_BASE_URL": "https://api.deepseek.com/v1",
        "PCB_LLM_MODEL": "deepseek-chat",
        "PCB_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
        "PCB_ALLOW_LOCALHOST_HTTP": False,
    },
    {
        "id": "newapi_gateway",
        "label": "OpenAI 兼容网关 (NewAPI / OneAPI / 硅基流动 / 百炼)",
        "PCB_ALLOW_CLOUD_EGRESS": True,
        "PCB_AGENT_LOOP": True,
        "PCB_EGRESS_SCOPE": "sanitized_catalog",
        "PCB_LLM_PROVIDER": "openai_chat_completions",
        "PCB_LLM_BASE_URL": "https://your-gateway.example.com/v1",
        "PCB_LLM_MODEL": "deepseek-chat",
        "PCB_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
        "PCB_ALLOW_LOCALHOST_HTTP": False,
    },
    {
        "id": "local_ollama",
        "label": "本机 Ollama / vLLM / LM Studio (免出境)",
        "PCB_ALLOW_CLOUD_EGRESS": True,
        "PCB_AGENT_LOOP": True,
        "PCB_EGRESS_SCOPE": "sanitized_catalog",
        "PCB_LLM_PROVIDER": "openai_chat_completions",
        "PCB_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
        "PCB_LLM_MODEL": "qwen2.5:14b",
        "PCB_LLM_API_KEY_ENV": "OLLAMA_API_KEY",
        "PCB_ALLOW_LOCALHOST_HTTP": True,
    },
    {
        "id": "openai_responses",
        "label": "OpenAI Responses API",
        "PCB_ALLOW_CLOUD_EGRESS": True,
        "PCB_AGENT_LOOP": True,
        "PCB_EGRESS_SCOPE": "public_only",
        "PCB_LLM_PROVIDER": "openai_responses",
        "PCB_LLM_BASE_URL": "https://api.openai.com/v1",
        "PCB_LLM_MODEL": "gpt-4.1-mini",
        "PCB_LLM_API_KEY_ENV": "OPENAI_API_KEY",
        "PCB_ALLOW_LOCALHOST_HTTP": False,
    },
    {
        "id": "offline",
        "label": "纯本地离线模式（零网络调用）",
        "PCB_ALLOW_CLOUD_EGRESS": False,
        "PCB_AGENT_LOOP": False,
        "PCB_EGRESS_SCOPE": "public_only",
        "PCB_LLM_PROVIDER": "openai_chat_completions",
        "PCB_LLM_BASE_URL": "",
        "PCB_LLM_MODEL": "",
        "PCB_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
        "PCB_ALLOW_LOCALHOST_HTTP": False,
    },
)


def validate_profile_name(name: str) -> str:
    clean = (name or "").strip()
    if not PROFILE_NAME_RE.fullmatch(clean):
        raise CatalogError("invalid_profile_name")
    return clean


def parse_dotenv(path: Path | None) -> dict[str, str]:
    """Parse a local .env file safely without shell interpolation or code execution."""
    if path is None or not Path(path).is_file():
        return {}
    try:
        content = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CatalogError("invalid_dotenv_file") from exc

    parsed: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = DOTENV_LINE_RE.match(line)
        if not match:
            continue
        key, raw_val = match.group(1), match.group(2).strip()
        if not KEY_NAME_RE.fullmatch(key):
            continue
        if len(raw_val) >= 2 and raw_val[0] == raw_val[-1] and raw_val[0] in ("'", '"'):
            val = raw_val[1:-1]
        else:
            # Strip inline comments only when preceded by whitespace in unquoted values.
            val = re.sub(r"\s+#.*$", "", raw_val).strip()
        parsed[key] = val

    # Allow custom API key variable referenced by PCB_LLM_API_KEY_ENV in the same .env.
    dynamic_key = parsed.get("PCB_LLM_API_KEY_ENV", "").strip()
    allowed = set(ALLOWED_DOTENV_KEYS)
    if KEY_NAME_RE.fullmatch(dynamic_key):
        allowed.add(dynamic_key)
    return {k: v for k, v in parsed.items() if k in allowed and v != ""}


def _normalize_entry(raw: dict) -> dict:
    entry: dict = {}
    if isinstance(raw.get("label"), str) and raw["label"].strip():
        entry["label"] = raw["label"].strip()[:80]
    for key in ALLOWED_PROFILE_ENV_KEYS:
        if key not in raw or raw[key] is None:
            continue
        val = raw[key]
        if isinstance(val, bool):
            entry[key] = val
        else:
            entry[key] = str(val).strip()
    if isinstance(raw.get("api_key"), str) and raw["api_key"].strip():
        entry["api_key"] = raw["api_key"].strip()
    return entry


def load_profile_store(profile_path: Path | None = None) -> tuple[dict, bool]:
    """Load v1 (flat) or v2 (multi-profile) store into normalized v2 structure."""
    target = Path(profile_path) if profile_path is not None else DEFAULT_PROFILE_PATH
    if not target.is_file():
        return {
            "schema": PROFILE_SCHEMA,
            "active_profile": DEFAULT_PROFILE_NAME,
            "profiles": {},
        }, False
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise CatalogError("invalid_agent_profile") from exc
    if not isinstance(raw, dict):
        raise CatalogError("invalid_agent_profile")

    if isinstance(raw.get("profiles"), dict):
        profiles: dict[str, dict] = {}
        for name, item in raw["profiles"].items():
            if not isinstance(name, str) or not PROFILE_NAME_RE.fullmatch(name.strip()):
                continue
            if isinstance(item, dict):
                profiles[name.strip()] = _normalize_entry(item)
        active = str(raw.get("active_profile") or DEFAULT_PROFILE_NAME).strip()
        if active not in profiles:
            active = next(iter(profiles), DEFAULT_PROFILE_NAME)
        return {
            "schema": PROFILE_SCHEMA,
            "active_profile": active,
            "profiles": profiles,
        }, True

    # Legacy v1 flat profile -> normalize seamlessly as "default".
    return {
        "schema": PROFILE_SCHEMA,
        "active_profile": DEFAULT_PROFILE_NAME,
        "profiles": {DEFAULT_PROFILE_NAME: _normalize_entry(raw)},
    }, True


def _write_profile_store(profile_path: Path, store: dict) -> None:
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": PROFILE_SCHEMA,
        "active_profile": store.get("active_profile") or DEFAULT_PROFILE_NAME,
        "profiles": store.get("profiles") or {},
    }
    tmp = profile_path.with_suffix(".json.tmp")
    tmp.write_text(json_text(payload, pretty=True) + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(profile_path)


def load_runtime_env(
    profile_path: Path | None = None,
    *,
    profile_name: str | None = None,
    dotenv_path: Path | None = None,
    base_env: dict[str, str] | None = None,
    include_default_dotenv: bool = False,
) -> tuple[dict[str, str], bool]:
    """Merge base_env > .env > active profile into a clean environment dict."""
    explicit_env = dict(os.environ if base_env is None else base_env)
    if explicit_env.get("PCB_DISABLE_LOCAL_CONFIG", "").strip().lower() in ("1", "true", "yes"):
        return explicit_env, False

    merged = dict(explicit_env)
    loaded_any = False

    effective_dotenv = dotenv_path
    if effective_dotenv is None and include_default_dotenv:
        effective_dotenv = DEFAULT_DOTENV_PATH
    if effective_dotenv is not None and Path(effective_dotenv).is_file():
        dotenv_values = parse_dotenv(Path(effective_dotenv))
        if dotenv_values:
            loaded_any = True
            for k, v in dotenv_values.items():
                if not merged.get(k):
                    merged[k] = v

    store, store_loaded = load_profile_store(profile_path)
    if store_loaded:
        loaded_any = True
        selected = (
            (profile_name or "").strip()
            or merged.get("PCB_PROFILE", "").strip()
            or store["active_profile"]
        )
        entry = store["profiles"].get(selected) or store["profiles"].get(store["active_profile"]) or {}
        for key in ALLOWED_PROFILE_ENV_KEYS:
            if key in entry and entry[key] is not None and not merged.get(key):
                val = entry[key]
                if isinstance(val, bool):
                    merged[key] = "true" if val else "false"
                elif str(val).strip() != "":
                    merged[key] = str(val).strip()
        key_name = merged.get("PCB_LLM_API_KEY_ENV", "OPENAI_API_KEY").strip()
        if (
            isinstance(entry.get("api_key"), str)
            and entry["api_key"].strip()
            and KEY_NAME_RE.fullmatch(key_name)
            and not merged.get(key_name)
        ):
            merged[key_name] = entry["api_key"].strip()

    return merged, loaded_any


@contextmanager
def runtime_env_context(
    profile_path: Path | None = None,
    *,
    profile_name: str | None = None,
    dotenv_path: Path | None = None,
    include_default_dotenv: bool = True,
    ignore_errors: bool = False,
):
    """Temporarily apply unified config to os.environ and restore on exit."""
    try:
        env, _ = load_runtime_env(
            profile_path,
            profile_name=profile_name,
            dotenv_path=dotenv_path,
            include_default_dotenv=include_default_dotenv,
        )
    except CatalogError:
        if not ignore_errors:
            raise
        yield False
        return

    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            os.environ[k] = v
        yield True
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def safe_profile_view(
    profile_path: Path | None = None,
    *,
    profile_name: str | None = None,
    dotenv_path: Path | None = None,
    base_env: dict[str, str] | None = None,
) -> dict:
    """Return secret-free view of the active profile plus named profile list and presets."""
    target = Path(profile_path) if profile_path is not None else DEFAULT_PROFILE_PATH
    store, store_loaded = load_profile_store(target)
    selected = (profile_name or "").strip() or store["active_profile"]
    if selected not in store["profiles"] and store["profiles"]:
        selected = store["active_profile"]

    env, loaded = load_runtime_env(
        target,
        profile_name=selected,
        dotenv_path=dotenv_path,
        base_env=base_env,
        include_default_dotenv=(base_env is None and dotenv_path is None and target == DEFAULT_PROFILE_PATH),
    )
    key_env = env.get("PCB_LLM_API_KEY_ENV", "DEEPSEEK_API_KEY").strip() or "DEEPSEEK_API_KEY"

    profiles_summary = []
    for name, item in store["profiles"].items():
        item_key_env = str(item.get("PCB_LLM_API_KEY_ENV") or "DEEPSEEK_API_KEY").strip()
        has_key = bool(item.get("api_key")) or bool((os.environ if base_env is None else base_env).get(item_key_env))
        profiles_summary.append({
            "name": name,
            "label": item.get("label") or name,
            "active": name == selected,
            "PCB_ALLOW_CLOUD_EGRESS": str(item.get("PCB_ALLOW_CLOUD_EGRESS", "false")).lower() == "true"
            if not isinstance(item.get("PCB_ALLOW_CLOUD_EGRESS"), bool)
            else item["PCB_ALLOW_CLOUD_EGRESS"],
            "PCB_EGRESS_SCOPE": item.get("PCB_EGRESS_SCOPE") or "public_only",
            "PCB_LLM_PROVIDER": item.get("PCB_LLM_PROVIDER") or "openai_chat_completions",
            "PCB_LLM_BASE_URL": item.get("PCB_LLM_BASE_URL") or "",
            "PCB_LLM_MODEL": item.get("PCB_LLM_MODEL") or "",
            "PCB_LLM_API_KEY_ENV": item_key_env,
            "PCB_ALLOW_LOCALHOST_HTTP": str(item.get("PCB_ALLOW_LOCALHOST_HTTP", "false")).lower() == "true"
            if not isinstance(item.get("PCB_ALLOW_LOCALHOST_HTTP"), bool)
            else item["PCB_ALLOW_LOCALHOST_HTTP"],
            "has_api_key": has_key,
        })

    return {
        "profile_loaded": loaded or store_loaded,
        "active_profile": selected,
        "profiles": profiles_summary,
        "presets": list(PRESET_TEMPLATES),
        "PCB_ALLOW_CLOUD_EGRESS": env.get("PCB_ALLOW_CLOUD_EGRESS", "false").strip().lower() == "true",
        "PCB_AGENT_LOOP": env.get("PCB_AGENT_LOOP", "true").strip().lower() == "true",
        "PCB_EGRESS_SCOPE": env.get("PCB_EGRESS_SCOPE", "public_only").strip() or "public_only",
        "PCB_LLM_PROVIDER": env.get("PCB_LLM_PROVIDER", "openai_chat_completions").strip() or "openai_chat_completions",
        "PCB_LLM_BASE_URL": env.get("PCB_LLM_BASE_URL", "").strip(),
        "PCB_LLM_MODEL": env.get("PCB_LLM_MODEL", "").strip(),
        "PCB_LLM_API_KEY_ENV": key_env,
        "PCB_ALLOW_LOCALHOST_HTTP": env.get("PCB_ALLOW_LOCALHOST_HTTP", "false").strip().lower() == "true",
        "has_api_key": bool(env.get(key_env, "").strip()),
    }


def save_profile_settings(profile_path: Path, payload: dict) -> dict:
    """Save or update a named profile, switch active profile, or delete a profile."""
    if not isinstance(payload, dict):
        raise CatalogError("invalid_agent_profile")

    action = str(payload.get("action") or "save").strip()
    store, _ = load_profile_store(profile_path)

    if action == "activate":
        target_name = validate_profile_name(str(payload.get("profile_name") or ""))
        if target_name not in store["profiles"]:
            raise CatalogError("profile_not_found")
        store["active_profile"] = target_name
        _write_profile_store(profile_path, store)
        return safe_profile_view(profile_path)

    if action == "delete":
        target_name = validate_profile_name(str(payload.get("profile_name") or ""))
        if target_name in store["profiles"]:
            del store["profiles"][target_name]
        if store["active_profile"] == target_name:
            store["active_profile"] = next(iter(store["profiles"]), DEFAULT_PROFILE_NAME)
        _write_profile_store(profile_path, store)
        return safe_profile_view(profile_path)

    raw_name = payload.get("profile_name")
    target_name = (
        validate_profile_name(str(raw_name))
        if raw_name is not None and str(raw_name).strip()
        else (store["active_profile"] or DEFAULT_PROFILE_NAME)
    )
    existing = dict(store["profiles"].get(target_name) or {})

    if "label" in payload and isinstance(payload["label"], str):
        clean_label = payload["label"].strip()[:80]
        if clean_label:
            existing["label"] = clean_label

    for key in ALLOWED_PROFILE_ENV_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if key == "PCB_EGRESS_SCOPE" and str(value).strip() not in ("public_only", "sanitized_catalog"):
            raise CatalogError("invalid_egress_scope")
        if key == "PCB_LLM_PROVIDER" and str(value).strip() not in ("openai_chat_completions", "openai_responses"):
            raise CatalogError("invalid_provider")
        if key == "PCB_LLM_API_KEY_ENV" and not KEY_NAME_RE.fullmatch(str(value).strip()):
            raise CatalogError("invalid_api_key_env")
        if isinstance(value, bool):
            existing[key] = value
        else:
            existing[key] = str(value).strip()

    if payload.get("clear_api_key") is True:
        existing.pop("api_key", None)
    elif isinstance(payload.get("api_key"), str) and payload["api_key"].strip():
        existing["api_key"] = payload["api_key"].strip()

    store["profiles"][target_name] = existing
    if payload.get("set_active", True):
        store["active_profile"] = target_name

    _write_profile_store(profile_path, store)
    return safe_profile_view(profile_path, profile_name=store["active_profile"])


def probe_llm_connection(
    profile_path: Path | None = None,
    *,
    overrides: dict | None = None,
    opener=None,
    timeout_seconds: int = 12,
) -> dict:
    """Send a minimal zero-data connectivity probe to verify Base URL, Model, and API Key."""
    target = Path(profile_path) if profile_path is not None else DEFAULT_PROFILE_PATH
    selected_name = None
    if isinstance(overrides, dict) and overrides.get("profile_name"):
        selected_name = str(overrides["profile_name"]).strip()
    env, _ = load_runtime_env(
        target,
        profile_name=selected_name,
        include_default_dotenv=(target == DEFAULT_PROFILE_PATH),
    )
    merged = dict(env)
    if isinstance(overrides, dict):
        for key in ALLOWED_PROFILE_ENV_KEYS:
            if key in overrides and overrides[key] is not None:
                val = overrides[key]
                merged[key] = ("true" if val else "false") if isinstance(val, bool) else str(val).strip()
        key_env = merged.get("PCB_LLM_API_KEY_ENV", "DEEPSEEK_API_KEY").strip() or "DEEPSEEK_API_KEY"
        if isinstance(overrides.get("api_key"), str) and overrides["api_key"].strip():
            merged[key_env] = overrides["api_key"].strip()

    provider = merged.get("PCB_LLM_PROVIDER", "openai_chat_completions").strip()
    model = merged.get("PCB_LLM_MODEL", "").strip()
    base_url = merged.get("PCB_LLM_BASE_URL", "").strip().rstrip("/")
    key_env = merged.get("PCB_LLM_API_KEY_ENV", "DEEPSEEK_API_KEY").strip() or "DEEPSEEK_API_KEY"
    api_key = merged.get(key_env, "").strip()
    allow_local_http = merged.get("PCB_ALLOW_LOCALHOST_HTTP", "false").strip().lower() == "true"

    if provider not in ("openai_chat_completions", "openai_responses"):
        return {"ok": False, "error_code": "invalid_provider", "message": "不支持的接口协议类型。"}
    if not base_url:
        return {"ok": False, "error_code": "missing_base_url", "message": "请填写 Base URL（例如 https://api.deepseek.com/v1）。"}
    if not model:
        return {"ok": False, "error_code": "missing_model", "message": "请填写模型名称（例如 deepseek-chat）。"}

    parsed = urlsplit(base_url)
    is_local_http = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
    if parsed.scheme != "https" and not (allow_local_http and is_local_http):
        hint = (
            "本机 HTTP 服务请勾选允许本机 HTTP（PCB_ALLOW_LOCALHOST_HTTP）。"
            if is_local_http
            else "远程接口必须使用 https:// 开头。"
        )
        return {"ok": False, "error_code": "invalid_base_url", "message": f"Base URL 协议不符合安全要求：{hint}"}

    if not api_key and not is_local_http:
        return {
            "ok": False,
            "error_code": "missing_api_key",
            "message": f"未检测到 API Key（请在输入框填入密钥或设置环境变量 {key_env}）。",
        }
    effective_key = api_key or "ollama"

    if provider == "openai_responses":
        endpoint = base_url if base_url.endswith("/responses") else f"{base_url}/responses"
        body = {
            "model": model,
            "input": "ping",
            "max_output_tokens": 16,
        }
    else:
        endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "temperature": 0,
        }

    req = Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {effective_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "pcb-assistant/6.3",
        },
        method="POST",
    )
    active_opener = urlopen if opener is None else opener
    started = time.monotonic()
    try:
        with active_opener(req, timeout=max(2, min(int(timeout_seconds), 30))) as resp:
            raw = resp.read(200_000)
        latency_ms = int((time.monotonic() - started) * 1000)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return {
                "ok": False,
                "error_code": "invalid_response",
                "latency_ms": latency_ms,
                "message": "接口返回的不是合法 JSON 对象，请确认 Base URL 是否指向 OpenAI 兼容网关。",
            }
        return {
            "ok": True,
            "error_code": None,
            "provider": provider,
            "model": data.get("model") or model,
            "host": parsed.hostname,
            "latency_ms": latency_ms,
            "message": f"连接成功 · 模型 {data.get('model') or model} 响应正常（{latency_ms} ms）",
        }
    except HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        if exc.code in (401, 403):
            msg = f"HTTP {exc.code}：鉴权失败，请检查 API Key 是否有效、模型是否有权限访问。"
            code = "auth_failed"
        elif exc.code == 404:
            msg = f"HTTP 404：未找到接口路径，请检查 Base URL 是否漏掉了 /v1 后缀，或模型名 '{model}' 是否存在。"
            code = "endpoint_or_model_not_found"
        elif exc.code == 429:
            msg = "HTTP 429：请求触发限流或账户余额不足。"
            code = "rate_limited"
        else:
            msg = f"HTTP {exc.code}：上游网关返回异常状态码。"
            code = f"http_{exc.code}"
        return {"ok": False, "error_code": code, "latency_ms": latency_ms, "message": msg}
    except (TimeoutError, socket.timeout):
        return {"ok": False, "error_code": "timeout", "message": "连接超时，请检查网络、代理或网关地址。"}
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        return {"ok": False, "error_code": "network_error", "message": f"网络无法连通目标地址（{type(reason).__name__}）。"}
    except (UnicodeError, ValueError):
        return {
            "ok": False,
            "error_code": "non_json_response",
            "message": "网关返回了非 JSON 内容（如 HTML 登录页），请检查 Base URL 是否少了 /v1。",
        }
    except OSError:
        return {"ok": False, "error_code": "os_error", "message": "底层套接字连接失败。"}


def run_doctor(
    *,
    catalog_directory: Path = DEFAULT_CATALOG,
    profile_path: Path = DEFAULT_PROFILE_PATH,
    dotenv_path: Path = DEFAULT_DOTENV_PATH,
    probe_llm: bool = False,
    opener=None,
) -> dict:
    """Inspect runtime environment, read-only Catalog, privacy assets, and LLM profile."""
    checks: list[dict] = []
    recommendations: list[str] = []

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 11)
    venv_py = Path(python_executable(".venv"))
    checks.append({
        "name": "python_runtime",
        "status": "ok" if py_ok else "failed",
        "summary": f"Python {py_ver} ({'UTF-8' if sys.flags.utf8_mode else 'default encoding'})",
        "details": {
            "version": py_ver,
            "utf8_mode": bool(sys.flags.utf8_mode),
            "venv_present": venv_py.is_file(),
        },
    })
    if not py_ok:
        recommendations.append("请升级到 Python 3.11 或更高版本。")

    cat_path = Path(catalog_directory).expanduser().resolve()
    if cat_path.is_dir() and (cat_path / "catalog.sqlite3").is_file():
        try:
            import pcb_assistant.interfaces.mcp as phase6_mcp
            import pcb_assistant.catalog.cli as catalog_cli

            phase6_mcp._check_catalog(cat_path)
            st = catalog_cli.status(cat_path)
            ver = phase6_mcp.catalog_version(cat_path)
            docs = st.get("totals", {}).get("documents", 0)
            rels = st.get("totals", {}).get("relationships", 0)
            checks.append({
                "name": "catalog_database",
                "status": "ok",
                "summary": f"Catalog 就绪 · 版本 {ver} · {docs} 篇文档 / {rels} 条关系",
                "details": {"catalog_version": ver, "documents": docs, "relationships": rels},
            })
        except Exception as exc:
            checks.append({
                "name": "catalog_database",
                "status": "failed",
                "summary": f"Catalog 校验失败：{exc}",
                "details": {},
            })
            recommendations.append("请运行 python -X utf8 -B knowledge_catalog.py validate 排查知识库目录。")
    else:
        checks.append({
            "name": "catalog_database",
            "status": "warn",
            "summary": "未检测到默认 Catalog 目录（首次使用需指定 --catalog 或构建知识库）",
            "details": {"exists": False},
        })
        recommendations.append("请使用 --catalog 指定已有知识库目录，或参考 docs/DATA_PIPELINE.md 构建。")

    opt_mods = {
        "jieba": importlib.util.find_spec("jieba") is not None,
        "spacy": importlib.util.find_spec("spacy") is not None,
        "pydantic_ai": importlib.util.find_spec("pydantic_ai") is not None,
        "pypdf": importlib.util.find_spec("pypdf") is not None,
        "rapidocr_onnxruntime": importlib.util.find_spec("rapidocr_onnxruntime") is not None,
    }
    checks.append({
        "name": "optional_packages",
        "status": "ok" if opt_mods["jieba"] else "warn",
        "summary": "核心中文分词 jieba="
        + ("已安装" if opt_mods["jieba"] else "未安装（将自动回退标准库分词）")
        + f" · spacy={'已安装' if opt_mods['spacy'] else '未安装'}",
        "details": opt_mods,
    })
    if not opt_mods["jieba"]:
        recommendations.append("建议安装 requirements/requirements-core.txt（jieba）以提升中文检索精度。")

    pseudo_key = (ROOT / ".private" / "pseudonym.key").is_file()
    sens_terms = (ROOT / ".private" / "sensitive_terms.txt").is_file()
    sanitized_ready = opt_mods["spacy"] and pseudo_key
    checks.append({
        "name": "privacy_scrubber",
        "status": "ok" if sanitized_ready else "warn",
        "summary": (
            "二次脱敏出境组件就绪（支持 sanitized_catalog 与 public_only）"
            if sanitized_ready
            else "未装齐本机 spaCy/脱敏密钥（外部 API 仍可使用 public_only 范围或纯本地模式）"
        ),
        "details": {
            "pseudonym_key_present": pseudo_key,
            "sensitive_terms_present": sens_terms,
            "spacy_installed": opt_mods["spacy"],
        },
    })

    view = safe_profile_view(profile_path, dotenv_path=dotenv_path)
    llm_ready = bool(view["PCB_ALLOW_CLOUD_EGRESS"] and view["has_api_key"] and view["PCB_LLM_MODEL"])
    checks.append({
        "name": "llm_profile",
        "status": "ok" if llm_ready else "warn",
        "summary": (
            f"渠道 [{view['active_profile']}] 就绪 · {view['PCB_LLM_MODEL']} ({view['PCB_EGRESS_SCOPE']})"
            if llm_ready
            else f"渠道 [{view['active_profile']}] 未启用外部模型（默认运行纯本地离线证据回答）"
        ),
        "details": {
            "active_profile": view["active_profile"],
            "profile_count": len(view["profiles"]),
            "dotenv_present": Path(dotenv_path).is_file(),
            "allow_cloud_egress": view["PCB_ALLOW_CLOUD_EGRESS"],
            "egress_scope": view["PCB_EGRESS_SCOPE"],
            "provider": view["PCB_LLM_PROVIDER"],
            "model": view["PCB_LLM_MODEL"],
            "has_api_key": view["has_api_key"],
        },
    })

    if probe_llm and llm_ready:
        probe = probe_llm_connection(profile_path, opener=opener)
        checks.append({
            "name": "llm_connectivity",
            "status": "ok" if probe["ok"] else "failed",
            "summary": probe["message"],
            "details": {k: v for k, v in probe.items() if k != "message"},
        })
        if not probe["ok"]:
            recommendations.append(probe["message"])

    overall = "ok"
    if any(c["status"] == "failed" for c in checks):
        overall = "failed"
    elif any(c["status"] == "warn" for c in checks):
        overall = "warn"

    return {
        "schema": "pcb-doctor-v1",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": overall,
        "checks": checks,
        "recommendations": recommendations,
    }


def format_doctor_text(report: dict) -> str:
    icon = {"ok": "[OK]", "warn": "[提示]", "failed": "[失败]"}
    lines = [f"PCB 压合助手环境自检（状态: {report.get('status', 'unknown')}）", "-" * 56]
    for item in report.get("checks", ()):
        tag = icon.get(item.get("status"), "[-]")
        lines.append(f"{tag} {item.get('name')}: {item.get('summary')}")
    recs = report.get("recommendations") or []
    if recs:
        lines.extend(["", "优化建议："])
        for rec in recs:
            lines.append(f"  - {rec}")
    return "\n".join(lines)
