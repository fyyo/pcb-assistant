# 测试目录

所有 Python 回归测试集中在此目录。项目根目录不再放置 `test_*.py`，避免交付脚本与测试代码混杂。

`test_contextual_retrieval.py` 覆盖厚铜条件、解释类问答，以及 Tg／Td／RF／GT 的缩写歧义、跨行定义、适用性缺口、诊断话题切换与原文偏移／哈希。

## 运行

```powershell
# 安装合成测试及 CPU OCR 依赖
.venv\Scripts\python.exe -m pip install -r requirements/requirements-test.txt

# 全量发现；真实 Codex 预检在未显式开启时按设计跳过
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -v

# 指定专项
.venv\Scripts\python.exe -X utf8 -B -m unittest -v tests.test_catalog_chat
```

## 分组

| 范围 | 文件 |
| --- | --- |
| Catalog 核心 | `test_catalog.py`、`test_catalog_attachment_sources.py`、`test_pcb_knowledge.py` |
| 数据库备份与恢复 | `test_catalog_backup.py`（真实 SQLite 恢复、身份键、拒绝覆盖、篡改及路径穿越拒绝） |
| 交付后维护检查 | `test_database_delivery.py`（只读、默认库／回退库／代码变化、身份键变化、额外文件、活动日志及损坏备份；输出不含私有值） |
| Catalog 检索与答案 | `test_catalog_insights.py`、`test_catalog_evidence.py`、`test_catalog_agent.py`、`test_catalog_chat.py`、`test_contextual_retrieval.py`（厚铜条件、连续原文及知识解释） |
| Catalog 增量与评估 | `test_incremental.py`、`test_catalog_evaluation.py` |
| PP 选型检索 | `test_pp_selection.py`（意图、完整牌号与数值条件、字段对应、连续原文、条件缺口、追问，以及单次查询复用、来源元数据隔离与新查询重读） |
| 表格定位与原文对应 | `test_table_context.py`（表头／单行对应、单位、损坏结构拒绝、分别引用、页／工作表／行列、完整正文去重与分页） |
| 表格行类型与并列区块 | `test_table_row_roles.py`（规格章节、备注／签核、数据候选、行号重置、并列边界、跨区块参数拒绝、完整行与分类依据引用） |
| 叠构表召回 | `test_stackup_retrieval.py`（高覆盖干扰下的稀疏表召回、布型×张数、来源过滤、有界候选、分页与完整表头／命中行） |
| 单一区块核对清单 | `test_stackup_checklist.py`（左右侧隔离、原文与引用范围、缺失／重复表头、单位／缺值、材料代码、跨来源和截断行拒绝） |
| 表格连续追问 | `test_table_followup.py`（来源序号／文档 ID、行／区块继承、多表头、过滤与缺失不回退、有状态 CLI 和无状态 MCP、历史文字不作证据） |
| 字段追问 | `test_table_fields.py`（RC／介厚等别名、字段继承与替换、重复／缺失表头、单位／缺值、左右隔离、材料代码保护、可选生成器跳过） |
| 字段补证清单 | `test_field_evidence.py`（文件／材料身份、表头／单位／缺值的具体资料请求、同范围去重、稳定 ID、引用、字段继承、未核验声明与空证据） |
| 缺失引用审计 | `test_missing_references.py`、`test_missing_reference_origin.py` |
| 邮件、附件与隐私 | `test_mail_status.py`、`test_mail_corpus_audit.py`、`test_attachments.py`、`test_privacy.py`、`test_candidate_privacy.py`（候选身份修复、数值与来源保留）、`test_case_identity_repair.py`（案例引用偏移、片段/FTS、独立案例来源及历史排除记录） |
| 持续学习与训练数据 | `test_continuous.py`、`test_learning.py`、`test_case_study.py`、`test_existing_cases.py`、`test_public_training.py`、`test_public_lamination.py`、`test_public_lamination_v2.py` |
| 压合诊断 Agent | `test_agent_workflow.py`、`test_agent_scope.py`（CFRP 修补背景隔离、来源哈希、公开包及外部兜底）、`test_agent_evaluation.py`、`test_agent_calibration.py`、`test_agent_mcp.py` |
| 导出 | `test_dify_export.py` |
| 统一压合助手 | `test_pcb_assistant.py`（自动分流、上下文、引用与指纹）、`test_mcp_catalog_selection.py`（进程选库、附件、版本及真实 stdio） |
| 命令入口 | `test_cli_entrypoints.py`（目录迁移后的模块启动、异目录兼容入口、可选工具帮助） |
| Phase 6 | `test_phase6_*.py` |
| 内部试用 | `test_internal_pilot_feedback.py`、`test_internal_pilot_review.py` |
| 影子检索与建议复核 | `test_openviking_shadow.py`、`test_recommendation_review.py` |

`catalog_test_fixtures.py` 和 `phase6_test_fixtures.py` 是测试共享夹具，不属于生产代码。协议、托管 mock 和 stdio 回归使用临时合成 Catalog，不依赖本机交付数据库。没有对应生产模块的临时调试脚本、补丁和导出产物应放在 `.tmp_work/`，不要加入本目录。

日期命名的本机验收报告已移至 `.private/project_records/`，不随源码分发。测试使用合成夹具；依赖真实本机资料或客户端的检查在条件不具备时跳过。旧资产快照及已退役一次性脚本对应的测试已删除，当前隐私规则、工程代码保留和 OCR 增量入库测试继续运行。
