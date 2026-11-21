# 可选持续学习

持续学习把自有案例整理、候选训练、新旧模型比较和模型指针更新组合成独立本地流程。日常证据问答不会因为一次对话就自动更新模型权重。

```powershell
python -X utf8 -B -m pcb_assistant.training.worker --help
python -X utf8 -B -m pcb_assistant.training.worker status
python -X utf8 -B -m pcb_assistant.training.worker submit --help
python -X utf8 -B -m pcb_assistant.training.worker once --help
python -X utf8 -B -m pcb_assistant.training.service --help
```

提交真实案例时区分观察输入、结论和验证依据；不把助手的候选原因当成现场事实。数据检查、分组划分、训练触发条件和升级阈值由 `continuous_policy.json` 及相关策略决定。

后台任务需要本机训练环境、模型与足够资源。候选必须完成训练、行为与数值检查、新旧模型比较及文件校验，才更新模型指针；失败时保留当前版本。运行前检查任务状态，避免同时启动重复训练。

记录、训练来源、模型、反馈与日志保持本机私有。升级门槛表示软件流程和已定义评测通过，不是工程准确率认证。更多说明见[训练](TRAINING.md)与[隐私处理](docs/PRIVACY.md)。
