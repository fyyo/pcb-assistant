# PCB 压合助手

把本机的邮件、附件和工艺资料整理成可查询的本地知识库。查资料、排查压合缺陷时，每个结论都能追回到原文的具体位置。

![CI](https://github.com/fyyo/pcb-assistant/actions/workflows/ci.yml/badge.svg?branch=main)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)

## 为什么需要它

- 关键资料散落在邮件、附件和表格里，找一份叠构表要先想起是谁发的。
- 排查白斑、分层、缺胶、空洞、翘曲时，结论往往没有出处，事后无法复核。
- 通用大模型答得很像，但说不清这条结论来自哪份文件、哪一行、哪个字段。

这个项目把资料落到本机 Catalog，回答时附带原文摘录、字符偏移和引用；资料不足时列出证据缺口，而不是补全猜测。

## 能做什么

| 能力 | 说明 | 可以这样问 |
| --- | --- | --- |
| 缺陷排查 | 围绕白斑、分层、缺胶、空洞、翘曲，整理候选原因、正常观测排除项、检查方向和待补信息 | 8层板 IT-180A 压合后板边出现白斑，真空曲线正常，怎么排查？ |
| 材料与叠构查询 | 查 PP、树脂含量、介厚等；按工作表、行列、区块定位原文；支持连续追问 | 这份叠构表右侧第 12 行的介厚有没有单位？ |
| 材料术语解释 | Tg／Td／RF／GT 等别名解析、定义与适用条件，缺条件时单独列出未核实的子问题 | Tg 和 Td 在这份材料表里分别指什么？ |
| 多步受控 Agent | 可选经本地脱敏网关调用兼容 API，沿两跳关系链自动追查关联附件并强制核验引用与数字依据 | — |
| 本机 Web 工作台 | 零新增依赖网页工作台（持久化病例、点击 `EXC_` 看原文、导出 Markdown 排查草稿） | — |
| 资料整理 | 本机邮件脱敏、附件解析、CPU OCR、增量入库、备份与恢复 | — |

支持浏览器本机工作台、命令行单次问答与连续对话，也可以通过 stdio 或 Streamable HTTP MCP 接进外部 Agent 与数字员工平台。

## 快速开始

已有本地知识库时，装核心依赖即可查询。下面以 Windows PowerShell 为例，Linux 把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

```powershell
git clone https://github.com/fyyo/pcb-assistant.git
cd pcb-assistant

# 可选：一键创建 .venv 并安装核心中文分词（Windows 也可直接执行 start_workbench.bat --init-venv）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[core]"

# 环境与知识库自检（加 --probe-llm 可一并测试模型网关连通性）
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog doctor

# 方式一：Windows 直接双击 PCB压合工作台.vbs（无黑框独立桌面应用窗口），或运行 start_workbench.bat
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog web --desktop --reuse-running --auto-port

# 方式二：命令行确定性问答（--model off 不依赖任何模型权重）
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog chat "压合后板边出现白斑，先核对什么？" --model off
```

还没有知识库？从自己的资料建一个，步骤见[资料处理](docs/DATA_PIPELINE.md)。**本仓库只提供源码，不含邮件、附件、数据库或模型。**

## 接入 Agent 与数字员工

任何支持本地 stdio MCP 的客户端都可以启动这个服务（默认仅暴露唯一入口 `pcb_answer`，也可加 `--toolset agent` 开放全部 8 个只读工具）：

```powershell
.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --catalog data/my_catalog
.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --catalog data/my_catalog --toolset agent
```

需要接入只支持 HTTP 的平台（如 Dify、MaxKB、FastGPT、Open WebUI 或企业 IM 数字员工）时，`knowledge_catalog.py ... web` 同时在本机暴露 `POST /mcp`（MCP Streamable HTTP）与 `POST /api/answer`。配置、脱敏出境开关、多轮历史与返回结构见 [MCP 与工作台指南](docs/UNIFIED_PCB_ASSISTANT.md)。

默认只读本地资料，外部兼容 API 与公开搜索均需显式开启并通过本地脱敏与残留检查。

## 设计要点

- **统一契约与工具箱**：默认单入口 `pcb_answer` 自动分流检索、诊断与追问；高级 Agent 宿主可显式启用 8 个只读工具集。
- **只读与不可信隔离**：普通查询强制使用 SQLite 只读连接；来源正文视为不可信数据，不能改配置或触发指令。
- **本地优先与脱敏出境**：默认零外部请求；显式开启外部 API 时，所有出境摘录经本地 NER + 敏感词二次脱敏且零残留，返回结果强制校验 `EXC_` 引用白名单与数字依据，任一失败自动回退本地确定性回答。
- **可评测**：150 条合成评测骨架配合引用完整性、敏感外发、提示注入三类门禁；CI 在 Windows／Linux × Python 3.11／3.12 上运行。

## 边界

回答用于资料查询与排查辅助。来源讨论、OCR 数字和候选原因需要进一步核对，**不能作为已批准的生产参数**。

本地评测尚未建立专家 gold，因此不对外声明准确率或发布就绪结论（可通过 `python -m pcb_assistant.evaluation.quality` 与 `pcb_assistant.evaluation.release` 在本机核查）。遇到资料缺失、单位不明或版本不一致时，系统会列证据缺口而不是给结论。

## 目录结构

```text
pcb_assistant/   catalog（构建、检索、证据、对话）、ingestion、privacy、interfaces（MCP、脱敏网关、受控 Agent、Web 工作台）、evaluation、training
agent/           压合诊断：病例事实、正常观测排除、多跳附件追踪、候选原因评分与评测任务
scripts/         数据处理、备份交付核验与人工复核工具
contracts/       接口 schema、工具映射与合成评测骨架
config/          运行配置；可选训练配置在 config/training/
cases/           合成诊断与试用输入样例
docs/            使用与设计文档
tests/           合成测试与 PDF 夹具
```

根目录仅保留 `knowledge_catalog.py`、`phase6_mcp.py` 两个薄 Python 入口及 Windows 一键工作台脚本 `start_workbench.bat`。更多分层说明见[架构](docs/ARCHITECTURE.md)。

## 文档

| 需要做什么 | 看这里 |
| --- | --- |
| 准备邮件、附件或 OCR 资料 | [资料处理](docs/DATA_PIPELINE.md) |
| MCP、受控出境 Agent 与本机 Web 工作台 | [MCP 与工作台指南](docs/UNIFIED_PCB_ASSISTANT.md) |
| 诊断流程、矛盾排除、公开知识包与可选搜索 | [诊断指南](docs/DIAGNOSIS.md) |
| 备份、恢复与切换数据库 | [数据库维护](docs/DATABASE_OPERATIONS.md) |
| 安装依赖、运行测试、提交修改 | [依赖说明](requirements/README.md) · [开发](docs/DEVELOPMENT.md) · [测试](tests/README.md) |
| 接口契约与工具映射 | [contracts/phase6](contracts/phase6/README.md) |
| 可选本地模型训练 | [训练](docs/TRAINING.md) |
| 记录使用问题与反馈 | [反馈](docs/PILOT.md) |
| 报告安全问题 | [安全策略](SECURITY.md) |
| 参与贡献 | [贡献指南](CONTRIBUTING.md) |

## 许可与安全

以 [Apache-2.0](LICENSE) 发布，版权声明见 [NOTICE](NOTICE)，第三方组件与许可证见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。仓库只包含源码，不含邮件、附件、数据库或模型。

安全问题请按 [安全策略](SECURITY.md) 私下报告，不要开公开 issue。
