# 笔记与知识点的直接关联

## 1. 解决的问题

原结构中，消息批注只关联原消息，与知识点之间的关系是间接的：

```text
批注 → 原消息 → 所属会话 → conversation_point → 知识点
```

当一个会话同时涉及“外键”和“事务”，而某条批注只讨论外键时，上述路径无法准确表达批注自身的主题。本轮增加直接关联：

```text
annotation ──< annotation_point >── knowledge_point
     │
     └── message（原消息仍保留，作为回看依据）

point_note >── knowledge_point（知识点页直接写的独立笔记）
```

- 一条消息批注可以关联多个知识点。
- 一个知识点可以汇总多条消息批注。
- 批注仍保留 `message_id`，汇总页可以回到原消息和原会话。
- 知识点页还可以写不依赖消息的独立笔记。

## 2. 数据模型

`schema.sql` 新增两张表：

| 表 | 字段 | 约束和用途 |
| --- | --- | --- |
| `annotation_point` | annotation_id、point_id、created_at | 复合 PK；FK 到 `annotation`、`knowledge_point`，两侧删除时级联；表达一条批注讨论哪些知识点。 |
| `point_note` | point_note_id、point_id、author_id、content、created_at、updated_at | PK；FK 到 `knowledge_point`、`app_user`；知识点页的独立笔记。 |

`annotation` 本身不改结构，`message_id` 仍为 NOT NULL，消息批注不会脱离原消息。独立笔记走 `point_note`，避免把两种来源混成同一张表。

索引：

- `idx_annotation_point_point(point_id, annotation_id)`：按知识点汇总批注。
- `idx_point_note_point(point_id, created_at DESC)`：按知识点读取独立笔记。

## 3. 接口

### 3.1 消息批注

`POST /api/annotations` 新增可选字段：

```json
{
  "message_id": 12,
  "annotation_type": "NOTE",
  "content": "外键删除行为要结合业务规则判断。",
  "point_ids": [3, 7]
}
```

- `point_ids` 可省略或为空，表示不关联知识点。
- 会去重；每个知识点必须属于该消息所属会话的课程，否则返回 400。
- 返回批注字段和 `points` 数组。

`PATCH /api/annotations/{annotation_id}` 新增 `point_ids`：

- 省略：只改内容/解决状态，不动关联。
- `[]`：清除全部关联。
- 非空：整体替换关联，仍校验课程。
- 返回 `points` 数组。

`GET /api/annotations` 和 `GET /api/conversations/{id}` 中，每条批注都带：

```json
"points": [{"point_id": 3, "point_name": "SQL 与查询"}]
```

### 3.2 知识点汇总页

`GET /api/knowledge-points/{point_id}/notes?limit=20&offset=0`

返回知识点信息、合并后的笔记列表和总数：

```json
{
  "point": {"point_id": 3, "point_name": "SQL 与查询", "course_id": 1, "course_name": "数据库系统概论"},
  "items": [
    {
      "source": "message",
      "id": 21,
      "annotation_type": "NOTE",
      "content": "只讨论外键……",
      "message_id": 12,
      "seq_no": 1,
      "conversation_id": 5,
      "title": "SQL 外键学习 04",
      "message_excerpt": "什么是外键？……",
      "created_at": "..."
    },
    {
      "source": "point",
      "id": 8,
      "content": "独立笔记……",
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "total": 2,
  "limit": 20,
  "offset": 0
}
```

消息批注只统计当前用户拥有的会话；独立笔记只统计当前作者。两种来源按 `datetime(created_at)` 倒序合并分页。

