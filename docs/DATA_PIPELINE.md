# 本机数据流程

原始 PST 与附件、完整邮件归档、脱敏候选、Catalog 和训练集是不同资产。进入检索库不等于已获准外发或训练。

1. 核对自有来源、策略与环境，用新运行目录提取和脱敏；附件处理见[处理说明](../ATTACHMENTS.md)。
2. 检查清单、来源身份、处理状态、覆盖缺口和文件校验值；正文与数值不通过核对时保留隔离或未核验状态。
3. 使用 `knowledge_catalog.py build` 或 `ingest` 在新目录建立版本，完整性通过后再显式选用。
4. 备份并验证恢复；保存原始来源、身份键和历史快照，见[数据库维护](DATABASE_OPERATIONS.md)。

```powershell
python -X utf8 -B knowledge_catalog.py build --help
python -X utf8 -B knowledge_catalog.py ingest --help
python -X utf8 -B knowledge_catalog.py --catalog data/my_catalog validate
```

缺少父邮件等来源时保留 `unresolved`；这与外键损坏不同，不能伪造目标补齐。用 `scripts/analyze_missing_references.py` 和 `analyze_missing_reference_origin.py` 进行精确身份审计，来源补齐后在新版本重新核对。

完整邮件归档由 `scripts/data/organize_mail_archive.py` 维护，保存正文、收发人、线程与附件出现信息，只供本机内部查询；脱敏派生入口为 `scripts/data/derive_mail_candidates.py`。这些脚本有本机来源和目录配置，新环境先查看 `--help` 与策略，不直接沿用旧批次默认值。

公开资料导出可使用 `scripts/data/export_dify.py`；该工具只准备本地文件，不自动获得原文再分发或云端传输权限。公开资料构建脚本生成的批次说明只留本机，公开知识包见[使用说明](AGENT_PUBLIC_PACK.md)。
