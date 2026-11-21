# 上传到 GitHub

本仓库按源码项目整理。原始邮件、数据库、模型、备份、身份键、本机配置与历史报告保留在本机忽略目录中。逐阶段设计、交接和旧试点指南也已归档；仓库只保留当前使用、维护和开发说明。

已删除一次性的 `finalize_candidate_contacts.py`、`restore_candidate_codes.py`、`finish_ocr_candidate.py`、`rewash_historical_identities.py`，以及绑定旧资产快照的 `scripts/verify_phase3_assets.py` 和对应测试。当前隐私清洗、工程代码保留、OCR、增量入库与备份能力继续保留；本机历史文档和报告已移入 `.private/project_records/`。

整理后的源码包位于本机 `.private/github_prepare_20261116/pcb-source.zip`，包含源码、合成案例和文档，可解压后作为独立源码目录使用；不会将私有资料带到新目录。

Python 工具按用途归入 `scripts/data/`、`scripts/audits/`、`scripts/evaluation/` 和 `scripts/experimental/`，运行方式见[脚本目录](../scripts/README.md)。已退役无业务调用的旧 `catalog_diagnostics.py`、`catalog_snapshot_cache.py` 及其专属测试；原文件保存在本机 `.private/python_cleanup_20261117/original/`。

## 本地提交

当前整理包含删除文件和新文件。原先暂存的内容可能仍是旧版本，提交前需要重新暂存：

```powershell
git add -A
git diff --cached --stat
git status --short
```

确认提交清单没有 `.private/`、`data/`、`models/`、`.aws/`、`.codex/`、`.agents/`、`.env`、邮件原档或数据库备份，再执行：

```powershell
git commit -m "Prepare PCB knowledge assistant source repository"
```

`.gitignore` 不会自动移除已经被 Git 跟踪的私有文件。以后若误跟踪了此类文件，需要先从索引移除；已提交或上传的凭据还需要撤销和清理历史。

## 连接仓库

在 GitHub 创建空仓库后，用实际仓库地址替换下方占位符：

```powershell
git branch -M main
git remote add origin <你的GitHub仓库地址>
git push -u origin main
```

已配置 `origin` 时先用 `git remote -v` 核对，不要重复添加。若用 GitHub Desktop，选择本项目目录，核对变更后提交并发布即可。

不要将整个工作目录压缩后上传；Git 的忽略规则只作用于 Git 文件选择，不会从任意 ZIP 或网页手动上传中删除本机私有资料。
