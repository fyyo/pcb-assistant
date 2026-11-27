# MCP 接入与 Catalog 选择

更新时间：2026-11-16。当前 MCP 只对外暴露 `pcb_answer`。知识咨询、缺陷诊断和必要追问由服务内部自动分流，客户端不选择检索或诊断工具。

## 接入

将以下示例中的项目路径和 Catalog 目录替换为本机实际位置：

```text
程序：C:\path\to\pcb\.venv\Scripts\python.exe
参数：-X utf8 -B C:\path\to\pcb\phase6_mcp.py --catalog C:\path\to\pcb\data\my_catalog
工作目录：C:\path\to\pcb
传输：stdio
```

所选目录同时用于问答、诊断、状态、文档、关联附件和信封版本；提问参数不能覆盖选库。路径不存在、schema 错误或版本登记无效时启动失败，不创建空库或回退到其他库。切换目录后重启服务。源码不包含数据库；本机默认路径来自 `pcb_assistant.catalog.common`，其他机器应显式选择自有库。备份与恢复见[数据库维护](DATABASE_OPERATIONS.md)。

```powershell
python -X utf8 -B phase6_mcp.py --catalog data/my_catalog
python -X utf8 -B phase6_mcp.py --catalog data/my_catalog --self-test
python -X utf8 -B -m pcb_assistant.evaluation.stdio_trial --catalog data/my_catalog --json
```

已有 DSH 配置无需另加诊断服务。重启 DSH 或重载服务后，在新会话中确认工具列表只有 `pcb_answer`。旧会话可能缓存旧工具列表；旧的 `pcb_search`、`pcb_evidence` 等工具名现在会被 MCP 拒绝。

`python -m agent.mcp` 是同一服务的兼容启动别名，也只暴露 `pcb_answer`。不要把两个启动命令同时注册为两个服务。

```powershell
.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --list-tools
.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --self-test
.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --call pcb_answer --arguments-json '{"question":"压合后板边出现白斑，怎么排查？"}'
```

## 内部行为

| 提问 | 行为 |
| --- | --- |
| 什么是白斑？白斑和分层有什么区别？ | 本地资料检索与证据问答 |
| 压合后板边出现白斑，怎么排查？ | 提取病例事实，执行受控压合诊断 |
| 补充：FR-4，6层板，真空20 kPa | 有当前病例历史时合并补充事实，重新诊断 |
| 压合后有问题，怎么解决？ | 先追问可检索的缺陷与条件 |
| 同时出现白斑和分层 | 追问主要失效现象及位置、批次关系 |
| 另一批板出现分层 | 开始新病例，清除旧病例参数 |
| 本地知识库有多少文档？ | 内部读取本地库状态和统计 |
| 读取文档 KNOWLEDGE_… | 按完整文档 ID 读取正文 |
| 查看相似案例或关联关系，并给出完整文档 ID | 内部调用相似案例或关系查询 |

分流使用本地确定性规则，默认不调用模型、不联网。它不是语义理解模型，表达复杂或超出已覆盖规则时仍可能分流不准，应结合使用反馈继续纠正。

显式传入来源或文档类型筛选时，仍走受筛选的证据检索，不通过文档 ID 查询绕过筛选。

知识问答仍支持原来的显式 `managed` 模式及服务端出境开关；默认情况下缺陷诊断仍走本地证据流程，即使请求了 `managed` 也不调用托管模型。

## 可选脱敏出境受控 Agent 与工具箱模式

服务默认完全离线（`mode="evidence_only"`、`PCB_EGRESS_SCOPE=public_only`、`PCB_AGENT_LOOP=false`、`--toolset single`）。需要调用外部兼容 API（如 DeepSeek、通义千问、OpenAI 等）或对外暴露只读调查工具箱时，可按需显式开启：

