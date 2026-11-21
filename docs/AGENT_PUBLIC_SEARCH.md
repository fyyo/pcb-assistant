# 压合诊断公开检索兜底

`agent.cli diagnose` 新增 SearXNG JSON 搜索适配器。默认 `--external-search off`；用户单次显式选择 `searxng` 后，工作流先检索本地 Catalog，某个候选原因没有匹配的陈述性本地摘录时，才向配置的搜索服务请求公开结果。该能力无需模型或专家标签。

## 配置与使用

需要已有的 SearXNG 实例支持 JSON 格式（服务端 `search.formats` 包含 `json`）。本项目不安装、启动或托管搜索服务。以下地址仅为已有本机实例的示例：

```powershell
.venv\Scripts\python.exe -X utf8 -B -m agent.cli diagnose --question "压合后出现空洞，怎么排查？" --external-search searxng --searxng-url http://127.0.0.1:8888 --json
```

也可通过环境变量配置地址，每轮仍需显式选择搜索：

```powershell
$env:PCB_AGENT_SEARXNG_URL = 'http://127.0.0.1:8888'
.venv\Scripts\python.exe -X utf8 -B -m agent.cli diagnose --question "压合后出现空洞，怎么排查？" --external-search searxng --search-timeout 5
```

`--searxng-url` 优先于环境变量，接受实例根地址、部署前缀或完整 `/search` 地址。地址不带用户名、密码、查询串或 fragment。未配置地址而选择 `searxng` 时返回 `searxng_url_required`；无效地址或超时配置在发送请求前拒绝。

Python 调用示例：

```python
from agent.web_search import SearXNGSearchAdapter
from agent.workflow import diagnose

result = diagnose(
    {"question": "压合后出现空洞，怎么排查？"},
    external_search=SearXNGSearchAdapter("http://127.0.0.1:8888", timeout=5),
    allow_external_search=True,
)
```

## 请求与结果

- 请求为 `GET /search?q=...&format=json`，只发送受控分类中的 PCB 压合、缺陷与原因词。原始问题、批次、材料、叠构、设备、参数和备注均不拼接到公开查询中。
- 每个候选原因最多请求一次；单次默认超时 5 秒，可设置为大于 0 且不超过 30 秒。响应最多读取 1 MiB，不自动重试或跟随重定向，也不抓取搜索结果网页。
- SearXNG 的 `results[].title/content/url/score` 经 `ExternalSearchAdapter` 规范化；输出带稳定的 `EXC_WEB_...`、URL、`provider=searxng`、`source_tier=C` 与 `evidence_status=unverified_external`。
- 查询超时、HTTP 错误、非 JSON 或错误结构等统一记录为 `external_search_failed`，保留其他候选的本地证据，文本回答提示公开请求失败。网页摘录仍需匹配原因词且为陈述性内容才进入候选证据。
- `retrieval.external_queries_sent` 记录尝试请求数，`provider_errors` 记录失败；`providers_used` 包含返回过结果的服务。启用时 `external_services=true`、`data_egress=defect_and_cause_terms_only` 表示允许该范围的外部请求，是否尝试请求以计数为准。
- 结果不写入 Catalog 或训练数据。`evaluate`、后台评测和 `agent.mcp` 继续仅用本地证据，没有隐式读取环境变量并开启搜索的路径。

`agent.providers.ExternalSearchAdapter` 保留为不联网的通用结果适配器；`agent.web_search.SearXNGSearchAdapter` 增加实际 HTTP 传输。Tavily / Firecrawl 尚未实现。

## 验证

```powershell
python -X utf8 -B -m unittest -v tests.test_agent_web_search tests.test_agent_workflow
```

测试使用合成输入；本机数据规模和历史验收结果不作为新环境的验证结论。
