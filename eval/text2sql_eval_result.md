# Text2SQL 执行准确率：baseline vs C3-SQL

- 数据库：合成评测库（演示数据 + 确定性补充） · 模型：`deepseek-chat` · C3 采样数：5 · 用例数：18
- 指标：execution accuracy（生成 SQL 的执行结果与标准 SQL 一致，忽略列名/行序/列序）
- 运行时间：2026-11-14 04:27，并发跑完共 6 秒；逐题 SQL 与 C3 诊断见 `eval/text2sql_eval_detail.jsonl`

| 方法 | 正确 | 准确率 | 平均耗时 |
| --- | --- | --- | --- |
| baseline | 15/18 | 83.3% | 2.2 秒/题 |
| c3 | 17/18 | 94.4% | 5.3 秒/题 |

## 逐题结果

| # | 问题 | baseline | C3 | C3 胜出票 / 可执行（分组） | C3 召回表 |
| --- | --- | --- | --- | --- | --- |
| 1 | 一共有多少个会话？ | ✅ | ✅ | 5/5（5） | conversation |
| 2 | 一共有多少条消息？ | ✅ | ✅ | 5/5（5） | message |
| 3 | 用户自己发了多少条消息？ | ✅ | ✅ | 5/5（5） | message, conversation |
| 4 | 助手（AI）回复了多少条消息？ | ✅ | ✅ | 5/5（5） | message |
| 5 | 每门课程各有多少个会话？ | ❌ | ✅ | 5/5（5） | course, conversation |
| 6 | 每个来源平台各有多少个会话？ | ✅ | ✅ | 5/5（5） | conversation, conversation_source |
| 7 | 还没解决的批注有多少条？ | ✅ | ✅ | 5/5（5） | annotation |
| 8 | 各类型的批注各有多少条？ | ✅ | ✅ | 5/5（5） | annotation |
| 9 | 一共有多少张复习卡？ | ✅ | ✅ | 4/4（4） | review_card |
| 10 | 复习答对和答错各有多少次？ | ❌ | ✅ | 5/5（5） | review_log |
| 11 | 收藏了多少个会话？ | ✅ | ✅ | 5/5（5） | conversation_state, conversation |
| 12 | 每门课程各有多少条消息？ | ❌ | ❌ | 5/5（5） | course, conversation, message |
| 13 | 系统里一共有多少个知识点？ | ✅ | ✅ | 5/5（5） | knowledge_point |
| 14 | 有多少个不同的标签？ | ✅ | ✅ | 5/5（5） | tag |
| 15 | 处于已确认（CONFIRMED）状态的知识点关联有多少条？ | ✅ | ✅ | 5/5（5） | conversation_point, knowledge_point |
| 16 | 标题里包含“外键”的会话有哪些标题？ | ✅ | ✅ | 5/5（5） | conversation |
| 17 | 每个来源平台各有多少条消息？ | ✅ | ✅ | 5/5（5） | message, conversation, conversation_source |
| 18 | 有多少条会话级的深读标注？ | ✅ | ✅ | 5/5（5） | conversation_annotation |
