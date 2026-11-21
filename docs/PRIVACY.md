# 本地隐私处理与候选修复

原始邮件和附件仅在本机处理。`sanitize_pst.py`、`sanitize_attachments.py` 负责初次清洗；`candidate_privacy.py`、`scripts/data/rewash_candidate_identities.py`、`scripts/data/rewash_case_identities.py` 用于已脱敏候选的修复。

修复保留已有占位符、原始换行、工艺数字、单位、工程代码与来源关系；候选回读发现残留或保留校验失败时中止。案例修复同步维护引用片段的偏移。具体参数使用相应脚本的 `--help` 查看，输出目录应为新目录。

原始正文、联系人索引、身份键、候选文本和详细失败记录保存于 `data/` 或 `.private/`。上传源码时只包含合成测试，不包含这些记录。

联系人和字段规则的通过结果不能证明完全匿名，也不代表工程字段已核验。公开参考资料的作者署名与工艺代码按其用途保留。

历史修复中用于特定旧版本的代码恢复、联系人收尾和固定批次重洗脚本已退役；相关记录保存在本机 `.private/project_records/`，后续候选使用当前通用修复模块。
