# 维护工具

日常查询使用根目录的 `knowledge_catalog.py` 和 `phase6_mcp.py`。其他工具按需执行；实际读写路径和覆盖行为以各模块的 `--help` 为准。

| 目录或工具 | 用途 |
| --- | --- |
| `catalog_backup.py` | 创建、校验并恢复到新目录 |
| `check_database_delivery.py` | 对照本机私有数据库、备份和维护基线 |
| `analyze_missing_references.py`、`analyze_missing_reference_origin.py` | 审计未解析来源，不伪造缺失身份 |
| `public_data/` | 收集、构建和校验公开参考资料 |
| `attachments/` | 附件盘点、OCR 服务和批次校验 |
| `data/` | 本机邮件归档、脱敏候选、身份修复和文件导出 |
| `audits/`、`evaluation/` | 本机资料审计与可选人工复核 |
| `training/`、`experimental/` | 可选训练支持与隔离实验 |

包内可复用功能位于 `pcb_assistant/`，使用 `python -m pcb_assistant.<子包>.<模块>` 启动。审计和数据工具的本机来源默认值不能直接用于其他数据集；报告写入 `.private/`。不要把真实来源文本、报告或本机数据加入 Git。

```powershell
python -m scripts.catalog_backup --help
python -m scripts.attachments.validate_attachments --help
python -m scripts.data.organize_mail_archive --help
python -m scripts.audits.verify_ocr_supplement --help
```