### 3.3 独立笔记

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/knowledge-points/{point_id}/notes` | 写一条独立笔记，body `{content}`。 |
| PATCH | `/api/point-notes/{note_id}` | 修改独立笔记内容。 |
| DELETE | `/api/point-notes/{note_id}` | 删除独立笔记。 |

内容要求非空，最多 4000 字符；修改保留 `created_at`，更新 `updated_at`。

### 3.4 知识点合并与改课

- `merge_points` 现在同时迁移 `annotation_point` 关联和 `point_note`：先 `INSERT OR IGNORE` 目标关联，再删除源关联，把独立笔记的 `point_id` 改成目标，最后删除源知识点。
- 会话改课时，除 `conversation_point` 外，也把该会话所有批注的知识点关联按名称迁到新课程，保留关联创建时间；不会留下跨课程链接。

## 4. 页面

### 4.1 消息批注

- 每条消息的批注表单增加“关联知识点（可多选）”，选项来自当前会话课程。
- 保存时把勾选的 `point_ids` 一并提交。
- 已保存批注下显示关联知识点；“调整关联知识点”可整体替换，清空即取消关联。
- 批注下和会话知识点 chip 中的知识点名称可点击，直接进入该知识点的汇总页。

### 4.2 知识点汇总页

入口：词库 → 知识点 → 行内“笔记 N”。

页面包含：

- 知识点名称、课程、笔记总数。
- 独立笔记输入框（不依赖消息）。
- 汇总列表：`消息笔记` 显示类型、原会话、消息序号、原消息摘要和“查看原消息”；`独立笔记` 支持就地编辑和删除。
- 20 条分页。

### 4.3 原有入口保持不变

消息批注仍可按类型筛选、标记解决/重新打开；笔记与知识点的关联不改变批注本身的内容、解决状态和原消息。

## 5. 实现位置

- 表与索引：`schema.sql`。
- 批注关联校验/写入：`app.py:validate_annotation_points`、`replace_annotation_points`、`create_annotation`、`update_annotation`、`conversation_detail`。
- 汇总与独立笔记：`app.py:knowledge_point_notes`、`create_point_note`、`update_point_note`、`delete_point_note`。
- 改课迁移：`app.py:curate_conversation`。
- 知识点合并迁移：`vocabulary.py:merge_points`。
- 页面：`static/app.js:pointChoiceList`、`annotationPointSummary`、`openConversation`；`static/vocabulary.js:openPointNotes`、`loadPointNotes`；`static/index.html` 的知识点汇总页；`static/style.css` 对应样式。

## 6. 验证

- 后端新增 10 项测试：
  - 一条批注关联多个知识点，并在详情/列表中回读；
  - 跨课程关联被拒绝；
  - 关联整体替换和清空；
  - 删除批注级联删除关联；
  - 消息笔记与独立笔记合并分页和数量；
  - 独立笔记编辑/删除与作者隔离；
  - 知识点合并迁移两类笔记；
  - 会话改课迁移批注关联；
  - 完整备份往返保留批注关联和独立笔记；
  - 旧备份缺少两张新表时仍可恢复。
- 当前 `uv run --group dev python -m unittest discover -s tests -q`：**84 项通过**。
- 真实 Edge 专项检查 `tests/annotation_points_browser_check.py` 通过，JavaScript 错误为 0；记录见 [ANNOTATION_POINTS_EVIDENCE.json](ANNOTATION_POINTS_EVIDENCE.json)。
- 截图：
  - [消息批注直接关联多个知识点](screenshots-annotation-points/01-note-point-link.png)
  - [知识点汇总页：消息笔记与独立笔记](screenshots-annotation-points/02-point-notes.png)
- 既有核心、附加、可用性、主题词库四套浏览器检查保持通过。

## 7. 边界

- 消息批注必须保留原消息，不提供“脱离消息的批注”；需要独立笔记时使用知识点的 `point_note`。
- 历史批注不会按会话级知识点自动补关联：会话涉及两个主题不代表每条旧批注都讨论两者；请在批注下按实际内容“调整关联知识点”。
- 跨课程关联由接口校验拒绝，数据库层没有触发器强制；应用只通过上述接口写入。
- 知识点合并是不可撤销的显式操作；旧名仍保留在别名表中，但批注关联会直接转到目标知识点。
- 独立笔记只有文字内容，不含附件、标签或复习卡；这些仍是后续扩展方向。
