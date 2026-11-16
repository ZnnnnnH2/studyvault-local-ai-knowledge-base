# 主题词库：已有词汇优先、近似词提示与合并

## 1. 这次补齐的范围

此前“主题”一词混用了三类信息，处理方式不同，也没有统一的已有词汇入口：

| 信息 | 来源 | 本轮处理 |
| --- | --- | --- |
| 标签（如 Python、待练习） | 正文 `#标签` 提取、人工编辑、演示快照 | 新增标签词库和会话内标签选择器；同名复用、近似词提示、重命名/合并。 |
| 知识点（如 数据库完整性） | 课程内的关系表；规则候选需人工确认/拒绝 | 新增课程内知识点建议、新建、重命名/合并；候选确认/拒绝保留。 |
| 深读标注主题 | 附带的 `manual_annotations_all.jsonl`，只读 JSON 字段 | 保持只读，但每个主题可带入标签编辑器，按标签规则选择或新建。 |

这样不再把“深读标注主题”当成可编辑词库，也不把标签和知识点混成同一张表；三者仍各自保留来源和语义。

## 2. 已确定的交互

### 2.1 会话详情里的标签/知识点

- 输入框聚焦时先显示已有词汇（按会话使用次数排序），输入后按“同名 → 相近 → 包含”排序显示。
- 完全同名（包括大小写、全角/半角、空格和常见分隔符差异）直接复用已有记录，不会新建。
- 如果只存在相近名称（例如已有“外键”，输入“外键约束”），服务端返回 409，界面显示已有候选和“仍然新建”按钮；只有用户再次确认后才写入新词。
- 知识点按会话所在课程检索，不能把别的课程的知识点加到本会话。
- 标签可逐个移除；知识点可新增、确认、拒绝或从本会话移除。词条本身不会被删除。
- 深读标注的“加入标签”只把主题带入标签输入框，随后仍按已有词汇优先规则处理，不直接改写只读标注。

### 2.2 主题词库页

- 顶部切换“标签 / 知识点”；知识点可按课程筛选。
- 列表显示使用次数；标签显示别名，知识点显示所属课程和别名。
- “重命名”把旧名写入别名表，之后恢复快照或再次导入旧名时仍解析到新词条。
- “合并…”把一个词条的会话关联转到目标词条，删除源词条，并把源名称保留为目标词条的别名。
- 新建时同样先检查同名和近似词，不会静默产生“外键”“外键约束”这类近义词。

### 2.3 编辑整理信息中的批量标签

原有“编辑整理信息”仍支持顿号/逗号分隔的批量标签，并增加：

- `datalist` 直接列出已有标签；
- 输入最后一段时提示同名或相近标签；
- “允许创建与已有标签相近的新标签”复选框，作为批量保存时的显式确认。

## 3. 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/tags?q=&limit=` | 已有标签、使用次数、别名、`relation`（exact/similar/contains/all）。 |
| POST | `/api/tags` | 新建标签；`{tag_name, allow_similar}`；同名返回已有项，近似词返回 409。 |
| PATCH | `/api/tags/{tag_id}` | 重命名；旧名写入 `tag_alias`。 |
| POST | `/api/tags/{target_id}/merge` | 合并标签；body `{source_tag_id}`。 |
| GET | `/api/knowledge-points/suggest?course_id=&q=&limit=` | 课程内知识点建议。 |
| POST | `/api/knowledge-points` | 新建知识点；`{course_id, point_name, allow_similar}`。 |
| PATCH | `/api/knowledge-points/{point_id}` | 重命名知识点；旧名写入 `knowledge_point_alias`。 |
| POST | `/api/knowledge-points/{target_id}/merge` | 合并同一课程的知识点；body `{source_point_id}`。 |
| POST | `/api/conversations/{id}/tags` | `{tag_id}` 选择已有标签，或 `{tag_name, allow_similar}` 新建；同名复用、近似词 409。 |
| DELETE | `/api/conversations/{id}/tags/{tag_id}` | 从本会话移除标签，不删除词条。 |
| POST | `/api/conversations/{id}/points` | `{point_id}` 选择已有知识点，或 `{point_name, allow_similar}` 新建/复用。 |
| DELETE | `/api/conversations/{id}/points/{point_id}` | 从本会话移除知识点，不删除词条。 |
| PATCH | `/api/conversations/{id}` | 批量整理仍接受 `tags`，并新增 `tags_allow_similar`。 |

