# 2026-11-15 验收问题修复记录

对应验收报告：验收方本地 `docs/review-2026-11-15/ACCEPTANCE.md` 与 `recheck-01/REACCEPTANCE.md`（外部验收材料，未纳入本次提交）。本文件只记录本轮修复，不改写验收方的报告、截图或证据文件。

## R1 · P2：规则知识点改名/合并后，后续导入丢失关联

### 原因

`app.py:import_parsed()` 在命中 `POINT_RULES` 后，仍按规则中的旧名称精确查询：

```sql
SELECT point_id FROM knowledge_point WHERE course_id=? AND point_name=?
```

初始化、快照恢复和改课路径已经使用 `ensure_point()`，可以解析别名；普通导入漏掉了这一步。因此：

- “SQL 与查询”改名为“SQL基础”后，新会话命中 SQL 规则，但查不到旧名称；
- 把“SQL 与查询”合并到“SQL查询”后，同样查不到旧名称；
- 会话和消息虽然导入成功，`conversation_point` 却不产生规则关联。

### 修复

`import_parsed()` 现在对命中的规则点调用 `ensure_point(db, course_id, point_name)`：

```python
point_id = ensure_point(db, course[0], point_name)
db.execute(
    "INSERT OR IGNORE INTO conversation_point(...) VALUES(?,?,?,?,?)",
    (conversation_id, point_id, "rule", evidence, "PROPOSED"))
```

`ensure_point()` 的解析顺序是：

1. 课程内完全同名；
2. `knowledge_point_alias` 中的旧名；
3. 规范化后的同名（大小写、全半角、常见分隔符）；
4. 都没有时才创建。

因此改名后返回改名后的点，合并后返回目标点；规则候选仍以 `PROPOSED` 状态进入会话，等待用户确认或拒绝。标签的普通导入路径原本就使用支持别名的 `ensure_tag()`。

### 回归测试

新增 `tests/test_import_aliases.py`，直接走“API 改名/合并 + `import_file()` 导入 + 查询 `conversation_point`”：

- `test_import_after_rename_links_the_renamed_point`：改名为“SQL基础”后导入，关联到原 `point_id`，名称为“SQL基础”，状态为 `PROPOSED`，重复导入跳过。
- `test_import_after_merge_links_the_target_point`：合并到“SQL查询”后导入，关联到目标 `point_id`，名称为“SQL查询”，状态为 `PROPOSED`。

验证命令：

```powershell
uv run --group dev python -m unittest discover -s tests -p "test_import_aliases.py" -v
uv run --group dev python -m unittest discover -s tests -q
```

当前完整套件：**84 项通过**。

## dsh 压缩导入：补 Python `zstandard` 后备

验收报告同时记录：本机缺少 `zstd` 命令，实际 `.jsonl.zstd` 不能直接读取；用临时 Python 解压后的真实 dsh 文件可以入库，说明解析格式本身兼容。

本轮已补上 Python 依赖后备：

- `pyproject.toml` 增加 `zstandard>=0.23`，`uv sync` 会安装；
- `app.py:iter_records()` 的 `.zst` / `.zstd` 分支优先调用 `zstandard.ZstdDecompressor().stream_reader(..., read_across_frames=True)`，不再要求系统 PATH 中有 `zstd`；
- 只有 Python 依赖不可用时才回退到原有 `zstd` CLI 路径；两者都没有时返回明确的“读取压缩会话需要 zstandard 或 zstd”。

验证：

- 新增 `tests/test_zstd_import.py` 三项：压缩 Codex 会话可解析、入库、重复跳过；完全无效的帧头返回受控 `RuntimeError`；尾部截断、半文件和多帧末帧截断均返回受控错误，且不写入部分会话，随后用完整文件重试可正常入库 1000 条。
- 本机真实 dsh 目录只读探测：发现 25 个压缩会话文件；抽取其中 1 个直接解析得到 106 条消息，证明不再需要临时解压脚本。探测没有输出正文，也没有写入项目数据库。
- 当前完整套件 **84 项通过**。

README 的压缩读取说明已同步改为“优先 Python `zstandard`，CLI 仅后备”。

注意：验收脚本 `extra_acceptance.py` 的 `zstd_executable_available` 只检测系统 CLI，因此即使实际解析已经走 Python 后备，这个字段仍可能显示 `false`；复跑时应以 `actual_file_samples` 是否成功解析为准。

## R2 · P2：截断压缩文件被当作成功导入部分消息

### 原因

`zstandard` 的 `stream_reader` 把底层 EOF 当正常结束；当压缩帧只写入一部分时，它不会报错，已经解出的 JSON 行仍会交给 `parse_conversation`，随后被当作完整会话写入。更严重的是，第一次不完整导入已经占用了来源会话 ID，之后用完整文件重试会被 `SKIPPED_DUPLICATE` 挡住。

### 修复

新增 `zstd_decompressed_chunks()`，不再用 `stream_reader` 直接读到底：

- 用 `ZstdDecompressor().decompressobj()` 按块解压；
- 每帧结束后读取 `decompressobj.eof`，并处理 `unused_data` 以继续下一帧；
- 文件结束时若当前帧尚未 `eof`，抛出“压缩帧不完整或文件被截断”；
- 支持完整多帧；空文件或无完整帧同样报错；
- 解压仍按 128 KiB 块流式进行，不一次性加载整个压缩文件。

`zstd_text_lines()` 用增量 UTF-8 解码器把解压块转成行，避免行被块边界切断。错误发生在 `parse_conversation` 阶段，`import_file` / `import_parsed` 不会创建会话或消息。

### 回归

`tests/test_zstd_import.py` 的 1000 条消息合成 Codex 会话（压缩时启用校验和）：

| 输入 | 轮次 | 结果 |
| --- | --- | --- |
| 完整压缩文件 | 解析 | 1000 条 |
| 删除末尾 50 字节 | 解析 | `RuntimeError: 压缩帧不完整或文件被截断`，不写库 |
| 保留前一半 | 解析 | 同上 |
| 完整帧 + 截断第二帧 | 解析 | 同上，不写库 |
| 完整帧 + 完整帧 | 解析 | 2000 条（多帧正常） |
| 先失败，再用完整文件导入 | 导入 | 成功 1000 条，不被重复去重阻挡 |
| 完整文件重复导入 | 导入 | `SKIPPED_DUPLICATE` |

## 对应关系

| 验收项 | 本轮状态 |
| --- | --- |
| R1 规则知识点改名后导入 | 已修复，新增 2 项回归测试 |
| R1 合并后导入（同原因） | 已修复，新增回归测试 |
| R2 截断压缩文件部分导入 | 已修复：逐帧验证 `eof`；新增截断/半文件/多帧截断与完整重试测试 |
| 笔记与知识点直接关联 | 本轮未改，验收通过 |
| 标签/知识点选词与合并 | 本轮未改，验收通过 |
| dsh 压缩文件直接读取 | 已补 Python `zstandard` 后备；新增 3 项测试与真实文件只读探测 |
