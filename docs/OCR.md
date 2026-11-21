# 附件图片 OCR

`supplement_attachment_ocr.py` 在本机盘点图片、运行 OCR 并生成可增量导入的来源。`ocr_image_sources.py` 负责图像枚举与去重，`ocr_candidate_privacy.py` 负责 OCR 文本隐私处理。

```powershell
python -X utf8 -B supplement_attachment_ocr.py inventory --help
python -X utf8 -B supplement_attachment_ocr.py run --help
python -X utf8 -B -m scripts.audits.verify_ocr_supplement --help
```

现有工作流需要相应 PST、归档、清洗版附件、身份键和 OCR 依赖，不能仅凭源码运行。新批次应检查来源配置和输出目录；恢复未完成任务使用 `--resume`，不得覆盖已完成产物。

OCR 导出保留识别数字，按置信度及隐私规则隔离不合格结果。导出哈希证明处理过程保留识别内容，不能证明数字与原图一致；小图、矢量和解码失败等情况需要保留覆盖缺口。

完成后的 `manifest.json` 可通过 `knowledge_catalog.py ingest` 导入新 Catalog。随后验证数据库并执行本地问答测试。一次性的旧版本 OCR 收尾脚本已删除，通用的盘点、处理和独立核验能力继续保留。

独立核验兼容原 `docs/` 下的基线文件，也可读取本机归档 `.private/project_records/docs/` 中的历史基线。历史基线只适用于对应的本机来源。
