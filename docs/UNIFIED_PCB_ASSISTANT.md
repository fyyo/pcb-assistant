# 统一压合助手 MCP

更新时间：2026-11-16。当前 MCP 只对外暴露 `pcb_answer`。知识咨询、缺陷诊断和必要追问由服务内部自动分流，客户端不选择检索或诊断工具。

## 接入

将以下示例中的项目路径和 Catalog 目录替换为本机实际位置：

```text
程序：C:\path\to\pcb\.venv\Scripts\python.exe
参数：-X utf8 -B C:\path\to\pcb\phase6_mcp.py --catalog C:\path\to\pcb\data\my_catalog
工作目录：C:\path\to\pcb
传输：stdio
```

所选目录同时用于问答、诊断、状态、文档、关联附件和信封版本；提问参数不能覆盖选库。切换目录后重新启动服务。默认路径为本机交付时的 v9，源码不包含该数据库；详见[数据库维护](DATABASE_OPERATIONS.md)和[指定 Catalog](MCP_CATALOG_SELECTION.md)。

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

知识问答仍支持原来的显式 `managed` 模式及服务端出境开关；缺陷诊断统一走本地证据流程，即使请求了 `managed` 也不调用托管模型，并返回提示。原 CLI 的公开知识包与 SearXNG 搜索能力继续保留；本次没有把它们接入 MCP。

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

- `phase6_mcp.py`：唯一协议实现，负责参数检查、输出脱敏、文本预算和信封。
- `pcb_assistant.py`：内部自动分流和病例上下文合并。
- `agent.workflow.diagnose()`：诊断业务逻辑，继续供 CLI 和无标签评测复用。
- `phase6_managed.answer()`：知识问答及已有的可选托管综合。
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
