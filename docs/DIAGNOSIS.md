# 压合诊断与公开资料

`agent/` 为白斑、分层、缺胶、空洞和翘曲等现象组织候选原因、来源摘录和待补信息。候选只是排查线索，需要结合现场曲线、切片、测量与复现验证。

```powershell
python -m agent.cli diagnose --question "压合后板边出现白斑，怎么排查？" --catalog data/my_catalog
python -m agent.cli diagnose --input cases/white_spot_001.json --catalog data/my_catalog
python -m agent.cli evaluate --cases cases/agent_eval_smoke.jsonl --validate-only
python -m agent.cli evaluate --cases cases/agent_eval_smoke.jsonl --catalog data/my_catalog --check-determinism --output .private/evaluation.json
```

结构化病例可提供材料、叠构、位置、设备与工艺条件；缺少具体现象时先追问。`evaluate` 可以不提供专家 `expected` 标签，仍检查执行状态、引用完整性、候选分布、耗时与重复运行指纹；无独立标签时准确率保持 `n/a`。长任务可加 `--background`，再用 `agent.cli jobs list/status/result/stop` 管理。真实问题和回答保留在本机私有目录。

`cases/white_spot_001.json` 保留一个结构化输入示例；各类缺陷评测集中在 `cases/agent_eval_smoke.jsonl`，无需为每类现象单独维护重复的 JSON 示例。`cases/internal_pilot.json` 是可选试用复核工具的合成默认输入。

## 本地公开知识包

本地知识包可独立于邮件 Catalog 使用已有公开资料归档；构建和校验不会自动授权原文再分发。

```powershell
python -m agent.cli public-pack build --json
python -m agent.cli public-pack verify --pack data/agent_public_pack_v1.json --json
python -m agent.cli diagnose --question "压合后出现空洞，怎么排查？" --public-pack data/agent_public_pack_v1.json
```

`--public-pack` 使本轮只使用指定知识包，不读取 Catalog。包保留来源、版本、许可说明、原文件与正文校验值、页码或章节定位和稳定引用；不做语义重排，也不据此判断不同材料体系可互相套用。包文件不随源码分发。

公开来源分级说明来源类型，不证明当前版本、因果关系或现场适用性。已确认的 CFRP 修补研究只作为背景，排除出 PCB 缺陷的原因评分与直接引用，原资料仍可查询。

## 可选 SearXNG 搜索

外部搜索默认关闭。需已有支持 JSON 的 SearXNG 服务，并在单次诊断显式启用；服务端不负责安装或托管搜索服务。

```powershell
python -m agent.cli diagnose --question "压合后出现空洞，怎么排查？" --external-search searxng --searxng-url http://127.0.0.1:8888 --json
$env:PCB_AGENT_SEARXNG_URL = 'http://127.0.0.1:8888'
python -m agent.cli diagnose --question "压合后出现空洞，怎么排查？" --external-search searxng --search-timeout 5
```

只发送受控的 PCB 缺陷与原因词，不发送原始问题、批次、材料、叠构、设备或参数。请求有界超时、响应大小限制，不自动重试、不跟随重定向、不抓取网页；外部结果仍标为未核验来源，不写入 Catalog 或训练数据。公开搜索与托管模型的数据范围见[数据处理与隐私](DATA_PIPELINE.md)。

接口客户端仍只调用 `pcb_answer` MCP 工具，见[MCP 接入与 Catalog 选择](UNIFIED_PCB_ASSISTANT.md)。
