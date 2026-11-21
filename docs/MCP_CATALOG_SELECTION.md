# MCP 指定本地 Catalog

使用启动参数选择整个服务进程的数据库：

```powershell
python -X utf8 -B phase6_mcp.py --catalog data/my_catalog
python -X utf8 -B phase6_mcp.py --catalog data/my_catalog --self-test
python -X utf8 -B -m pcb_assistant.evaluation.stdio_trial --catalog data/my_catalog --json
```

问答、诊断、统计、引用、文档及关联附件使用同一目录，响应版本从所选库读取。路径缺失、schema 错误或版本登记无效时启动失败，不创建空数据库，不回退到其他库。提问参数不能覆盖启动时选库。

省略参数时使用 `pcb_assistant/catalog/common.py` 的默认路径；本机交付实例为 `data/phase3_catalog_v9`，源码不含该数据库，其他机器应明确选择自有目录。回退时停止服务，用保留的旧目录重新启动。

选库不会启用外部检索或托管模型。兼容入口 `python -m agent.mcp` 支持同样参数，只公开 `pcb_answer`，无需注册第二个服务。备份与恢复见[数据库维护](DATABASE_OPERATIONS.md)。