| 环境变量 / 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `PCB_ALLOW_CLOUD_EGRESS` | `false` | 服务端云端出境总开关，必须显式设为 `true` |
| `PCB_LLM_PROVIDER` | `openai_responses` | 支持 `openai_responses`（`/v1/responses`）与 `openai_chat_completions`（`/v1/chat/completions`） |
| `PCB_LLM_BASE_URL` | `https://api.openai.com/v1` | HTTPS 端点地址；仅当显式设 `PCB_ALLOW_LOCALHOST_HTTP=true` 时允许本机 `http://127.0.0.1` / `localhost` |
| `PCB_LLM_MODEL` / `PCB_LLM_API_KEY_ENV` | 空 / `OPENAI_API_KEY` | 模型名称与存放密钥的环境变量名 |
| `PCB_EGRESS_SCOPE` | `public_only` | `public_only` 仅放行公开参考资料；`sanitized_catalog` 允许经本地 spaCy NER + 敏感词二次清洗且零残留的 Catalog 有界摘录出境（受 `PCB_MAX_EVIDENCE_CHARS` 限制） |
| `PCB_AGENT_LOOP` | `false` | 设为 `true` 且请求 `mode="managed"` 时，启用有界多步受控 Agent（知识与缺陷诊断均可用），最终结果强制过引用、数字依据与残留校验，失败自动回退本地确定性回答 |
| `PCB_AGENT_BACKEND` | `native` | `native` 为标准库零额外依赖循环；安装 `requirements/requirements-agent.txt` 后可切换为 `pydantic_ai` |
| `--toolset agent` | `single` | 启动 `phase6_mcp.py --toolset agent` 时开放契约登记的全部 8 个只读工具，便于接 Open WebUI（配合 `mcpo`）、Claude Desktop 或 Codex 进行多步查证 |

```powershell
# 示例：使用 Chat Completions 兼容端点运行脱敏出境受控 Agent
$env:PCB_ALLOW_CLOUD_EGRESS = 'true'
$env:PCB_EGRESS_SCOPE = 'sanitized_catalog'
$env:PCB_AGENT_LOOP = 'true'
$env:PCB_LLM_PROVIDER = 'openai_chat_completions'
$env:PCB_LLM_BASE_URL = 'https://api.deepseek.com/v1'
$env:PCB_LLM_MODEL = 'deepseek-chat'
$env:PCB_LLM_API_KEY_ENV = 'DEEPSEEK_API_KEY'
$env:DEEPSEEK_API_KEY = '<你的密钥>'

.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --catalog data/my_catalog `
  --call pcb_answer `
  --arguments-json '{"question":"8层板 IT-180A 压合后板边出现白斑，真空曲线正常，怎么排查？","mode":"managed"}'

# 示例：面向外部 Agent 客户端开放全部 8 个只读工具
.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --catalog data/my_catalog --toolset agent --list-tools
```

## 多轮排查工作台与本地配置（`workbench`）

不想每次手动设置 8 个环境变量时，可将本机私有配置写入被 `.gitignore` 忽略的 `.private/agent_profile.json`（环境变量始终优先覆盖该文件，**禁止提交至版本库**）：

```json
{
  "PCB_ALLOW_CLOUD_EGRESS": true,
  "PCB_EGRESS_SCOPE": "sanitized_catalog",
  "PCB_AGENT_LOOP": true,
  "PCB_LLM_PROVIDER": "openai_chat_completions",
  "PCB_LLM_BASE_URL": "https://api.deepseek.com/v1",
  "PCB_LLM_MODEL": "deepseek-chat",
  "PCB_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
  "api_key": "<仅保存在本机 .private/ 中>"
}
```

启动交互式多轮排查工作台或生成 MCP / Open WebUI（`mcpo`）接入配置：

```powershell
# 启动交互式多轮工作台，并将排查记录持久化到 .private/cases/WS_20261127.json
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog workbench --case WS_20261127

# 单次问答（自动读取 .private/agent_profile.json，无配置则安全回退本地证据回答）
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog workbench "8层板 IT-180A 压合后板边出现白斑，真空曲线正常，怎么排查？"

# 输出不含密钥的 Claude / Codex / Open WebUI (mcpo) 接入配置片段
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog workbench --mcp-config --toolset agent
```

