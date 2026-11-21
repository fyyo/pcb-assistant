# 内部试用、质量边界与反馈

助手用于本地资料检索和工程排查辅助，不是工程放行或准确率认证。引用可追溯不代表来源正确；OCR 数值、讨论中的参数、候选原因和材料适用范围仍需相应证据。软件测试通过也不会自动把结论升级为已核验工艺参数。

内部人员可以在正常使用中发现并记录问题，无需先完成专家评审。需要作正式质量承诺或启用新的外部能力时，再完成对应评测。`pcb_assistant.evaluation.release` 保留正式发布条件；未满足条件时不得伪造标签、复核记录或性能结果。

## 反馈

在 `knowledge_catalog.py ... chat` 中至少完成一轮问答后，使用 `/feedback` 记录对上一轮的纠正线索：

```text
/feedback 结论不准确，缺少板边/板中位置和影响批次
/feedback wrong_citation EXC_... 该引用不支持结论
/feedback wrong_retrieval 检索应匹配真空度曲线
/feedback missing_context 还需 PP 规格和压合程序
/feedback status
```

类型可为 `wrong_citation`、`wrong_retrieval`、`wrong_conclusion`、`missing_context` 或 `other`。也可用 `/feedback list`、`/feedback show FBK_...` 和 `/feedback resolve FBK_... 处理说明` 查看或关闭记录。

台账保存在 `.private/internal_pilot_feedback/feedback.jsonl`。默认列表只显示摘要；查看单条才显示反馈正文。记录绑定上一轮结果指纹，损坏时拒绝重写。反馈不会改写回答或 Catalog，也不会自动成为事实、评测 gold 或训练标签；原始邮件、附件和未脱敏证据不会随反馈发往外部服务。
