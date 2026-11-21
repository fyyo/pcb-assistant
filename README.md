# PCB 压合知识助手

面向 PCB 压合工作的本地知识检索与问答工具。支持缺陷排查、材料与叠构查询、表格字段追问，并为回答提供可追溯的来源摘录。

核心能力：

- **本地 Catalog**：SQLite 全文检索、文档去重、来源关系与版本管理。
- **证据问答**：引用校验、上下文追问、证据缺口和补充资料提示。
- **压合诊断**：白斑、分层、缺胶、空洞、翘曲等问题的辅助排查。
- **表格检索**：定位工作表、行列与区块，查询 PP、RC、介厚等字段。
- **统一 MCP**：通过 stdio 向客户端提供一个 `pcb_answer` 工具。
- **本地数据处理**：邮件脱敏、附件提取、OCR、增量入库和备份恢复。

回答用于资料检索与排查辅助；来源讨论、OCR 数值和缺少受控依据的材料字段会保留未核验状态。

## 安装

使用 Python 3.11 或更高版本，在项目根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements/requirements-core.txt
```

以上是使用已有 Catalog 进行本地检索与证据问答的最小依赖。邮件清洗、附件提取、OCR 和训练分别使用 [requirements/](requirements/README.md) 中的依赖集，按需安装；原始 PST 处理还需要可用的 `libpff`。

## 准备本地数据

**GitHub 仓库只分发源码、配置、合成测试与使用文档。**原始邮件、附件、数据库、模型、身份键、备份和本机验收记录不随源码上传；克隆仓库不会获得这些资料。

已有 Catalog 时，通过 `--catalog` 指向本机目录：

```powershell
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog validate
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog status
```

从自有资料构建时，先按[邮件与附件处理说明](ATTACHMENTS.md)准备脱敏来源，再查看 `knowledge_catalog.py build --help` 和 `ingest --help`。导入和增量处理使用新目录，保留原始来源。

本机默认目录在 `catalog_common.py` 中配置为 `data/phase3_catalog_v9`。其他机器可直接通过参数选库。

## 使用

```powershell
# 本地连续对话，关闭模型生成
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog chat --model off

# 单次问答
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog chat "压合后板边出现白斑，应该核对什么？" --model off

# 启动统一 stdio MCP
.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --catalog data/my_catalog

# 检查合成案例格式，无需私有数据库
.venv\Scripts\python.exe -X utf8 -B -m agent.cli evaluate --cases cases/agent_eval_smoke.jsonl --validate-only
```

默认问答使用本地证据。外部检索和托管模型为可选功能，需要显式配置。客户端若将 MCP 返回内容发送给外部模型，其数据处理范围应单独确认。

## 项目结构

| 位置 | 内容 |
| --- | --- |
| `knowledge_catalog.py` | Catalog CLI：构建、检索、证据、对话与增量操作 |
| `phase6_mcp.py`、`pcb_assistant.py` | 统一 MCP 与问答分流 |
| `catalog_*.py`、`pcb_*.py` | 数据库、检索、引用与表格能力 |
| `agent/` | 压合诊断、评测与后台任务 |
| `scripts/` | 备份、审计、公开资料与训练维护工具 |
| `cases/`、`contracts/` | 合成案例与接口契约 |
| `tests/` | 自动化测试 |
| `docs/` | 使用与技术说明 |
| `requirements/` | 按用途拆分的依赖清单 |

`data/`、`.private/`、`models/`、`.venv/`、`.tmp_work/` 和 `dist/` 属于本机内容，已被 Git 忽略。历史开发记录保存在本机 `.private/project_records/`。

## 测试与维护

```powershell
# 安装测试依赖，无需私有资料、PST 解析器或 GPU
.venv\Scripts\python.exe -m pip install -r requirements/requirements-test.txt
.venv\Scripts\python.exe -m pip check

# 全量测试；真实客户端测试默认不执行
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -v

# 不依赖私有资料的备份恢复测试
.venv\Scripts\python.exe -X utf8 -B -m unittest -v tests.test_catalog_backup tests.test_database_delivery
```

进一步阅读：[文档索引](docs/README.md)、[MCP 接入](docs/UNIFIED_PCB_ASSISTANT.md)、[数据库维护](docs/DATABASE_OPERATIONS.md)、[维护脚本](scripts/README.md)、[测试说明](tests/README.md)、[GitHub 上传](docs/GITHUB.md)。

GitHub Actions 配置在 Windows／Linux、Python 3.11／3.13 上运行依赖一致性、接口契约和合成回归检查。开发约定、贡献流程与运行命令见[开发说明](docs/DEVELOPMENT.md)。
