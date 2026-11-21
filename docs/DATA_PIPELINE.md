# 本机资料处理

原始 PST、附件、邮件归档、脱敏候选、Catalog、备份和训练集是不同资产。进入检索库不等于获准外发或训练。只处理自己有权限使用的来源；新批次和新 Catalog 使用新目录，保留原始输入，不覆盖已核验产物。

## 邮件与附件

处理 PST 前，核对来源路径、`privacy_policy.json`、`attachment_policy.json`，安装 `requirements/requirements.txt` 与 `requirements/requirements-attachments.txt` 所需依赖。PST 读取需要本机可用的 libpff；部分旧 Office 格式需要额外解析工具。

```powershell
python -m pcb_assistant.ingestion.mail --help
python -m pcb_assistant.ingestion.attachments --help
python -m pcb_assistant.ingestion.attachments <自有PST路径> --run-name attachments_new --workers 4
python -m scripts.attachments.validate_attachments --run data/attachments_new
```

先用小批次核对环境和输出，再处理完整来源。恢复未完成的批次才使用 `--resume`；`--no-ocr` 可只提取原生文字。支持 PDF、常见图片、Office、文本和有界压缩包成员。表格保留工作表、行列位置及静态或缓存值，不执行宏或公式；解析失败、资源上限、低置信度 OCR 和不支持格式都保留处理状态。

附件 `documents.jsonl` 记录独立附件，`occurrences.jsonl` 记录它与父邮件的每次关联，`engineering_candidates.jsonl` 保存脱敏工程文本。附件按内容去重且父邮件关系保留；文档数、出现次数和文本单元数口径不同。

完整邮件归档由 `scripts.data.organize_mail_archive` 维护，只供本机查询；脱敏派生工具是 `scripts.data.derive_mail_candidates`。这些流程有本机来源依赖，运行前查看各模块 `--help`，不要照搬旧批次输入路径。原始 PST、附件缓存、候选正文、联系人索引和身份键留在 `data/` 或 `.private/`。

## 隐私与 OCR

`pcb_assistant.ingestion.mail` 与 `pcb_assistant.ingestion.attachments` 执行本机初次清洗；`pcb_assistant.privacy.candidates` 和 `scripts.data.rewash_candidate_identities`、`scripts.data.rewash_case_identities` 用于修复已有脱敏候选。失败或保留校验不通过时应停止处理，输出目录用新路径。

图片盘点、OCR 和增量来源生成由 `pcb_assistant.ingestion.ocr` 完成；`pcb_assistant.ingestion.images` 负责图像枚举与去重，`pcb_assistant.privacy.ocr` 清理 OCR 文本。

```powershell
python -m pcb_assistant.ingestion.ocr inventory --help
python -m pcb_assistant.ingestion.ocr run --help
python -m scripts.audits.verify_ocr_supplement --help
```

OCR 需要相应 PST、归档、清洗版附件、身份键和 OCR 依赖。导出哈希只能说明处理流程保留了识别内容，不能证明识别数字与原图一致；低置信结果、小图、矢量及解码失败都应保留为覆盖缺口。来源文件与文本单元校验通过，也不等于参数已独立核验。

## Catalog 与公开资料

```powershell
python -X utf8 -B knowledge_catalog.py build --help
python -X utf8 -B knowledge_catalog.py ingest --help
python -X utf8 -B knowledge_catalog.py --catalog data/my_catalog validate
```

只将完成清洗并核对来源身份的资料构建或增量导入到新 Catalog。父邮件缺失时保留 `unresolved`，不伪造引用；用 `scripts.analyze_missing_references` 和 `scripts.analyze_missing_reference_origin` 审计来源身份。公开资料文件导出不会自动获得再分发或云端传输权限；本地公开参考来源的处理见[诊断与公开资料](DIAGNOSIS.md)。

数据库备份与恢复见[数据库维护](DATABASE_OPERATIONS.md)。训练数据另有授权、隔离和留出集要求，见[可选训练](TRAINING.md)。