409 响应中的 `detail` 是对象：

```json
{
  "code": "SIMILAR_EXISTS",
  "kind": "tag",
  "term": "外键约束",
  "message": "已有相近标签「外键」。请先选择已有项；如确需新建，请确认后再创建。",
  "suggestions": [{"tag_id": 8, "tag_name": "外键", "conversation_count": 6, "relation": "similar"}]
}
```

前端 `api()` 会保留这个对象，选择器据此渲染候选按钮和“仍然新建”，而不是只显示一句错误。

## 4. 数据与恢复

- 新增 `tag_alias(owner_id, alias_name, tag_id)`，唯一约束 `(owner_id, alias_name)`。
- 新增 `knowledge_point_alias(course_id, alias_name, point_id)`，唯一约束 `(course_id, alias_name)`。
- 重命名和合并写入别名；`ensure_tag` / `ensure_point` 在导入、快照恢复和种子数据补齐时先查别名，再查大小写/全半角规范化后的同名项。
- 因此重复恢复附带快照时，旧标签或旧知识点名称会解析到存活词条，不会重建已合并/改名的记录。
- 普通规则导入也通过 `ensure_point` 解析种子知识点名称：知识点改名或合并后，新导入仍关联到存活词条，不再按旧名称精确查询而丢失关联。修复记录见 [验收 R1 修复记录](archive/ACCEPTANCE_FIXES_2026-11-15.md)。
- 旧完整备份若没有这两张表仍可恢复：备份校验把它们视为可选表，恢复后由 `schema.sql` 创建空表。

## 5. 实现位置

- 词库匹配与路由：`vocabulary.py`（`normalize_term`、`term_relation`、`ensure_tag`、`ensure_point`、`create_or_get_tag`、`create_or_get_point`、`rename_tag`、`rename_point`、`merge_tags`、`merge_points`、`create_router`）。
- 会话内标签/知识点：`app.py:add_tag`、`add_point`、`remove_point`、`conversation_detail`、`curate_conversation`。
- 页面：`static/vocabulary.js`（词库页、选择器、近似冲突面板）；`static/app.js:openConversation`、`switchView`、`pointChip`。
- 样式：`static/style.css` 的“主题词库”与“会话详情中的词库编辑器”两段。

## 6. 验证

- 后端新增 11 项词库测试，覆盖已有词汇和使用次数、同名/大小写/全半角复用、近似词 409、显式新建、标签重命名/合并、知识点课程范围/合并、合并时确认状态优先、批量整理、别名跨恢复、旧备份无别名表；另新增 2 项导入别名回归（改名后导入、合并后导入）。
- 当前 `uv run --group dev python -m unittest discover -s tests -q` 为 84 项通过（含后续笔记关联、导入别名和压缩导入测试）。
- 真实 Edge 专项检查 `tests/vocabulary_browser_check.py` 通过，页面 JavaScript 错误为 0；记录见 [VOCABULARY_EVIDENCE.json](VOCABULARY_EVIDENCE.json)。
- 截图：
  - [标签词库：已有词、相近提示、合并后的别名](screenshots-vocabulary/01-vocab-tags.png)
  - [知识点词库](screenshots-vocabulary/02-vocab-points.png)
  - [会话详情：已有标签/知识点选择与深读主题](screenshots-vocabulary/03-conversation-picker.png)
  - [会话详情输入框聚焦时先列已有标签](screenshots-vocabulary/04-existing-tag-menu.png)
- 既有三套 Edge 回归（核心、附加、可用性）仍通过。

## 7. 边界

- 近似判断是确定性的字符串规范化、包含关系和编辑相似度，不调用大模型；它只用于提示，不会自动合并或自动改名。
- 合并是显式操作；本页不提供“撤销合并”，但旧名作为别名保留，仍可解析到目标词条。
- 深读标注本身没有改成关系表，仍按附带 JSON 只读展示；“加入标签”只是把主题带入标签选择流程。
- 课程识别、固定演示账户等既有边界不变。