工作台支持 `/mode agent|local`、`/case new|open|list|show|close`、`/note <现场记录>`、`/report <路径.md>`（导出 Markdown 排查草稿）、`/sources` 与 `/trace`。

## 本机 Web 工作台与 HTTP/MCP 服务（双击即用）

Windows 下可直接双击根目录的 `start_workbench.bat`，或通过命令启动零依赖本机 Web 工作台（默认仅监听 `127.0.0.1:8765`）：

```powershell
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog web --open
```

该服务同时提供：
- `GET /`：浏览器交互式多轮排查工作台（支持在弹窗中保存 `.private/agent_profile.json`、点击 `EXC_...` 展开原文出处、管理病例备注与下载 Markdown 排查草稿）；
- `POST /api/answer`：标准 JSON 问答/诊断接口；
- `POST /mcp`：标准 MCP Streamable HTTP 端点（默认 `--toolset agent`），可直接供 Dify、MaxKB、FastGPT、Open WebUI 或企业微信/钉钉/飞书数字员工桥接调用。非本机地址监听必须提供 `--api-token`（或 `PCB_WEB_API_TOKEN`）Bearer 鉴权。

原 CLI 的公开知识包与 SearXNG 搜索能力继续保留；本次没有把它们接入 MCP。

## 多轮上下文

服务不保存全局会话。客户端每次调用同一个 `pcb_answer`，通过已有可选 `history` 携带最近对话（最多 8 条消息）；初次提问只需 `question`。不需要填写流程或路由参数。

```json
{
  "question": "补充：FR-4，6层板，实际位置在板中。",
  "history": [
    {"role": "user", "content": "压合后板边出现白斑，怎么排查？"},
    {"role": "assistant", "content": "请补充材料、叠构和曲线记录。"}
  ]
}
```

只有用户消息能提供病例事实，助手的候选原因与引用不会变成现场观察。后补值覆盖同一字段的旧值。明确换批次、换缺陷或切换知识话题时结束旧病例继承。历史截断后未携带的信息不会被服务凭空恢复；新会话不共享其他会话的病例。

## 结构与维护

- `phase6_mcp.py`：兼容启动入口，唯一协议实现在 `pcb_assistant/interfaces/mcp.py`，负责参数检查、输出脱敏、文本预算和信封。
- `pcb_assistant/router.py`：内部自动分流和病例上下文合并。
- `agent.workflow.diagnose()`：诊断业务逻辑，继续供 CLI 和无标签评测复用。
- `pcb_assistant.interfaces.managed.answer()`：知识问答及已有的可选托管综合。
- 原有 7 个底层操作保留为本地维护能力及内部函数，不再对外提供 MCP 工具。

外层沿用 `pcb-agent-v1` 信封。知识与诊断回答保持统一的 `answer`、`citations`、`missing_information`、`clarifying_questions`、引用校验和结果指纹；诊断细节保存在 `result.diagnosis`，内部路由记录在 `result.workflow`。诊断的 `insufficient_evidence` 映射为外层 `no_evidence`，细分状态仍保存在诊断结果中。

输出先脱敏，再重算嵌套诊断与整体回答指纹。展示文本保持在 7,600 字符以内；完整结构化结果随调用返回。

`contracts/phase6/pcb-agent-v1.tools.json` 继续登记 8 个历史能力定义用于本地维护；`public_tools=["pcb_answer"]` 明确当前唯一公开工具，工具映射的 `public_exposed` 同步标记。历史验收报告保留原样，不代表新版本已完成 DSH 或真实模型回合联调。

当前仍是内部辅助工具；不要求专家标签，原因准确率保持 `n/a`，不把引用有效或规则稳定等同于根因已确认。

## 验证

```powershell
python -X utf8 -B -m unittest -v tests.test_phase6_mcp tests.test_mcp_catalog_selection tests.test_pcb_assistant
```

测试使用合成输入；本机数据规模和历史验收结果不作为新环境的验证结论。
