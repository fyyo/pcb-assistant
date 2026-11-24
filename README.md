# PCB 压合助手

把邮件、附件和工艺资料整理为可查询的本地知识库，帮助 PCB 工程师查资料、排查压合缺陷，并追溯回答依据。可以在命令行使用，也可以通过 MCP 接入 Agent。

## 能做什么

- **缺陷排查**：围绕白斑、分层、缺胶、空洞和翘曲，整理可能原因、检查方向和待补信息。
- **材料与叠构查询**：查找 PP、树脂含量、介厚等资料，按工作表、行列和区块定位原文，支持连续追问。
- **回答溯源**：附来源摘录与引用；资料缺失或材料、单位、版本对应不明确时，列出证据缺口。
- **资料整理**：本机邮件脱敏、附件解析、OCR、增量入库与备份恢复。

例如可以问：“压合后板边出现白斑，先核对什么？”“这份叠构表右侧第 12 行的介厚有没有单位？”

回答用于资料查询与排查辅助。来源讨论、OCR 数字和候选原因需要进一步核对，不能作为已批准的生产参数。

## 开始使用

已有本地知识库时，安装核心依赖后即可查询。以下命令在 Windows PowerShell 中执行；Linux 使用 `.venv/bin/python` 替换 Python 路径。

```powershell
git clone https://github.com/fyyo/pcb-assistant.git
cd pcb-assistant
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements/requirements-core.txt

# 将 data/my_catalog 替换为你的知识库目录
.venv\Scripts\python.exe -X utf8 -B knowledge_catalog.py --catalog data/my_catalog chat "压合后板边出现白斑，先核对什么？" --model off
```

**本仓库提供源码，不附带邮件、附件、数据库或模型。**首次使用需要用自己的资料建立知识库，步骤见[资料处理](docs/DATA_PIPELINE.md)。完整附件、OCR 和测试环境使用 Python 3.11 或 3.12；核心检索无需训练模型或 GPU。

## 接入 Agent

支持本地 stdio MCP 的客户端可启动以下服务，注册后调用 `pcb_answer`：

```powershell
.venv\Scripts\python.exe -X utf8 -B phase6_mcp.py --catalog data/my_catalog
```

知识咨询、缺陷诊断和追问由服务内部处理。客户端配置、多轮历史和返回结构见[MCP 接入](docs/UNIFIED_PCB_ASSISTANT.md)。只支持远程 HTTP 的平台需要另加网关。

默认使用本地资料；公开搜索和托管模型需要单独配置。客户端如何使用返回的资料，取决于它所连接的模型和数据设置。

## 使用与开发

| 需要做什么 | 说明 |
| --- | --- |
| 准备邮件、附件或 OCR 资料 | [资料处理](docs/DATA_PIPELINE.md) |
| 诊断、公开知识包与可选搜索 | [诊断指南](docs/DIAGNOSIS.md) |
| 备份、恢复与切换数据库 | [数据库维护](docs/DATABASE_OPERATIONS.md) |
| 记录使用问题与反馈 | [反馈](docs/PILOT.md) |
| 了解代码、运行测试或贡献修改 | [架构](docs/ARCHITECTURE.md) · [开发](docs/DEVELOPMENT.md) · [测试](tests/README.md) |
| 可选模型训练 | [训练](docs/TRAINING.md) |

业务代码位于 `pcb_assistant/`，诊断代码位于 `agent/`，维护工具位于 `scripts/`。运行配置集中在 `config/`，可选训练配置放在 `config/training/`；日常查询无需调整训练配置。根目录两个 Python 文件分别是命令行和 MCP 的启动入口。

## 许可证

本项目以 [Apache-2.0](LICENSE) 发布，版权声明见 [NOTICE](NOTICE)。所用第三方组件及其许可证见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。仓库只包含源码，不含邮件、附件、数据库或模型。
