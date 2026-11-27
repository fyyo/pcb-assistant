# 依赖

按需从仓库根目录安装：

| 清单 | 用途 |
| --- | --- |
| `requirements-core.txt` | 已有 Catalog 检索、回答与 MCP |
| `requirements.txt` | 本机邮件与 PST 清洗 |
| `requirements-attachments.txt` | 附件解析和 CPU OCR |
| `requirements-test.txt` | 完整合成测试，包含附件与 OCR 依赖 |
| `requirements-agent.txt` | 可选 `PydanticAI` 受控 Agent 编排后端（默认 `native` 后端不依赖） |
| `requirements-training.txt` | 可选本地模型训练；PyTorch/CUDA 需匹配机器 |
| `requirements.lock.txt`、其他 `*.lock.txt` | 对应本机处理环境的版本记录 |

```powershell
python -m pip install -r requirements/requirements-core.txt
python -m pip install -r requirements/requirements-test.txt
python -m pip check
```

完整附件与测试环境使用 Python 3.11 或 3.12；当前 `rapidocr-onnxruntime` 1.4 后端要求 Python < 3.13。邮件处理还需 libpff 和相应 spaCy 语言模型；托管问答的隐私清洗同样需要本机隐私模型。核心检索不依赖训练权重、GPU OCR 或邮件来源。
