# 贡献指南

## 开始之前

1. 用 Python 3.11 或 3.12 建立虚拟环境并安装测试依赖，命令见 [开发与验证](docs/DEVELOPMENT.md)。
2. 运行全部检查：

```powershell
.venv\Scripts\python.exe -X utf8 -B -m pip check
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -v
.venv\Scripts\python.exe -X utf8 -B -m pcb_assistant.interfaces.contract --validate
.venv\Scripts\python.exe -X utf8 -B -m pcb_assistant.evaluation.cases --validate
.venv\Scripts\python.exe -X utf8 -B -m pcb_assistant.interfaces.mapping
```

测试只使用合成输入与临时 Catalog，不读取私有资料。

## 提交约定

- 一个 PR 解决一个具体问题；从功能分支提交，说明触发条件、行为变化和验证结果。
- 行为、接口、数据流程的变化要补充可复现的合成测试，并同步更新文档与契约 schema。
- 新代码使用 4 空格缩进与 UTF-8、LF 换行，格式遵循 `.editorconfig`。
- 可复用工具放进 `scripts/` 对应目录，用 `python -m scripts.<目录>.<模块>` 调用；一次性产物留在忽略目录。
- 新增外部依赖时同步更新 `requirements/` 清单与 [第三方组件](THIRD_PARTY_LICENSES.md)。

## 不要提交的内容

真实邮件、附件、数据库、模型权重、身份键、本机配置、私有验收报告和任何真实资料原文都只保留在本地。提交前检查暂存区：

```powershell
git diff --cached --stat
```

`.gitignore` 无法挽回已经被跟踪的文件，历史中的泄露需要单独处理。

## 许可

本项目以 Apache-2.0 发布。除非另有说明，你提交的贡献按 [Apache-2.0](LICENSE) 授权（inbound = outbound）。

## 反馈问题

提供复现命令、Python 与系统版本、合成输入，并说明日志中已移除哪些私有内容。PR 使用仓库模板并填写测试结果。
