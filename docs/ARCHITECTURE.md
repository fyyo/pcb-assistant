# 架构概览

```mermaid
flowchart LR
    Source[本机原始资料] --> Clean[清洗、提取与 OCR]
    Clean --> Catalog[脱敏 Catalog]
    CLI[CLI 与连续对话] --> Router[问答分流]
    MCP[stdio MCP: pcb_answer] --> Router
    Router --> Evidence[检索与引用核验]
    Router --> Diagnose[压合诊断]
    Catalog --> Evidence
    Catalog --> Diagnose
    Evidence --> Answer[回答与证据缺口]
    Diagnose --> Answer
```

| 层 | 主要模块 | 职责 |
| --- | --- | --- |
| 数据与版本 | `catalog_common.py`、`catalog_build.py`、`catalog_incremental.py` | SQLite、稳定身份、导出清单、新版本构建与增量导入 |
| 检索与证据 | `pcb_knowledge.py`、`catalog_evidence.py`、`catalog_agent.py` | 关键词收敛、有界候选、来源去重、摘录与引用 |
| 材料术语 | `pcb_material_concepts.py` | Tg／Td／RF／GT 查询别名、完整定义与测试说明排序、适用性子问题缺口 |
| 表格字段 | `pcb_table_*.py`、`pcb_stackup_checklist.py`、`pcb_field_evidence.py` | 工作表／行／区块定位、字段追问、资料缺口 |
| 对话与分流 | `catalog_chat.py`、`pcb_assistant.py` | 上下文、知识与诊断路由、导出与反馈 |
| 诊断 | `agent/` | 病例事实、候选原因、本地证据和评测任务 |
| 接口 | `phase6_mcp.py`、`phase6_contract.py` | 单工具 stdio、参数检查、信封与输出边界 |
| 可选生成 | `model_chat.py`、`phase6_managed.py` | 本地模型或显式开启的公开证据托管综合 |
| 数据维护与评审 | `scripts/data/`、`scripts/audits/`、`scripts/evaluation/` | 独立运行的数据处理、核验与人工复核工具 |
| 可选实验 | `scripts/experimental/` | OpenViking 影子检索工具 |

普通查询使用只读连接；来源文本是不可信数据，不能修改工具配置或执行指令。构建、增量、反馈与训练是各自显式的写入流程。检索分页只表示有界候选窗口，不保证穷尽全库。

解释与选型查询在单次调用内复用候选正文及摘录，选页后仅保留该页所需结果。来源定位信息附加到独立副本；新查询重新读取 Catalog。身份、结果指纹与 JSON 导出共用 `catalog_common.json_text`，保持稳定序列化并拒绝非有限数值。

知识引用可携带原文字符偏移和哈希；诊断展示可能合并空白，应按返回格式验证。不同引用路径的定位信息不能互相冒用。字段覆盖表示模式识别到的资料范围，不是工程参数合格判定。

材料指标的定义、比较和适用性问题进入知识解释流程；明确的缺陷排查保留诊断流程。术语别名只作用于查询，不改变既有全文索引。跨行定义仍保留连续原文及偏移；公式变量 `tD` 和 RF 射频文字不作为对应材料定义。名称、表头和定义不能确认卸压温度或实板流胶／填充时间的对应关系，回答单列尚未核实的子问题。

原始邮件归档与脱敏 Catalog 分开保存，前者不通过 MCP 暴露。外部搜索、托管综合和客户端模型的数据范围见[隐私说明](PRIVACY.md)与[MCP 接入](UNIFIED_PCB_ASSISTANT.md)。
