# 邮件与附件处理

处理自有 PST 前，安装基础清洗依赖和附件依赖，并核对 `privacy_policy.json`、`attachment_policy.json` 与来源路径。PST 读取需要本机可用的 libpff，部分旧 Office 格式需要额外解析工具。

```powershell
python -X utf8 -B sanitize_pst.py --help
python -X utf8 -B sanitize_attachments.py --help
python -X utf8 -B sanitize_attachments.py <自有PST路径> --run-name attachments_new --workers 4
python -m scripts.attachments.validate_attachments --run data/attachments_new
```

新批次使用新运行名，先小规模验证环境与处理结果；恢复未完成任务才使用 `--resume`，不要覆盖已经验收的来源。`--no-ocr` 可只提取原生文字。其他缓存和重试选项以 `--help` 为准。

支持 PDF、常见图片、Office、文本和有界压缩包成员处理。表格保留工作表、行列位置及静态／缓存值，不运行宏或公式。解析失败、资源上限、低置信度 OCR 和不支持格式保留处理状态，不能当成完整提取。

输出中的 `documents.jsonl` 记录独立附件，`occurrences.jsonl` 记录附件与父邮件的每次关联，`engineering_candidates.jsonl` 保存脱敏工程单元。重复附件按内容去重，父邮件关系保留；文档数、出现次数和文本单元数是不同口径。

原始 PST、提取缓存、候选文本和身份键只留本机。导入 Catalog 使用已完成且校验通过的脱敏来源，新版本构建后再执行完整性核对。参见[数据流程](docs/DATA_PIPELINE.md)、[隐私处理](docs/PRIVACY.md)、[OCR](docs/OCR.md)。
