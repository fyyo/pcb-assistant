# 可选本地模型训练

本地证据检索和 MCP 不依赖训练。模型训练需要自有且允许训练的数据、底座权重、独立训练环境和足够的显存／磁盘，源码仓库不附带这些资产。

依赖见 `requirements/requirements-training.txt`；其中 PyTorch/CUDA 需按本机环境匹配。`base_model.json` 登记底座信息和文件校验值，`training_policy.json` 定义训练策略。

```powershell
python -X utf8 -B learn_local.py --help
python -X utf8 -B learn_local.py --status

# 明确选择已完成并获准训练的来源；会执行真实本地训练
python -X utf8 -B learn_local.py --source-run data/my_training_source
```

训练使用领域文本和问答目标进行本地适配，完成后校验权重、评测和模型指针。相同来源已有匹配完成结果时可复用；来源和输出配置以脚本帮助和本机策略为准。

同一来源、附件及关联片段应分组后划分训练与评测；留出集不能混入训练。预测损失下降、合成行为检查和引用有效不能替代独立工程验证。缺少训练权限的候选和 OCR 来源不得因为进入 Catalog 而自动获得训练权限。

新增案例和持续学习入口见[持续学习](CONTINUOUS_LEARNING.md)。训练权重、日志、问答数据和模型指针保存于本机忽略目录。
