"""Interactive multi-turn PCB Agent workbench and persistent local Case workspace.

All case state and optional local API profiles are stored strictly under
``.private/`` (never inside the read-only Catalog and never committed to Git).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

from pcb_assistant.catalog.common import CatalogError, DEFAULT_CATALOG, json_text
import pcb_assistant.config as runtime_config
import pcb_assistant.interfaces.mcp as phase6_mcp
from pcb_assistant.paths import ROOT, python_executable
import pcb_assistant.router as router


DEFAULT_PROFILE_PATH = runtime_config.DEFAULT_PROFILE_PATH
DEFAULT_CASES_DIR = ROOT / ".private" / "cases"
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
CASE_SCHEMA = "pcb-case-workspace-v1"
ALLOWED_PROFILE_ENV_KEYS = runtime_config.ALLOWED_PROFILE_ENV_KEYS
_KEY_NAME_RE = runtime_config.KEY_NAME_RE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_case_id(case_id: str) -> str:
    if not isinstance(case_id, str) or not CASE_ID_RE.fullmatch(case_id.strip()):
        raise CatalogError("invalid_case_id")
    return case_id.strip()


def load_local_agent_env(
    profile_path: Path | None = None,
    *,
    profile_name: str | None = None,
    dotenv_path: Path | None = None,
    base_env: dict[str, str] | None = None,
) -> tuple[dict[str, str], bool]:
    """Load optional local agent profile JSON + .env into an environment mapping.

    Explicit environment variables in ``base_env`` always take precedence over
    values from ``.env`` and the profile file. Never logs or prints secret values.
    """
    target = Path(profile_path) if profile_path is not None else DEFAULT_PROFILE_PATH
    use_default_dotenv = (
        base_env is None and dotenv_path is None and target == DEFAULT_PROFILE_PATH
    )
    return runtime_config.load_runtime_env(
        target,
        profile_name=profile_name,
        dotenv_path=dotenv_path,
        base_env=base_env,
        include_default_dotenv=use_default_dotenv,
    )


class CaseWorkspace:
    """Local persistent case store under .private/cases/ (never writes to Catalog)."""

    def __init__(self, cases_dir: Path = DEFAULT_CASES_DIR, *, catalog_directory: Path = DEFAULT_CATALOG):
        self.cases_dir = Path(cases_dir).expanduser().resolve()
        self.catalog_directory = Path(catalog_directory).expanduser().resolve()
        if (
            self.cases_dir == self.catalog_directory
            or self.cases_dir.is_relative_to(self.catalog_directory)
        ):
            raise CatalogError("case_workspace_inside_catalog_forbidden")

    def _path(self, case_id: str) -> Path:
        clean_id = validate_case_id(case_id)
        return self.cases_dir / f"{clean_id}.json"

    def list_cases(self) -> list[dict]:
        if not self.cases_dir.is_dir():
            return []
        items = []
        for path in sorted(self.cases_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("schema") != CASE_SCHEMA:
                continue
            items.append({
                "case_id": data.get("case_id", path.stem),
                "title": data.get("title", ""),
                "turns": len(data.get("turns") or []),
                "notes": len(data.get("notes") or []),
                "updated_at": data.get("updated_at", ""),
            })
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return items

    def open_or_create(self, case_id: str, *, title: str = "") -> dict:
        clean_id = validate_case_id(case_id)
        path = self._path(clean_id)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise CatalogError("invalid_case_workspace_file") from exc
            if not isinstance(data, dict) or data.get("schema") != CASE_SCHEMA:
                raise CatalogError("invalid_case_workspace_file")
            return data
        now = _utc_now()
        data = {
            "schema": CASE_SCHEMA,
            "case_id": clean_id,
            "title": title.strip() or clean_id,
            "created_at": now,
            "updated_at": now,
            "catalog_version": phase6_mcp.catalog_version(self.catalog_directory),
            "history": [],
            "turns": [],
            "notes": [],
        }
        self.save(data)
        return data

    def save(self, case_data: dict) -> Path:
        clean_id = validate_case_id(case_data.get("case_id", ""))
        self.cases_dir.mkdir(parents=True, exist_ok=True)
        case_data["updated_at"] = _utc_now()
        target = self._path(clean_id)
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json_text(case_data, pretty=True) + "\n", encoding="utf-8")
        temp.replace(target)
        return target

    def append_turn(self, case_data: dict, question: str, outcome) -> dict:
        result = outcome.result
        answer_text = str(result.get("answer") or "").strip()
        case_data.setdefault("history", []).append({"role": "user", "content": question})
        case_data["history"].append({"role": "assistant", "content": answer_text[:2800]})
        if case_data.get("title") == case_data.get("case_id") and question:
            case_data["title"] = question[:60]
        citations = [
            {
                "excerpt_id": item.get("excerpt_id") or item.get("citation_id"),
                "title": item.get("title", ""),
                "source_type": item.get("source_type", ""),
            }
            for item in (result.get("citations") or [])
            if isinstance(item, dict) and (item.get("excerpt_id") or item.get("citation_id"))
        ]
        turn = {
            "timestamp": _utc_now(),
            "question": question,
            "status": outcome.status,
            "route": (result.get("workflow") or {}).get("route"),
            "answer_mode": result.get("answer_mode"),
            "data_egress": outcome.data_egress,
            "answer": answer_text,
            "citations": citations,
            "missing_information": list(result.get("missing_information") or []),
            "agent_trace": list(result.get("agent_trace") or []),
            "result_fingerprint": result.get("result_fingerprint"),
        }
        case_data.setdefault("turns", []).append(turn)
        self.save(case_data)
        return turn

    def append_note(self, case_data: dict, text: str) -> dict:
        clean = str(text or "").strip()
        if not clean:
            raise CatalogError("note_text_required")
        entry = {"timestamp": _utc_now(), "text": clean}
        case_data.setdefault("notes", []).append(entry)
        self.save(case_data)
        return entry


def export_case_markdown(
    case_data: dict,
    destination: Path,
    *,
    overwrite: bool = False,
    catalog_directory: Path | None = None,
) -> dict:
    """Export one persisted Case workspace to a Markdown 8D / troubleshooting draft."""
    target = Path(destination).expanduser().resolve(strict=False)
    if target.suffix.lower() != ".md":
        raise CatalogError("case_export_requires_markdown")
    if catalog_directory is not None:
        cat_root = Path(catalog_directory).expanduser().resolve(strict=False)
        if target == cat_root or target.is_relative_to(cat_root):
            raise CatalogError("case_export_inside_catalog_forbidden")
    if not target.parent.is_dir():
        raise CatalogError("case_export_parent_missing")

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

    payload = "\n".join(lines).rstrip() + "\n"
    try:
        with target.open("w" if overwrite else "x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise CatalogError("case_export_exists") from exc
    return {
        "status": "exported",
        "path": str(target),
        "bytes": len(payload.encode("utf-8")),
        "turns": len(turns),
        "notes": len(notes),
    }


def mcp_client_config(
    catalog_directory: Path = DEFAULT_CATALOG,
    *,
    toolset: str = "agent",
    include_agent_env_placeholders: bool = True,
    http_origin: str = "http://127.0.0.1:8765",
) -> dict:
    """Return ready-to-paste MCP client & mcpo snippets without secrets."""
    cat = str(Path(catalog_directory).expanduser().resolve())
    py_venv = Path(python_executable(".venv"))
    py = str(py_venv) if py_venv.is_file() else sys.executable
    script = str((ROOT / "phase6_mcp.py").resolve())
    args = ["-X", "utf8", "-B", script, "--catalog", cat]
    if toolset == "agent":
        args.extend(["--toolset", "agent"])
    env_block = {}
    if include_agent_env_placeholders:
        env_block = {
            "PCB_ALLOW_CLOUD_EGRESS": "true",
            "PCB_EGRESS_SCOPE": "sanitized_catalog",
            "PCB_AGENT_LOOP": "true",
            "PCB_LLM_PROVIDER": "openai_chat_completions",
            "PCB_LLM_BASE_URL": "https://api.deepseek.com/v1",
            "PCB_LLM_MODEL": "deepseek-chat",
            "PCB_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
            "DEEPSEEK_API_KEY": "<YOUR_API_KEY>",
        }
    return {
        "recommended_mcpServers_using_local_profile": {
            "mcpServers": {
                "pcb-assistant": {
                    "command": py,
                    "args": args,
                    "cwd": str(ROOT),
                }
            }
        },
        "mcpServers": {
            "pcb-assistant": {
                "command": py,
                "args": args,
                "cwd": str(ROOT),
                **({"env": env_block} if env_block else {}),
            }
        },
        "streamable_http_mcp": {
            "mcpServers": {
                "pcb-assistant-http": {
                    "type": "streamable-http",
                    "url": f"{http_origin.rstrip('/')}/mcp",
                }
            }
        },
        "mcpo_command_for_open_webui": (
            f"uvx mcpo --port 8000 -- \"{py}\" "
            + " ".join(f'"{item}"' if " " in item else item for item in args)
        ),
    }


def _ask_once(
    question: str,
    history: list[dict],
    *,
    mode: str,
    catalog_directory: Path,
    env: dict[str, str],
    filters: dict | None = None,
):
    """Execute one turn through router.answer() under the active environment."""
    arguments = {
        "question": question,
        "mode": "managed" if mode == "agent" else "evidence_only",
        "history": list(history)[-8:],
        **{k: v for k, v in (filters or {}).items() if v},
    }
    request_id = f"REQ_WB_{int(time.time() * 1000)}"
    # Apply merged profile env only for the duration of this turn.
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            os.environ[k] = v
        if mode == "agent" and "PCB_AGENT_LOOP" not in env:
            os.environ["PCB_AGENT_LOOP"] = "true"
        with phase6_mcp._catalog_context(catalog_directory):
            return router.answer(
                arguments,
                request_id,
                catalog_directory=catalog_directory,
                operation_runner=lambda name, inputs: phase6_mcp._tool_call_result(
                    name, inputs, request_id),
            )
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def _print_turn(outcome, elapsed_s: float):
    result = outcome.result
    route = (result.get("workflow") or {}).get("route", "unknown")
    answer_mode = result.get("answer_mode", "unknown")
    trace = result.get("agent_trace") or []
    print()
    print(result.get("answer", ""))
    meta = [
        f"route={route}",
        f"mode={answer_mode}",
        f"status={outcome.status}",
        f"egress={outcome.data_egress}",
        f"elapsed={elapsed_s:.1f}s",
    ]
    if trace:
        tools_summary = ",".join(item.get("tool", "?") for item in trace)
        meta.append(f"tools={len(trace)}({tools_summary})")
    usage = (result.get("model") or {}).get("usage") or {}
    if usage.get("total_tokens"):
        meta.append(f"tokens={usage['total_tokens']}")
    if outcome.status == "fallback":
        reason = (result.get("model") or {}).get("fallback_reason")
        if reason:
            meta.append(f"fallback={reason}")
    print("\n[" + " | ".join(meta) + "]")


def _repl(
    *,
    catalog_directory: Path,
    workspace: CaseWorkspace,
    initial_case: dict | None,
    mode: str,
    env: dict[str, str],
    profile_loaded: bool,
):
    active_case = initial_case
    ephemeral_history: list[dict] = []
    last_outcome = None
    active_mode = mode

    print("PCB 压合排查工作台（只读 Catalog + 脱敏出境受控 Agent）")
    print(
        f"当前模式：{active_mode} | 本地配置：{'已加载' if profile_loaded else '未找到（使用当前环境变量）'}"
        + (f" | 当前病例：{active_case['case_id']}" if active_case else "")
    )
    print(
        "指令：/mode agent|local 切换模式 | /case new|open|list|show|close <ID> 管理病例 | "
        "/note <记录> | /report <路径.md> [--overwrite] 导出排查草稿 | /sources | /trace | /clear | /exit"
    )

    while True:
        prompt = f"\n[{active_case['case_id']}|{active_mode}] 你> " if active_case else f"\n[{active_mode}] 你> "
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit"):
            print("已退出。")
            return 0
        if line == "/clear":
            if active_case:
                active_case["history"] = []
                workspace.save(active_case)
            ephemeral_history.clear()
            print("当前对话上下文已清空。")
            continue
        if line.startswith("/mode"):
            parts = line.split()
            if len(parts) == 2 and parts[1] in ("agent", "local"):
                active_mode = parts[1]
                print(f"已切换为 {active_mode} 模式。")
            else:
                print("用法：/mode agent 或 /mode local")
            continue
        if line == "/sources":
            if not last_outcome:
                print("还没有查询记录。")
                continue
            cits = last_outcome.result.get("citations") or []
            if not cits:
                print("上一轮没有返回引用。")
                continue
            for item in cits:
                cid = item.get("excerpt_id") or item.get("citation_id")
                print(f"- {cid} | {item.get('source_type', '')} | {item.get('title', '')}")
            continue
        if line == "/trace":
            if not last_outcome:
                print("还没有查询记录。")
                continue
            trace = last_outcome.result.get("agent_trace") or []
            print(json.dumps(trace, ensure_ascii=False, indent=2))
            continue
        if line.startswith("/case"):
            parts = line.split(maxsplit=2)
            sub = parts[1] if len(parts) >= 2 else "list"
            if sub == "list":
                print(json.dumps(workspace.list_cases(), ensure_ascii=False, indent=2))
            elif sub in ("new", "open") and len(parts) >= 3:
                try:
                    active_case = workspace.open_or_create(parts[2])
                    print(f"已打开病例：{active_case['case_id']}（已有 {len(active_case['turns'])} 轮记录）")
                except CatalogError as exc:
                    print(f"病例操作失败：{exc}", file=sys.stderr)
            elif sub == "show":
                if not active_case:
                    print("当前未打开病例。")
                else:
                    summary = {
                        "case_id": active_case["case_id"],
                        "title": active_case["title"],
                        "turns": len(active_case["turns"]),
                        "notes": active_case["notes"],
                        "updated_at": active_case["updated_at"],
                    }
                    print(json.dumps(summary, ensure_ascii=False, indent=2))
            elif sub == "close":
                active_case = None
                print("已关闭当前病例，回到临时会话。")
            else:
                print("用法：/case list | /case new <ID> | /case open <ID> | /case show | /case close")
            continue
        if line.startswith("/note"):
            if not active_case:
                print("请先用 /case new <ID> 打开一个病例。", file=sys.stderr)
                continue
            note_text = line[5:].strip()
            try:
                entry = workspace.append_note(active_case, note_text)
                print(f"已记录现场备注：{entry['text']}")
            except CatalogError as exc:
                print(f"记录失败：{exc}", file=sys.stderr)
            continue
        if line.startswith("/report"):
            if not active_case:
                print("请先用 /case new <ID> 打开一个病例。", file=sys.stderr)
                continue
            parts = line.split()
            overwrite = "--overwrite" in parts
            path_parts = [p for p in parts[1:] if p != "--overwrite"]
            if not path_parts:
                print("用法：/report <路径.md> [--overwrite]", file=sys.stderr)
                continue
            try:
                receipt = export_case_markdown(
                    active_case,
                    Path(path_parts[0]),
                    overwrite=overwrite,
                    catalog_directory=catalog_directory,
                )
                print(json.dumps(receipt, ensure_ascii=False, indent=2))
            except CatalogError as exc:
                print(f"导出失败：{exc}", file=sys.stderr)
            continue

        current_history = active_case["history"] if active_case else ephemeral_history
        started = time.monotonic()
        try:
            outcome = _ask_once(
                line,
                current_history,
                mode=active_mode,
                catalog_directory=catalog_directory,
                env=env,
            )
        except Exception as exc:
            print(f"执行失败：{exc}", file=sys.stderr)
            continue
        elapsed_s = time.monotonic() - started
        last_outcome = outcome
        if active_case:
            workspace.append_turn(active_case, line, outcome)
        else:
            ephemeral_history.append({"role": "user", "content": line})
            ephemeral_history.append({
                "role": "assistant",
                "content": str(outcome.result.get("answer") or "")[:2800],
            })
        _print_turn(outcome, elapsed_s)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="PCB 压合多轮排查工作台（受控出境 Agent + 本地持久化病例）",
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--mode", choices=("agent", "local"), default="agent",
        help="agent 启用脱敏出境受控 Agent（无配置自动回退本地）；local 强制纯本地",
    )
    parser.add_argument("--case", dest="case_id", help="打开或新建本地持久化病例 ID")
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument(
        "--agent-config", type=Path, default=None,
        help="可选本地配置 JSON（默认读取 .private/agent_profile.json）",
    )
    parser.add_argument("--profile", default=None, help="选择命名模型渠道（默认使用当前激活渠道）")
    parser.add_argument("--json", action="store_true", dest="as_json", help="单次提问输出完整 JSON")
    parser.add_argument("--mcp-config", action="store_true", help="输出 MCP 客户端 / Open WebUI (mcpo) 接入配置")
    parser.add_argument("--toolset", choices=("single", "agent"), default="agent")
    parser.add_argument("question", nargs="?", default="")
    args = parser.parse_args(argv)

    if args.mcp_config:
        cfg = mcp_client_config(args.catalog, toolset=args.toolset)
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0

    try:
        env, loaded = load_local_agent_env(args.agent_config, profile_name=args.profile)
        workspace = CaseWorkspace(args.cases_dir, catalog_directory=args.catalog)
        active_case = workspace.open_or_create(args.case_id) if args.case_id else None
    except CatalogError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    if args.question:
        history = active_case["history"] if active_case else []
        started = time.monotonic()
        outcome = _ask_once(
            args.question,
            history,
            mode=args.mode,
            catalog_directory=args.catalog,
            env=env,
        )
        elapsed_s = time.monotonic() - started
        if active_case:
            workspace.append_turn(active_case, args.question, outcome)
        if args.as_json:
            print(json.dumps({
                "status": outcome.status,
                "external_services": outcome.external_services,
                "data_egress": outcome.data_egress,
                "warnings": list(outcome.warnings),
                "errors": list(outcome.errors),
                "result": outcome.result,
            }, ensure_ascii=False, indent=2))
        else:
            _print_turn(outcome, elapsed_s)
        return 0

    return _repl(
        catalog_directory=args.catalog,
        workspace=workspace,
        initial_case=active_case,
        mode=args.mode,
        env=env,
        profile_loaded=loaded,
    )


if __name__ == "__main__":
    raise SystemExit(main())
