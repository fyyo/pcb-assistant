# Phase 6 契约与评测骨架

此目录保存内部接口 schema、工具映射和合成评测骨架。当前公开 MCP 只暴露 `pcb_answer`，接入方式见[统一 MCP](../../docs/UNIFIED_PCB_ASSISTANT.md)。

## 文件

- `pcb-agent-v1.envelope.schema.json`：所有 MCP 工具的统一返回信封。
- `pcb-answer-v1.schema.json`：云端模型返回的结构化答案。
- `pcb-agent-v1.tools.json`：8 个只读工具、输入参数和出境策略。
- `pcb-agent-v1.errors.json`：稳定错误码和降级原因。
- `tool_mapping.json`：8 个工具到现有只读函数的静态审计映射。
- `eval/case.schema.json`：评测用例结构。
- `eval/cases.jsonl`：150 条 Phase 6.0 评测骨架。
- `eval/annotations.jsonl`：专家 gold sidecar（完成标注后生成，不修改 seed 用例）。

## 验证

```powershell
.venv\Scripts\python.exe -X utf8 -B phase6_contract.py --validate
.venv\Scripts\python.exe -X utf8 -B phase6_eval.py --validate
.venv\Scripts\python.exe -X utf8 -B phase6_annotation.py --status
.venv\Scripts\python.exe -X utf8 -B phase6_mapping.py
.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --self-test
.venv\Scripts\python.exe -X utf8 -B -m unittest -v tests.test_phase6_contract
```

重新生成：

```powershell
.venv\Scripts\python.exe -X utf8 -B phase6_contract.py --write
.venv\Scripts\python.exe -X utf8 -B phase6_mapping.py --write
.venv\Scripts\python.exe -X utf8 -B phase6_annotation.py --template .tmp_work/phase6_annotations_draft.jsonl
```

## 当前边界

- 所有工具 `side_effects=false`。
- 除 `pcb_answer` 外，所有工具 `external_services=false`、`data_egress=none`。
- `pcb_answer` 默认 `evidence_only`；`managed` 只有在调用方逐请求选择、服务端显式允许出境、公开证据通过本地脱敏且 Provider 配置有效时才会尝试。
- schema 中保留 8 个内部工具定义，公开 MCP 只注册 `pcb_answer`；其他操作由服务内部调用。
- 托管请求只发送本地检索到的公开证据摘录；Provider 返回必须通过结构、引用、隐私和数值依据校验，失败时回退本地证据答案。
- 文档 ID 使用真实 Catalog 前缀 `MAIL_/CASE_/KNOWLEDGE_/ATTACHMENT_/PUBLIC_`。
- `pcb_evidence` 要求非空 `query`；`pcb_get_document` 默认不返回正文。
- `pcb_get_document` 可用 `include_attachments=true` 返回邮件/附件关联的可读正文；
  `pcb_evidence` 会自动附带前几个来源的附件提取正文，避免只暴露邮件编号。
- MCP `content[].text` 是面向客户端展示的紧凑摘要，完整、未裁剪的返回信封位于
  `structuredContent`；程序化调用应以 `structuredContent` 为准。
- 150 条问题只是 `seed_authored` 骨架，全部 `annotation.status=pending`。
- `relevant_doc_ids` 和 `required_evidence_ids` 必须由专家从真实 Catalog 标注。
- 专家标注写入独立 sidecar；只有通过校验的 `complete` 记录会合并进评测，候选证据不会被自动升级为 gold。
- 在 gold 标注和 Phase 6.3 门槛通过前，不得把该数据集表述为发布验收结果。
