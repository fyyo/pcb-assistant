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
| 缺陷排查 | 围绕白斑、分层、缺胶、空洞、翘曲，整理可能原因、检查方向和待补信息 | 压合后板边出现白斑，先核对什么？ |
| 材料与叠构查询 | 查 PP、树脂含量、介厚等；按工作表、行列、区块定位原文；支持连续追问 | 这份叠构表右侧第 12 行的介厚有没有单位？ |
| 材料术语解释 | Tg／Td／RF／GT 等别名解析、定义与适用条件，缺条件时单独列出未核实的子问题 | Tg 和 Td 在这份材料表里分别指什么？ |
| 回答溯源 | 每段结论给出源摘录与引用；材料、单位、版本对不上时标明证据缺口 | — |
| 资料整理 | 本机邮件脱敏、附件解析、CPU OCR、增量入库、备份与恢复 | — |

支持命令行单次问答、连续对话，也可以通过 MCP 接进 Agent，用同一套检索与诊断逻辑。

## 快速开始

已有本地知识库时，装核心依赖即可查询。下面以 Windows PowerShell 为例，Linux 把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

```powershell
git clone https://github.com/fyyo/pcb-assistant.git
cd pcb-assistant
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements/requirements-core.txt

# 把 data/my_catalog 换成你自己的知识库目录
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog chat "压合后板边出现白斑，先核对什么？" --model off
```

`--model off` 使用确定性的证据回退，不依赖模型权重，适合先验证链路是否通。

还没有知识库？从自己的资料建一个，步骤见[资料处理](docs/DATA_PIPELINE.md)。**本仓库只提供源码，不含邮件、附件、数据库或模型。**

## 接入 Agent

任何支持本地 stdio MCP 的客户端都可以启动这个服务，注册后调用唯一工具 `pcb_answer`：

```powershell
.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --catalog data/my_catalog
```

问题、补充信息和最近几轮对话都走同一个入口，内部自动完成检索、诊断或必要的追问，返回回答、引用和待补信息。客户端配置、多轮历史和返回结构见 [MCP 接入](docs/UNIFIED_PCB_ASSISTANT.md)；只支持远程 HTTP 的平台需要另加网关。

默认只读本地资料，公开搜索和托管模型需要显式配置。

## 设计要点

- **单入口**：对外只暴露 `pcb_answer`，调用方不需要先判断该走知识查询还是缺陷诊断。
- **只读查询**：普通查询使用只读连接，来源正文视为不可信数据，不能改配置或触发指令。
- **本地优先**：默认无外部请求；外发能力是显式开关，且有边界检查。
- **可评测**：150 条合成用例，配合引用完整性、敏感外发、提示注入三类门禁；CI 在 Windows／Linux × Python 3.11／3.12 上运行。

## 边界

回答用于资料查询与排查辅助。来源讨论、OCR 数字和候选原因需要进一步核对，**不能作为已批准的生产参数**。

本地评测尚未建立专家 gold，因此不对外声明准确率或发布就绪结论；[评测报告](docs/PHASE6_3_LOCAL_EVAL_20261123.json)按原样保留其未达项。遇到资料缺失、单位不明或版本不一致时，系统会列证据缺口而不是给结论。

## 目录结构

```
pcb_assistant/   catalog（构建、检索、证据、对话）、ingestion、privacy、interfaces、evaluation、training
agent/           压合诊断：病例事实、候选原因、本地证据
scripts/         数据处理、核验与人工复核工具
contracts/       接口 schema、工具映射与合成评测骨架
config/          运行配置；可选训练配置在 config/training/
docs/            使用与设计文档
tests/           合成测试与 PDF 夹具
```

根目录的 `knowledge_catalog.py` 和 `phase6_mcp.py` 是两个薄启动入口，实现分别在 `pcb_assistant.catalog.cli` 与 `pcb_assistant.interfaces.mcp`。更多分层说明见[架构](docs/ARCHITECTURE.md)。

## 文档

| 需要做什么 | 看这里 |
| --- | --- |
| 准备邮件、附件或 OCR 资料 | [资料处理](docs/DATA_PIPELINE.md) |
| 诊断流程、公开知识包与可选搜索 | [诊断指南](docs/DIAGNOSIS.md) |
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