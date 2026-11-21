# 内部使用中的反馈与纠错

- 日期：2026-10-29
- 入口：交互式 `chat` 中的 `/feedback`
- 台账：`.private/internal_pilot_feedback/feedback.jsonl`
- 定位：正常使用流程的一部分，不是使用前审批

## 设计原则

内部辅助工具不要求工程师在开始使用前逐条签署五题复核或 120 条严重结论复核。
正确做法是让工程师在真实排查中发现问题时立即记录，再按需回溯整理。反馈台账只
保存纠错线索，不自动改写本轮回答，不修改 Catalog，也不自动进入训练数据。

## 使用方式

先完成至少一轮问答，再对上一轮记录反馈：

```text
/feedback 结论不准确，缺少板边/板中位置和影响批次
/feedback wrong_citation EXC_... 该引用上下文不支持这个结论
/feedback wrong_retrieval 检索应优先匹配真空度曲线而不是槽宽
/feedback missing_context 还需要 PP 规格、叠构和压合程序
/feedback status
```

支持的类型为 `wrong_citation`、`wrong_retrieval`、`wrong_conclusion`、
`missing_context` 和 `other`。类型和引用 ID 都可省略；未指定引用时，记录上一轮
回答的全部引用 ID，未指定类型时记为 `other`。

反馈记录后的管理命令：

```text
/feedback list                  # 最近 20 条开放反馈，默认不显示正文
/feedback list resolved         # 已关闭反馈
/feedback list all              # 全部反馈
/feedback show FBK_...          # 查看单条详情，包含反馈正文
/feedback resolve FBK_... 已补充分层边界回归
```

`list` 只返回 ID、时间、类型、状态、指纹前缀和引用 ID，不回显正文；`show` 才
显示正文和完整记录。`resolve` 在同一把本地锁内原子更新台账，写入关闭时间和可选
处理说明；已关闭记录再次执行会返回 `already_resolved`，不会重复改写。

## 记录内容

每条记录包含反馈 ID、时间、类型、正文、上一轮 `result_fingerprint`、引用 ID、
回答模式和证据状态，初始状态为 `open`。关闭后增加 `resolved_at` 和可选的
`resolution`。记录与上一轮回答指纹绑定，便于之后对照原始问答、引用和检索结果
复查。

`/feedback status` 只返回总数、开放数、类型分布和最近时间，不回显反馈正文或本地
路径。反馈正文只保存在本机私有目录，默认被 `.gitignore` 排除。台账存在损坏行时，
列表和状态会报告 `invalid_records`；关闭操作会拒绝重写，避免静默丢失原始记录。

## 边界

- 反馈是人工判断，不会因为写入台账就自动成为事实、gold 或训练标签。
- 本轮回答和会话归档不会被反馈改写；需要纠正现场结论时，应重新提问或由工程师
  直接采用正确判断。
- 不发送内部邮件、附件原文或未脱敏证据到外部服务。
- 反馈数量、五题复核完成度或反馈关闭数量都不能替代正式发布审计；`release_ready`
  仍按 `pcb_assistant/evaluation/release.py` 的正式口径计算。
- 五题复核和严重结论复核保留为可选回溯审计，用于整理质量声明，不作为日常使用
  的前置门槛。可选工具见[开发说明](DEVELOPMENT.md)。
