# 数据库维护

Catalog、邮件归档、身份键和备份属于本机资料，不随 GitHub 源码分发。维护前停止向相应数据库写入。

## 完整性与选库

```powershell
python -X utf8 -B knowledge_catalog.py --catalog data/my_catalog validate
python -X utf8 -B phase6_mcp.py --catalog data/my_catalog
```

更新数据时另建 Catalog 目录，验证后使用新路径启动服务。回退时显式选择原目录；已经运行的进程需要重新启动才会采用新的默认选择。

## 备份与恢复

```powershell
# 输出文件必须尚不存在；归档和身份键按需包含
python -X utf8 -B scripts/catalog_backup.py create `
  --catalog data/my_catalog `
  --archive data/mail_archive_v1/mail_archive.sqlite3 `
  --identity-key .private/pseudonym.key `
  --output .private/database_backup_new.zip

python -X utf8 -B scripts/catalog_backup.py verify .private/database_backup_new.zip

# 仅恢复到新目录，拒绝覆盖原库
python -X utf8 -B scripts/catalog_backup.py restore .private/database_backup_new.zip `
  --destination .private/database_restore_new

python -X utf8 -B knowledge_catalog.py --catalog .private/database_restore_new/catalog validate
```

备份包含 Catalog 目录和显式选择的归档、身份键，不包含原始 PST、来源处理目录或应用代码。异盘副本需要单独保存和核对，不能用同盘备份替代。

## 已交付本机实例的基线检查

```powershell
python -X utf8 -B scripts/check_database_delivery.py
```

该命令默认读取本机 `.private/project_records/database_maintenance_baseline.json` 及既有私有备份，核对默认库、验收登记的代码与数据库哈希、ZIP 内容及当前文件。返回汇总 JSON，通过为退出码 0，缺失或变化为 1；不写数据库或自动刷新基线。也可用 `--delivery`、`--backup` 指定匹配的验收记录和备份。

新克隆的源码不含该实例的基线、数据库和备份，因此不能直接通过这项实例检查。源码重构也可能使旧代码哈希不匹配；应结合变更和测试另行验收，保留历史记录。

缺失来源保持未解析；OCR 数值、工程生效版本和材料定义只在取得对应依据后更新核验状态。
