# 可选本地训练

本地检索与 MCP 无需训练。仅在自有数据获准用于训练且具备底座权重、磁盘和 GPU 环境时运行；训练数据、权重、日志、反馈与模型指针留在本机。

依赖见 `requirements/requirements-training.txt`，PyTorch/CUDA 需按机器配置。`config/training/model.json` 记录底座信息，`config/training/policy.json` 定义策略。

`config/training/benchmark.json` 是合成行为评测输入，不能混入训练集。配置中的本地训练开关保留当前流程行为，不替代对具体来源的使用授权；运行前核对自己的资料与环境。

```powershell
python -m pcb_assistant.training.local --help
python -m pcb_assistant.training.local --status
python -m pcb_assistant.training.local --source-run data/my_training_source
```

训练流程检查输入版本、准备数据、优化模型并评测。相同输入已有匹配的完成结果时可以复用。训练和评测必须按来源、附件及关联片段分组，防止同源内容跨入留出集；损失下降、合成测试通过和引用有效都不代表工程结论准确。

## 持续学习

持续学习把获准案例的整理、新旧模型比较和模型指针更新组合为独立后台流程。普通问答不会自动修改权重。提交案例时区分现场观察、结论和验证依据；不能把助手生成的候选原因当作现场事实。

```powershell
python -m pcb_assistant.training.worker --help
python -m pcb_assistant.training.worker status
python -m pcb_assistant.training.worker submit --help
python -m pcb_assistant.training.worker once --help
python -m pcb_assistant.training.service --help
```

策略由 `config/training/continuous.json` 控制。候选必须通过训练、行为与数值检查、新旧模型比较和文件校验后才更新指针；失败时保留当前版本。启动前检查任务状态，避免并行训练。软件升级门槛不构成工程准确率认证。

隐私处理、来源身份和训练授权见[数据处理与隐私](DATA_PIPELINE.md)。
