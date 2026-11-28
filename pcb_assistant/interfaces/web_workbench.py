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
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import urlopen
import webbrowser

from pcb_assistant.catalog.common import CatalogError, DEFAULT_CATALOG, json_text
import pcb_assistant.config as runtime_config
import pcb_assistant.interfaces.mcp as phase6_mcp
import pcb_assistant.interfaces.workbench as workbench
from pcb_assistant.interfaces.workbench_ui import WORKBENCH_HTML


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_REQUEST_BODY_BYTES = 1_000_000
_CASE_PATH_RE = re.compile(r"^/api/cases/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?:/(notes|clear|report))?$")


def _safe_profile_view(
    profile_path: Path,
    base_env: dict[str, str] | None = None,
    *,
    profile_name: str | None = None,
) -> dict:
    """Return non-secret multi-profile settings plus presets and has_api_key."""
    return runtime_config.safe_profile_view(
        profile_path,
        profile_name=profile_name,
        base_env=base_env,
    )


def _save_profile(profile_path: Path, payload: dict) -> dict:
    return runtime_config.save_profile_settings(profile_path, payload)


def _probe_existing_workbench(host: str, port: int) -> bool:
    """Return True if an existing PCB workbench server responds healthily on host:port."""
    check_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{check_host}:{port}/api/health"
    try:
        with urlopen(url, timeout=0.8) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read(8192).decode("utf-8"))
            return isinstance(data, dict) and data.get("status") == "ok" and "toolset" in data
    except (OSError, URLError, ValueError):
        return False


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
            parsed_url = urlsplit(self.path)
            path = unquote(parsed_url.path)
            query_params = parse_qs(parsed_url.query)
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
                        "active_profile": profile.get("active_profile", "default"),
                        "profile_configured": bool(
                            profile["PCB_ALLOW_CLOUD_EGRESS"]
                            and profile["has_api_key"]
                            and profile["PCB_LLM_MODEL"]
                        ),
                    })
                    return
                if path == "/api/profile":
                    req_name = (query_params.get("name") or [None])[0]
                    self._send_json(
                        HTTPStatus.OK,
                        _safe_profile_view(state.profile_path, profile_name=req_name),
                    )
                    return
                if path == "/api/doctor":
                    report = runtime_config.run_doctor(
                        catalog_directory=state.catalog_directory,
                        profile_path=state.profile_path,
                        probe_llm=(query_params.get("probe") or [""])[0] in ("1", "true"),
                    )
                    self._send_json(HTTPStatus.OK, report)
                    return
                if path == "/api/mcp-config":
                    host_hdr = self.headers.get("Host") or f"{DEFAULT_HOST}:{DEFAULT_PORT}"
                    origin = f"http://{host_hdr}"
                    self._send_json(
                        HTTPStatus.OK,
                        workbench.mcp_client_config(
                            state.catalog_directory,
                            toolset=state.toolset,
                            http_origin=origin,
                        ),
                    )
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
                if path == "/api/profile/test":
                    probe = runtime_config.probe_llm_connection(
                        state.profile_path,
                        overrides=body,
                    )
                    self._send_json(HTTPStatus.OK, probe)
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
    parser.add_argument("--catalog", type=Path, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--cases-dir", type=Path, default=workbench.DEFAULT_CASES_DIR)
    parser.add_argument("--agent-config", type=Path, default=workbench.DEFAULT_PROFILE_PATH)
    parser.add_argument("--profile", default=None, help="启动时切换激活的命名模型渠道")
    parser.add_argument("--toolset", choices=("single", "agent"), default="agent")
    parser.add_argument("--api-token", default=None, help="非本机地址监听时必须提供 Bearer Token")
    parser.add_argument("--open", action="store_true", dest="open_browser", help="启动后自动打开浏览器")
    parser.add_argument("--reuse-running", action="store_true", help="若同端口已有本工作台运行则直接打开并退出")
    parser.add_argument("--auto-port", action="store_true", help="若首选端口被其他程序占用则自动顺延空闲端口")
    args = parser.parse_args(argv)

    env, _ = runtime_config.load_runtime_env(
        args.agent_config,
        profile_name=args.profile,
        include_default_dotenv=(args.agent_config == workbench.DEFAULT_PROFILE_PATH),
    )
    catalog_dir = args.catalog
    if catalog_dir is None:
        env_cat = env.get("PCB_CATALOG", "").strip()
        catalog_dir = Path(env_cat) if env_cat else DEFAULT_CATALOG
    host = (args.host or env.get("PCB_WEB_HOST") or DEFAULT_HOST).strip()
    if args.port is not None:
        preferred_port = int(args.port)
    else:
        try:
            preferred_port = int(env.get("PCB_WEB_PORT") or DEFAULT_PORT)
        except ValueError:
            preferred_port = DEFAULT_PORT

    if args.profile:
        try:
            runtime_config.save_profile_settings(
                args.agent_config,
                {"action": "activate", "profile_name": args.profile},
            )
        except CatalogError:
            pass

    if args.reuse_running and preferred_port > 0 and _probe_existing_workbench(host, preferred_port):
        open_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
        url = f"http://{open_host}:{preferred_port}/"
        print(f"检测到 PCB 压合排查工作台已在运行：{url}")
        if args.open_browser:
            webbrowser.open(url)
        return 0

    try:
        phase6_mcp._check_catalog(catalog_dir)
        candidates = (
            [preferred_port, *range(preferred_port + 1, preferred_port + 11)]
            if (args.auto_port and preferred_port > 0)
            else [preferred_port]
        )
        server = None
        last_exc: Exception | None = None
        for candidate_port in candidates:
            try:
                server = create_server(
                    host=host,
                    port=candidate_port,
                    catalog_directory=catalog_dir,
                    cases_dir=args.cases_dir,
                    profile_path=args.agent_config,
                    toolset=args.toolset,
                    api_token=args.api_token,
                )
                break
            except OSError as exc:
                last_exc = exc
        if server is None:
            raise last_exc or RuntimeError("port_bind_failed")
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    actual_host, actual_port = server.server_address[:2]
    open_host = "127.0.0.1" if actual_host in ("0.0.0.0", "::", "") else actual_host
    url = f"http://{open_host}:{actual_port}/"
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
