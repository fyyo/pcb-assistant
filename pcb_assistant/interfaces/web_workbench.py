"""Zero-dependency local HTTP Web workbench + JSON REST + MCP HTTP server.

Binds strictly to 127.0.0.1 by default. Non-loopback bindings require an
explicit Bearer token (--api-token or PCB_WEB_API_TOKEN). All case data and
optional API profiles remain strictly under ``.private/``; the Catalog is
opened read-only.
"""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import unquote, urlsplit
import webbrowser

from pcb_assistant.catalog.common import CatalogError, DEFAULT_CATALOG, json_text
import pcb_assistant.interfaces.mcp as phase6_mcp
import pcb_assistant.interfaces.workbench as workbench
from pcb_assistant.interfaces.workbench_ui import WORKBENCH_HTML


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_REQUEST_BODY_BYTES = 1_000_000
_CASE_PATH_RE = re.compile(r"^/api/cases/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?:/(notes|clear|report))?$")
_KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _safe_profile_view(profile_path: Path, base_env: dict[str, str] | None = None) -> dict:
    """Return non-secret profile settings plus a boolean has_api_key flag."""
    env, loaded = workbench.load_local_agent_env(profile_path, base_env=base_env)
    key_env = env.get("PCB_LLM_API_KEY_ENV", "DEEPSEEK_API_KEY").strip() or "DEEPSEEK_API_KEY"
    return {
        "profile_loaded": loaded,
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


def _save_profile(profile_path: Path, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise CatalogError("invalid_agent_profile")
    existing = {}
    if profile_path.is_file():
        try:
            raw = json.loads(profile_path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                existing = raw
        except (OSError, ValueError):
            existing = {}

    updated = dict(existing)
    for key in workbench.ALLOWED_PROFILE_ENV_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if key == "PCB_EGRESS_SCOPE" and str(value).strip() not in ("public_only", "sanitized_catalog"):
            raise CatalogError("invalid_egress_scope")
        if key == "PCB_LLM_PROVIDER" and str(value).strip() not in ("openai_chat_completions", "openai_responses"):
            raise CatalogError("invalid_provider")
        if key == "PCB_LLM_API_KEY_ENV" and not _KEY_NAME_RE.fullmatch(str(value).strip()):
            raise CatalogError("invalid_api_key_env")
        if isinstance(value, bool):
            updated[key] = value
        else:
            updated[key] = str(value).strip()

    if "api_key" in payload and isinstance(payload["api_key"], str) and payload["api_key"].strip():
        updated["api_key"] = payload["api_key"].strip()

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = profile_path.with_suffix(".json.tmp")
    tmp.write_text(json_text(updated, pretty=True) + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(profile_path)
    return _safe_profile_view(profile_path)


def _render_case_markdown_text(case_data: dict) -> str:
    lines = [
        f"# PCB 压合异常排查草稿：{case_data.get('case_id', '')}",
        "",
        f"- 标题：{case_data.get('title', '')}",
        f"- 创建时间：{case_data.get('created_at', '')}",
        f"- 更新时间：{case_data.get('updated_at', '')}",
        f"- Catalog 版本：{case_data.get('catalog_version') or 'unknown'}",
        "",
        "> 边界说明：本草稿由本地检索与受控排查辅助生成，引用的邮件、附件和 OCR 数值均未独立核验，**不可直接作为放行或生产工艺参数**，需结合现场曲线、切片与工程师确认。",
        "",
    ]
    notes = case_data.get("notes") or []
    if notes:
        lines.append("## 现场补充记录")
        for note in notes:
            lines.append(f"- [{note.get('timestamp', '')}] {note.get('text', '')}")
        lines.append("")
    turns = case_data.get("turns") or []
    lines.append("## 排查回合记录")
    for idx, turn in enumerate(turns, start=1):
        lines.extend([
            f"### 第 {idx} 轮（{turn.get('route') or 'unknown'} / {turn.get('answer_mode') or 'unknown'}）",
            f"**问题**：{turn.get('question', '')}",
            "",
            turn.get("answer", ""),
            "",
        ])
        cits = turn.get("citations") or []
        if cits:
            lines.append("**引用清单**：")
            for cit in cits:
                lines.append(
                    f"- `{cit.get('excerpt_id')}` · {cit.get('source_type') or ''} · {cit.get('title') or ''}"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class WorkbenchServerState:
    def __init__(
        self,
        *,
        catalog_directory: Path = DEFAULT_CATALOG,
        cases_dir: Path = workbench.DEFAULT_CASES_DIR,
        profile_path: Path = workbench.DEFAULT_PROFILE_PATH,
        toolset: str = "agent",
        api_token: str | None = None,
    ):
        self.catalog_directory = Path(catalog_directory).expanduser().resolve()
        self.cases_dir = Path(cases_dir).expanduser().resolve()
        self.profile_path = Path(profile_path).expanduser().resolve()
        self.toolset = "agent" if toolset == "agent" else "single"
        self.api_token = (api_token or "").strip() or None
        self.workspace = workbench.CaseWorkspace(
            self.cases_dir, catalog_directory=self.catalog_directory)
        self.env_lock = threading.Lock()


def create_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    catalog_directory: Path = DEFAULT_CATALOG,
    cases_dir: Path = workbench.DEFAULT_CASES_DIR,
    profile_path: Path = workbench.DEFAULT_PROFILE_PATH,
    toolset: str = "agent",
    api_token: str | None = None,
) -> ThreadingHTTPServer:
    clean_host = (host or DEFAULT_HOST).strip()
    token = (api_token if api_token is not None else os.environ.get("PCB_WEB_API_TOKEN", "")).strip() or None
    if clean_host not in LOOPBACK_HOSTS and not token:
        raise CatalogError("non_loopback_requires_api_token")

    state = WorkbenchServerState(
        catalog_directory=catalog_directory,
        cases_dir=cases_dir,
        profile_path=profile_path,
        toolset=toolset,
        api_token=token,
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "pcb-workbench/1.0"

        def log_message(self, format, *args):  # noqa: A002
            return

        def _send_bytes(self, status: int, content_type: str, body: bytes, extra_headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", body)

        def _authorized(self) -> bool:
            if not state.api_token:
                return True
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {state.api_token}"

        def _read_json(self) -> dict:
            raw_len = self.headers.get("Content-Length")
            try:
                length = int(raw_len) if raw_len is not None else 0
            except ValueError as exc:
                raise CatalogError("invalid_content_length") from exc
            if length < 0 or length > MAX_REQUEST_BODY_BYTES:
                raise CatalogError("request_body_too_large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise CatalogError("invalid_json_body") from exc
            if not isinstance(parsed, dict):
                raise CatalogError("json_body_must_be_object")
            return parsed

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path in ("/", "/index.html"):
                self._send_bytes(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    WORKBENCH_HTML.encode("utf-8"),
                )
                return
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                if path == "/api/health":
                    profile = _safe_profile_view(state.profile_path)
                    self._send_json(HTTPStatus.OK, {
                        "status": "ok",
                        "catalog_version": phase6_mcp.catalog_version(state.catalog_directory),
                        "toolset": state.toolset,
                        "profile_configured": bool(
                            profile["PCB_ALLOW_CLOUD_EGRESS"]
                            and profile["has_api_key"]
                            and profile["PCB_LLM_MODEL"]
                        ),
                    })
                    return
                if path == "/api/profile":
                    self._send_json(HTTPStatus.OK, _safe_profile_view(state.profile_path))
                    return
                if path == "/api/cases":
                    self._send_json(HTTPStatus.OK, {"cases": state.workspace.list_cases()})
                    return
                if path == "/mcp":
                    self._send_json(HTTPStatus.OK, {
                        "server": phase6_mcp.SERVER_NAME,
                        "version": phase6_mcp.SERVER_VERSION,
                        "transport": "streamable-http",
                        "toolset": state.toolset,
                    })
                    return
                match = _CASE_PATH_RE.fullmatch(path)
                if match:
                    case_id, action = match.group(1), match.group(2)
                    case_data = state.workspace.open_or_create(case_id)
                    if action == "report":
                        md = _render_case_markdown_text(case_data).encode("utf-8")
                        self._send_bytes(
                            HTTPStatus.OK,
                            "text/markdown; charset=utf-8",
                            md,
                            extra_headers={
                                "Content-Disposition": f'attachment; filename="{case_id}_report.md"',
                            },
                        )
                        return
                    if action is None:
                        self._send_json(HTTPStatus.OK, case_data)
                        return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except CatalogError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

        def do_POST(self):
            path = unquote(urlsplit(self.path).path)
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                body = self._read_json()
                if path == "/api/profile":
                    saved = _save_profile(state.profile_path, body)
                    self._send_json(HTTPStatus.OK, saved)
                    return
                if path == "/api/cases":
                    case_id = body.get("case_id", "")
                    title = str(body.get("title") or "")
                    data = state.workspace.open_or_create(case_id, title=title)
                    self._send_json(HTTPStatus.OK, data)
                    return
                if path == "/api/answer":
                    question = str(body.get("question") or "").strip()
                    if not question:
                        raise CatalogError("question_required")
                    mode = body.get("mode", "agent")
                    if mode not in ("agent", "local"):
                        raise CatalogError("invalid_mode")
                    case_id = body.get("case_id")
                    active_case = None
                    if case_id:
                        active_case = state.workspace.open_or_create(str(case_id))
                        history = active_case.get("history") or []
                    else:
                        raw_hist = body.get("history") or []
                        history = raw_hist if isinstance(raw_hist, list) else []
                    filters = {
                        k: body[k]
                        for k in ("doc_type", "source", "tags", "version", "content_status")
                        if body.get(k)
                    }
                    started = time.monotonic()
                    with state.env_lock:
                        env, _ = workbench.load_local_agent_env(state.profile_path)
                        outcome = workbench._ask_once(
                            question,
                            history,
                            mode=mode,
                            catalog_directory=state.catalog_directory,
                            env=env,
                            filters=filters,
                        )
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    if active_case is not None:
                        state.workspace.append_turn(active_case, question, outcome)
                    self._send_json(HTTPStatus.OK, {
                        "status": outcome.status,
                        "external_services": outcome.external_services,
                        "data_egress": outcome.data_egress,
                        "warnings": list(outcome.warnings),
                        "errors": list(outcome.errors),
                        "elapsed_ms": elapsed_ms,
                        "result": phase6_mcp.sanitize_payload(outcome.result),
                    })
                    return
                if path == "/mcp":
                    with state.env_lock:
                        env, _ = workbench.load_local_agent_env(state.profile_path)
                        saved_env = {k: os.environ.get(k) for k in env}
                        try:
                            for k, v in env.items():
                                os.environ[k] = v
                            with phase6_mcp._catalog_context(state.catalog_directory), \
                                    phase6_mcp._toolset_context(state.toolset):
                                try:
                                    response = phase6_mcp.dispatch(body)
                                except phase6_mcp.RpcError as exc:
                                    response = phase6_mcp._rpc_error(body.get("id"), exc)
                        finally:
                            for k, old in saved_env.items():
                                if old is None:
                                    os.environ.pop(k, None)
                                else:
                                    os.environ[k] = old
                    if response is None:
                        self._send_bytes(HTTPStatus.ACCEPTED, "application/json; charset=utf-8", b"{}")
                    else:
                        self._send_json(HTTPStatus.OK, response)
                    return

                match = _CASE_PATH_RE.fullmatch(path)
                if match:
                    case_id, action = match.group(1), match.group(2)
                    case_data = state.workspace.open_or_create(case_id)
                    if action == "notes":
                        entry = state.workspace.append_note(case_data, body.get("text", ""))
                        self._send_json(HTTPStatus.OK, entry)
                        return
                    if action == "clear":
                        case_data["history"] = []
                        state.workspace.save(case_data)
                        self._send_json(HTTPStatus.OK, {"status": "cleared", "case_id": case_id})
                        return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except CatalogError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

    server = ThreadingHTTPServer((clean_host, int(port)), Handler)
    server.workbench_state = state  # type: ignore[attr-defined]
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="PCB 压合本机 Web 工作台 + JSON API + MCP Streamable HTTP 服务",
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--cases-dir", type=Path, default=workbench.DEFAULT_CASES_DIR)
    parser.add_argument("--agent-config", type=Path, default=workbench.DEFAULT_PROFILE_PATH)
    parser.add_argument("--toolset", choices=("single", "agent"), default="agent")
    parser.add_argument("--api-token", default=None, help="非本机地址监听时必须提供 Bearer Token")
    parser.add_argument("--open", action="store_true", dest="open_browser", help="启动后自动打开浏览器")
    args = parser.parse_args(argv)

    try:
        phase6_mcp._check_catalog(args.catalog)
        server = create_server(
            host=args.host,
            port=args.port,
            catalog_directory=args.catalog,
            cases_dir=args.cases_dir,
            profile_path=args.agent_config,
            toolset=args.toolset,
            api_token=args.api_token,
        )
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/"
    print(f"PCB 压合排查工作台已启动：{url}")
    print(f"MCP Streamable HTTP 端点：{url}mcp (toolset={args.toolset})")
    print("按 Ctrl+C 停止服务。")
    if args.open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
