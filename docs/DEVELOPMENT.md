# 开发与验证

使用 Python 3.11 或更高版本，从项目根目录执行下列命令。Windows 可用 `.venv\Scripts\python.exe`，Linux 可用 `.venv/bin/python` 替换激活环境后的 `python`。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements/requirements-test.txt
python -m pip check
python -X utf8 -B -m unittest discover -v
python -X utf8 -B phase6_contract.py --validate
python -X utf8 -B phase6_eval.py --validate
python -X utf8 -B phase6_mapping.py
```

上面使用 PowerShell 激活命令；Linux 使用 `source .venv/bin/activate`。若本机禁止执行激活脚本，直接使用虚拟环境中 Python 的完整路径运行命令即可。

完整测试使用合成输入与临时 Catalog，见[测试目录](../tests/README.md)。真实本机审计或客户端测试只有条件满足时才执行。测试环境不要求 PST 解析器、语言模型、私有资料或训练权重；CPU OCR 随测试依赖安装。

## 自动检查

[CI 配置](../.github/workflows/ci.yml) 在推送、PR 或手动触发时运行，覆盖 Windows／Linux 与 Python 3.11／3.13。检查包括依赖一致性、契约、评测案例、工具映射和全量合成测试。CI 使用全新源码目录；私有数据不参与流程。

GitHub 仓库创建后，可将 CI 的四个矩阵检查设为主分支合并条件。工作流随源码提供，远端执行结果以上传后的 Actions 页面为准。

## 贡献约定

- 一个变更解决一个具体问题；从功能分支提交 PR，说明触发条件、行为变化和验证结果。
- Python 新增代码使用 4 空格缩进与 UTF-8，格式遵循根目录 `.editorconfig`；不将全库格式重写混入功能修改。
- 对数据库、隐私清洗、数值、引用或接口的行为修改，补充能复现问题的合成测试。修复后的测试应验证实际行为。
- 可复用维护工具放入 `scripts/` 对应目录，使用 `python -m scripts.<目录>.<模块>`；临时补丁和一次性调试产物放入忽略目录。
- 外部依赖写入对应清单；核心检索、测试、数据处理、GPU OCR 和训练保持各自安装入口。
- 真实邮件、附件、数据库、身份键、模型和验收明细仅保留本机；提交前检查 `git diff --cached --stat` 及实际暂存内容。

问题反馈提供复现命令、Python／系统版本和合成输入；避免粘贴真实来源正文。PR 使用仓库模板，填写测试结果及对现有数据的影响。

接口 schema、工具映射与合成评测骨架保存在 [contracts/phase6/](../contracts/phase6/README.md)。公开 MCP 只暴露 `pcb_answer`；其他工具定义服务于内部接口和兼容实现，不应照历史列表注册额外 MCP 工具。

有自有 Catalog 时运行 `phase6_client_trial.py --catalog <目录> --json` 检查真实 stdio 子进程。`phase6_codex_preflight.py` 是可选真实客户端检查；本机 stdio 通过不等于任意客户端或模型回合已通过。

可选流程仍在源码中保留：

| 工具 | 用途 |
| --- | --- |
| `phase6_annotation.py` | 独立标注 sidecar；候选不会自动成为 gold |
| `scripts/evaluation/internal_pilot_review.py` | 从合成默认案例或指定私有案例准备可选复核包 |
| `phase6_severe_review.py` | 严重无依据结论的可选审查 |
| `phase6_quality.py`、`phase6_release.py` | 本地评测与正式发布条件汇总 |
| `scripts/experimental/openviking_shadow.py` | 隔离的公开候选导出和映射，不进入默认回答链路 |

以上工具的输入要求和写入选项以各自 `--help` 为准。真实问题、答案、证据正文、标注和失败明细放入 `.private/`，不要加入源码或报告文档。

历史阶段设计、试点、性能计数和交接记录保存在本机 `.private/project_records/`。仓库文档描述当前工作流；修改接口时同步更新 schema、合成测试与使用示例。
