# 压合诊断与评测

`agent/` 为白斑、分层、缺胶、空洞、翘曲等压合现象组织候选原因、来源摘录和需要补充的信息。候选是排查线索，不能替代现场曲线、切片、测量和复现验证。

```powershell
python -X utf8 -B -m agent.cli diagnose --question "压合后板边出现白斑，怎么排查？" --catalog data/my_catalog
python -X utf8 -B -m agent.cli diagnose --input cases/white_spot_001.json --catalog data/my_catalog

# 只验证合成评测输入的格式，无需私有 Catalog
python -X utf8 -B -m agent.cli evaluate --cases cases/agent_eval_smoke.jsonl --validate-only

# 用自有 Catalog 执行重复运行检查，结果保存本机
python -X utf8 -B -m agent.cli evaluate --cases cases/agent_eval_smoke.jsonl --catalog data/my_catalog --check-determinism --output .private/evaluation.json
```

结构化输入可提供材料、叠构、位置、设备及已知工艺条件；缺少可检索现象时先追问。来源正文视作数据，不执行其中指令。

`evaluate` 支持不含 `expected` 的病例，报告执行状态、引用完整性、候选分布、耗时和重复运行指纹。缺少独立标签时准确率为 `n/a`，无需先做专家标注；`calibrate` 是可选数据整理工具。

长评测可加 `--background`，再通过 `agent.cli jobs list`、`jobs status <任务ID>`、`jobs result <任务ID>` 和 `jobs stop <任务ID>` 管理本地任务。真实输入及完整回答只保存在私有目录。

仅公开知识包模式使用 `--public-pack`，见[公开知识包](AGENT_PUBLIC_PACK.md)。公开搜索兜底默认关闭，需要显式配置，见[公开检索](AGENT_PUBLIC_SEARCH.md)。MCP 客户端统一调用 `pcb_answer`，无需另行注册诊断服务，见[MCP 接入](UNIFIED_PCB_ASSISTANT.md)。
