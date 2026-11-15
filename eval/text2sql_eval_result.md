# Text2SQL 执行准确率：baseline vs C3-SQL

- 数据库：演示学期库（90 段合成对话 + 模拟学习记录，同 app.py --demo） · 模型：`deepseek-chat` · C3 采样数：5 · 用例数：40
- 指标：execution accuracy（生成 SQL 的执行结果与标准 SQL 一致，忽略列名/行序/列序）
- 运行时间：2026-11-14 21:44，并发跑完共 11 秒；逐题 SQL 与 C3 诊断见 `eval/text2sql_eval_detail.jsonl`

| 方法 | 正确 | 准确率 | 平均耗时 |
| --- | --- | --- | --- |
| baseline | 36/40 | 90.0% | 2.7 秒/题 |
| c3 | 35/40 | 87.5% | 8.7 秒/题 |

### 分档

| 档 | 题数 | baseline | c3 |
| --- | --- | --- | --- |
| basic | 18 | 15/18（83%） | 16/18（89%） |
| learner | 22 | 21/22（95%） | 19/22（86%） |

## 逐题结果

| # | 档 | 问题 | baseline | C3 | C3 胜出票 / 可执行（分组） | C3 召回表 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | basic | 一共有多少个会话？ | ✅ | ✅ | 5/5（5） | conversation |
| 2 | basic | 一共有多少条消息？ | ✅ | ✅ | 5/5（5） | message |
| 3 | basic | 用户自己发了多少条消息？ | ✅ | ✅ | 5/5（5） | message, conversation |
| 4 | basic | 助手（AI）回复了多少条消息？ | ✅ | ✅ | 5/5（5） | message |
| 5 | basic | 每门课程各有多少个会话？ | ❌ | ✅ | 5/5（5） | course, conversation_point, conversation |
| 6 | basic | 每个来源平台各有多少个会话？ | ✅ | ✅ | 5/5（5） | conversation_source, conversation |
| 7 | basic | 还没解决的批注有多少条？ | ✅ | ✅ | 5/5（5） | annotation |
| 8 | basic | 各类型的批注各有多少条？ | ✅ | ✅ | 5/5（5） | annotation |
| 9 | basic | 一共有多少张复习卡？ | ✅ | ✅ | 5/5（5） | review_card |
| 10 | basic | 复习答对和答错各有多少次？ | ❌ | ❌ | 2/3（2 / 1） | review_log |
| 11 | basic | 收藏了多少个会话？ | ✅ | ✅ | 5/5（5） | conversation_state, conversation |
| 12 | basic | 每门课程各有多少条消息？ | ❌ | ❌ | 5/5（5） | course, knowledge_point, conversation_point, conversation, message |
| 13 | basic | 系统里一共有多少个知识点？ | ✅ | ✅ | 5/5（5） | knowledge_point |
| 14 | basic | 有多少个不同的标签？ | ✅ | ✅ | 5/5（5） | tag |
| 15 | basic | 处于已确认（CONFIRMED）状态的知识点关联有多少条？ | ✅ | ✅ | 2/2（2） | conversation_point |
| 16 | basic | 标题里包含“外键”的会话有哪些标题？ | ✅ | ✅ | 5/5（5） | conversation |
| 17 | basic | 每个来源平台各有多少条消息？ | ✅ | ✅ | 5/5（5） | message, conversation, conversation_source |
| 18 | basic | 有多少条会话级的深读标注？ | ✅ | ✅ | 5/5（5） | conversation_annotation |
| 19 | learner | 哪门课我还没弄懂的疑问最多？只给课程名。 | ✅ | ✅ | 5/5（5） | annotation, message, conversation, conversation_point, knowledge_point, course |
| 20 | learner | 每门课各有多少个还没弄懂的疑问？列出课程名和数量，没有疑问的课程不用列。 | ✅ | ✅ | 5/5（5） | course, knowledge_point, conversation_point, conversation, annotation |
| 21 | learner | 按会话关联的知识点（不算已拒绝的关联）统计，AI 答错批注最多的 3 个知识点及次数。 | ✅ | ✅ | 5/5（5） | annotation, message, conversation, conversation_point, knowledge_point |
| 22 | learner | 累计答错 2 次及以上的复习卡有哪些？列出问题。 | ✅ | ✅ | 5/5（5） | review_card |
| 23 | learner | 最近 7 天新记下的疑问批注有多少条？ | ✅ | ✅ | 5/5（5） | annotation |
| 24 | learner | 现在已经到期（含逾期）、状态正常的复习卡有多少张？ | ✅ | ✅ | 5/5（5） | review_card |
| 25 | learner | 逾期超过 3 天还没复习的卡有哪些？列出问题。 | ✅ | ✅ | 5/5（5） | review_card |
| 26 | learner | 哪些知识点有关联的会话（不算已拒绝的），但这些会话里一张复习卡都没有？列出知识点名。 | ❌ | ✅ | 3/4（3 / 1） | knowledge_point, conversation_point, conversation, review_card |
| 27 | learner | 每门课的复习正确率是多少？列出课程名和答对次数占全部复习次数的比例（0 到 1 的小数，保留两位），没复习过的课不列。 | ✅ | ❌ | — | — |
| 28 | learner | 我指出过 AI 答错的会话有哪些？列出会话标题。 | ✅ | ✅ | 5/5（5） | annotation, message, conversation |
| 29 | learner | 来自 ChatGPT 的会话里，有多少个会话带有疑问批注？ | ✅ | ✅ | 5/5（5） | conversation, conversation_source, message, annotation |
| 30 | learner | 收藏了、但里面还有没弄懂的疑问的会话，列出标题。 | ✅ | ✅ | 2/2（2） | conversation, conversation_state, annotation, message |
| 31 | learner | 哪个来源平台上 AI 被我纠错的次数最多？只给平台名。 | ✅ | ✅ | 3/3（3） | conversation_source, conversation, message, annotation |
| 32 | learner | 每个月各开始了多少个会话？列出月份（形如 2026-09）和数量。 | ✅ | ✅ | 5/5（5） | conversation |
| 33 | learner | 平均每个会话有多少条消息？保留一位小数。 | ✅ | ✅ | 5/5（5） | message, conversation |
| 34 | learner | 同一个会话里既有疑问批注、又有 AI 答错批注的会话有几个？ | ✅ | ✅ | 4/5（4 / 1） | annotation, message, conversation |
| 35 | learner | 难度系数 ease_factor 低于 2.5 的复习卡有几张？ | ✅ | ✅ | 5/5（5） | review_card |
| 36 | learner | 还在等我确认（PROPOSED）的会话-知识点关联有多少条？ | ✅ | ❌ | — | — |
| 37 | learner | 当前连续答对 3 次及以上的复习卡有多少张？ | ✅ | ✅ | 5/5（5） | review_card |
| 38 | learner | 有多少个会话我从来没打开读过？ | ✅ | ✅ | 4/5（4 / 1） | conversation, conversation_state |
| 39 | learner | 我发过的最长（字数最多）的一条消息在哪个会话里？只给会话标题。 | ✅ | ✅ | 5/5（5） | message, conversation |
| 40 | learner | 每门课各有多少张复习卡至少答错过一次？列出课程名和张数，没有的课不列。 | ✅ | ❌ | 3/4（3 / 1） | course, knowledge_point, conversation_point, conversation, review_card |
