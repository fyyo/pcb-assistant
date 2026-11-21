# 维护脚本

日常使用从根目录 `knowledge_catalog.py` 和 `phase6_mcp.py` 进入；以下工具按需运行。

| 工具 | 用途 |
| --- | --- |
| `catalog_backup.py` | 创建、校验备份，恢复到新目录 |
| `check_database_delivery.py` | 核对本机已交付实例的历史基线，需要私有记录与备份 |
| `analyze_missing_references.py` | 审计缺失引用，生成本机报告 |
| `analyze_missing_reference_origin.py` | 按来源身份分析缺失父邮件，不读取邮件正文 |
| `evaluate_catalog_quality.py` | 相似案例推荐一致性评估 |
| `rewash_mail_names.py` | 对已有脱敏来源重新应用姓名清洗规则 |
| `compress_catalog_jsonl.py` | 将 Catalog 导出转换为确定性 gzip 并验证 |
| `public_data/` | 公开参考资料收集、提取、构建与校验 |
| `training/` | 可选模型下载、训练数据准备与评估 |
| `attachments/` | 附件盘点、OCR 服务与处理校验 |
| `data/` | 邮件归档、脱敏派生、候选及案例身份修复、Dify 文件导出 |
| `audits/` | 现有案例盘点、邮件语料审计、OCR 补充核验 |
| `evaluation/` | 可选的内部试用人工复核包 |
| `experimental/` | 可选 OpenViking 影子检索实验 |

```powershell
python -X utf8 -B scripts/catalog_backup.py --help
python -X utf8 -B scripts/analyze_missing_references.py --help
python -m scripts.attachments.validate_attachments --help
python -m scripts.data.organize_mail_archive --help
python -m scripts.data.derive_mail_candidates --help
python -m scripts.data.export_dify --help
python -m scripts.audits.verify_ocr_supplement --help
python -m scripts.evaluation.internal_pilot_review --help
python -m scripts.experimental.openviking_shadow --help
```

历史引用审计保留 `v1` 和 `v3` 来源配置，便于复现；新数据需要明确选择适用的来源和输出。报告保存到 `.private/`，或使用 `docs/` 下被 Git 忽略的日期命名文件，避免上传来源明细。来源分析可读取本机 `.private/project_records/` 中已归档的审计输入。

以上模块命令从项目根目录执行。`data/rewash_case_identities.py` 提供供脚本及测试调用的修复函数；`audits/audit_existing_cases.py` 运行时会生成本地盘点。运行会写入数据的工具前，先查看其参数并明确输入输出；它们不会随普通问答自动运行。

旧版固定资产计数核验脚本已经删除，当前数据库验证使用 `knowledge_catalog.py --catalog <目录> validate`。备份和恢复见[数据库维护](../docs/DATABASE_OPERATIONS.md)。
