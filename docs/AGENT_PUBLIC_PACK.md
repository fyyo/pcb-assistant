# 本地公开知识包与来源分级

从已有 `data/public_lamination_v1/` 与 `data/public_lamination_v2/` 资料构建独立的本地参考包，无需完整 Catalog、邮件库、模型或搜索服务。默认输出为 `data/agent_public_pack_v1.json`。

构建时读取自有公开归档，并保留来源、版本、权利及数据划分说明。评测专用来源继续隔离；包不随源码分发。

## 使用

```powershell
# 从已有公开归档构建；已有目标文件拒绝覆盖，可另选 --output
.venv\Scripts\python.exe -X utf8 -B -m agent.cli public-pack build --json

# 校验包结构、全文指纹和逐单元校验值
.venv\Scripts\python.exe -X utf8 -B -m agent.cli public-pack verify --pack data/agent_public_pack_v1.json --json

# 仅用公开知识包诊断，不读取 Catalog
.venv\Scripts\python.exe -X utf8 -B -m agent.cli diagnose --question "压合后出现空洞，怎么排查？" --public-pack data/agent_public_pack_v1.json

# 无标签评测，同样不读取 Catalog
.venv\Scripts\python.exe -X utf8 -B -m agent.cli evaluate --cases .private/agent_calibration/unlabeled.jsonl --public-pack data/agent_public_pack_v1.json --check-determinism --background
.venv\Scripts\python.exe -X utf8 -B -m agent.cli jobs list
```

`--public-pack` 表示本轮仅使用指定知识包，取代 `--catalog` 指定的资料来源；默认不设置时仍检索 Catalog。`diagnose` 可叠加显式的 SearXNG 开关，按原规则执行公开搜索兜底；`evaluate` 与后台评测不联网。`agent.mcp` 仍使用默认 Catalog，本轮没有增加 MCP 配置入口。

单个包文件可以复制到另一份已安装项目代码的本机目录，使用 `--public-pack <路径>` 加载，无需保留构建用的原归档路径。它只提供本地参考使用，原来源的版权与使用说明随包保留。

此模式按受控缺陷与原因词做确定性原文匹配，每个来源最多返回两条摘录；不做语义重排，也不据此判断用户材料牌号与文献体系相同。

## 可追溯内容

- 每个来源保留标题、原始 URL、出版方、类型、历史版本说明、原件 SHA-256、使用范围、原有数据划分及已知质量疑点。
- 每个正文单元保留原始提取文本、页码或章节定位、文本 SHA-256 与确定性 ID；不把章节序号当成 PDF 页码。
- 构建前核对归档原件和每个正文单元的 SHA-256，再生成包指纹；加载时校验包结构、指纹、正文哈希、来源引用和 ID，校验失败拒绝加载。
- 检索返回原始文本中的有界片段，`EXC_PUB_...` 引用保留 URL、页/章节、原文字符起止和片段哈希。定位与片段哈希针对包内原文切片；诊断展示文本会压缩空白，不能用展示文本替代原文做字节哈希。
- 诊断和评测记录绑定 `public_pack_fingerprint`。全文指纹用于检查一致性，不能证明来源真实性、当前版本适用性或工程结论正确。

## 来源分级

`agent/source_policy.py` 提供共享规则，独立知识包和 Catalog 适配器使用同一公开来源判定：

| 级别 | 依据 | 不代表的结论 |
|---|---|---|
| A | 来源类型为 `test_method`，出版方明确为 IPC/Global Electronics Association，URL 为 IPC 或 electronics.org 官方域名 | 不代表现行适用版本、满足客户规格或已完成现场验证 |
| B | 其他公开参考资料，如供应商加工指南、论文、课程、设备说明；元数据不足的公开资料也停留在 B | 不代表可跨材料体系直接移植工艺参数 |
| C | 未独立验证的邮件/其他来源，以及公开搜索摘录 | 不代表已经验证因果关系 |

既有案例与附件的规则继续使用原分级；明确独立验证标记的升级规则继续保留。标题或文件名含 `IPC`、`standard` 本身不再升为 A。Catalog 返回公开摘录时，适配器从只读元数据加载出版方、类型、原始 URL 和版本信息，再分级和附入引用。

已有的单位歧义、历史版本和非 PCB 研究适用范围说明随独立包保留。诊断文本会提示来源存在已知疑点，详细内容在结构化引用的 `provenance.quality_notes`；来源等级与因果证据强弱是两项不同信息，均不能代替现场根因验证。

## 诊断适用范围

诊断会在原因评分与引用整理之前，将明确研究 CFRP（碳纤维增强复合材料）修补的资料限制为背景。判定依据为已核对来源的文件 SHA-256，或公开资料标题／返回摘录中明确的 CFRP 修补语境；来源等级高或存在独立验证标记也不解除这一材料范围限制。Catalog、独立公开包和显式启用的外部搜索使用同一判定。

这类资料继续保留在原始归档、Catalog 和公开包中，可通过普通资料检索查阅；诊断不将它们加入 `citations`、候选原因的 `supporting_evidence` 或评分。实际排除的摘录 ID、文档、来源链接及判定依据记录在 `retrieval.excluded_background_evidence`，回答提示“仅作背景”。背景资料不阻止已显式启用的外部证据兜底。

该规则针对已核对来源和明确的复材修补语境，不是对全部研究的材料适用性审查。其他资料仍需核对牌号、试验对象、版本与工厂条件；玻纤、环氧等通用材料词本身不会触发排除。此规则不修改来源内容、公开包指纹或数据库。

## 验证

```powershell
python -X utf8 -B -m unittest -v tests.test_agent_public_pack tests.test_agent_workflow tests.test_agent_scope
```

测试使用合成输入；本机数据规模和历史验收结果不作为新环境的验证结论。
