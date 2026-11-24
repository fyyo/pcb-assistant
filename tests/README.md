# 测试

使用 Python 3.11 或 3.12。测试使用合成资料和临时 Catalog，不读取本机数据库、原始邮件或 `.private/` 记录。`tests/data/` 下只有自造的 PDF 夹具（CJK 文本页、稀疏页与带内嵌图片的正文页），用于 PDF 解析和渲染用例。

```powershell
python -m pip install -r requirements/requirements-test.txt
python -m pip check
python -X utf8 -B -m unittest discover -v
```

`requirements-test.txt` 包含 CPU OCR 依赖；OCR 后端当前要求 Python < 3.13。需要完整邮件处理或训练时，按[依赖说明](../requirements/README.md)单独安装。

关键专项：

| 测试 | 覆盖 |
| --- | --- |
| `test_catalog.py`、`test_catalog_chat.py`、`test_catalog_evidence.py` | Catalog 构建、检索、引用与对话 |
| `test_pp_selection.py`、`test_stackup_retrieval.py`、`test_table_*.py`、`test_field_evidence.py` | 材料选型、表格字段、来源行列与证据缺口 |
| `test_attachments.py`、`test_ocr_supplement.py`、`test_privacy.py` | 附件、OCR 与隐私处理 |
| `test_agent_workflow.py`、`test_agent_scope.py`、`test_pcb_assistant.py` | 诊断范围、分流与病例上下文 |
| `test_phase6_*.py`、`test_mcp_catalog_selection.py` | 契约、MCP、脱敏与 stdio 启动 |
| `test_catalog_backup.py`、`test_database_delivery.py` | 合成 Catalog 备份、恢复与交付检查 |

需要私有来源或真实客户端的检查在条件不具备时按设计跳过。`test_cli_entrypoints.py` 验证包内命令及兼容启动入口。
