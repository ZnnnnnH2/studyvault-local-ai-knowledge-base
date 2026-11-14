# StudyVault

面向个人学习的 AI 对话资料库：把 Agent 聊天记录存入 SQLite，支持课程归类、检索阅读、消息批注、复习卡、统计与导出。

## 运行环境

- Python 3.11 或更新版本、[uv](https://docs.astral.sh/uv/)。
- 默认只在 `127.0.0.1` 运行，不需要云服务或大模型 API。
- 恢复附带数据不需要原机器的 Agent 文件、旧中间产物或 zstd。

## 方式一：恢复附带的合成演示数据并启动（推荐演示路径）

仓库附带的是**人工编写的合成学习问答**，不含任何真实对话，仅用于演示和评测。在实际克隆目录执行：

```powershell
uv sync
uv run python app.py --restore-bundled
uv run uvicorn app:app --host 127.0.0.1 --port 8765
```

浏览器打开 [StudyVault](http://127.0.0.1:8765)。恢复命令会自动初始化数据库，恢复 14 个会话、56 条消息、14 条会话标注、19 条标签关联和 14 条知识点关联，并打印实际数量、完整性和外键检查结果。成功退出码为 0，失败为 1。

数据库默认写入项目目录的 `studyvault.db`，该文件被 Git 忽略。可在运行命令前通过 `$env:STUDYVAULT_DB = 'D:\your-path\studyvault.db'` 指定位置；恢复与启动必须使用相同设置。

重复恢复不会重复插入会话或消息。已存在且消息一致的会话只补缺失摘要、标签/知识点关系和会话级标注；已有非空摘要、标注、消息批注、复习进度保持不变。消息内容、顺序或时间冲突时整批恢复回滚，报错退出，不清空数据库。坏 JSON、重复标识、消息数量不符或标注无法一一对应时，也会明确失败。空库才会得到上述精确总数；已有其他记录时输出的是合并后的数据库总量。

快照未保存课程字段，恢复时使用标题与前十条消息关键词规则重建课程归属。标签、知识点和会话标注按快照保留。想用自己真实的对话，请改用方式二扫描本机 Agent 记录（真实数据只留在本机，不进版本库，详见 [DATA_POLICY.md](DATA_POLICY.md)）。

## 方式二：扫描当前机器的 Agent 记录

```powershell
uv run python app.py --init
uv run uvicorn app:app --host 127.0.0.1 --port 8765
```

页面点击“扫描本机 AI 数据”，或先执行：

```powershell
uv run python app.py --import-local
```

两种数据来源也可共存，但不是替代关系：扫描当前机器不会读取附带快照，也不会生成历史深读标注。已有来源会话 ID 会跳过，当前未实现同一会话后续消息的增量同步。

| 来源 | 默认扫描位置 | 可覆盖的环境变量 |
| --- | --- | --- |
| Codex | `%USERPROFILE%\.codex` 下的 `sessions`、`archived_sessions` | `CODEX_DATA_ROOT` |
| Claude Code | `%USERPROFILE%\.claude` 下的 `projects`、`transcripts` | `CLAUDE_DATA_ROOT` |
| Pi | `%USERPROFILE%\.pi\agent\sessions` | `PI_DATA_ROOT` |
| DeepSeek | `%USERPROFILE%\.dsh\sessions` | `DEEPSEEK_DATA_ROOT` |
| Gemini CLI | `%USERPROFILE%\.gemini\tmp` 下的 `chats` | `GEMINI_DATA_ROOT` |

解析 `.jsonl.zst` / `.jsonl.zstd` 时，需要 `zstd` 命令可在 PATH 找到。可先用 `Get-Command zstd` 和 `zstd --version` 检查。没有 zstd 时，可先恢复附带的普通 JSONL，或扫描未压缩记录。解析器仅保留可读取的用户/助手正文，执行确定性凭据脱敏，并跳过没有正文的工具事件文件。

## 页面演示

1. 总览核对会话与消息数量。
2. 会话库检索关键词、选择课程/来源，使用上一页/下一页浏览，每页 20 条。
3. 打开会话，添加笔记、疑问、纠错或重点批注。
4. 在消息下点击“加入复习”，填写问题，编辑预填答案，创建后立即进入复习队列。
5. 答对后按连续答对次数安排 3、7、14 天间隔；答错后次日复习，连续答对计数归零。重复建卡不会清除进度。
6. 查看 SQL 实时统计；导出当前库 JSON，或按课程筛选后导出 Markdown。

“下载历史会话快照”和“下载历史标注快照”是随仓库附带的固定归档，不包含本次运行新加的消息批注或复习卡。当前库 JSON 导出包含会话与消息；课程 Markdown 导出包含课程中的问答正文，二者都不是完整数据库备份。

### 日常使用功能

- 复习先显示问题，点击“显示答案”或空格揭晓后再评分；`1` 答错、`2` 答对，输入表单时不会截获快捷键。
- “学习待办”汇总疑问、纠错和重点/待练习，支持解决、重开和原消息定位。
- 正文支持 Markdown、公式、表格和代码复制，前端依赖本地提供；原消息按原文保存。
- “复习卡管理”可编辑题目答案、暂停/恢复和调整到期时间，已有复习记录保持不变。
- 会话可收藏，支持只看收藏及总览最近阅读；筛选、页码、会话和消息位置会保存到 SQLite。
- “导入与备份”可预览上传的 Agent JSON/JSONL、选择入库、查看进度和停止后续文件；每批最多 20 个文件、100 MB。
- 同页提供完整 SQLite 备份下载和恢复。恢复先校验预览，勾选确认后应用，自动保留恢复前备份；备份上传最多 200 MB。完整备份包含批注、人工整理、复习历史、收藏和设置。

完整库恢复适用于本地单进程服务，沿用上面的启动命令，不额外配置多个 Uvicorn worker。预览和后台任务在服务进程内运行，重启后需重新预览；已成功入库的数据保留。恢复前备份位于数据库旁 `backups/`，页面提供下载入口。

## 验证

```powershell
uv run --group dev python -m unittest discover -s tests -v
uv run --group dev python tests/browser_check.py
uv run --group dev python tests/additional_browser_check.py
uv run --group dev python tests/usability_browser_check.py
```

浏览器检查默认使用本机 Microsoft Edge，无需额外下载 Playwright Chromium；没有 Edge 的机器需要先安装 Edge。测试使用临时数据库和受控文件，不扫描个人 Agent 目录。

重新拍摄报告截图：

```powershell
uv run --group dev python tests/browser_check.py --capture-report
```

该命令先执行页面回归，再在临时库恢复全部附带数据、完成真实批注与复习演示，更新 `docs/screenshots/` 和 `docs/IMPLEMENTATION_EVIDENCE.json`；不会把演示批注写入日常使用的数据库。

## 文档与数据库设计

- [项目报告](docs/PROJECT_REPORT.md)：关系设计、逐功能输入输出、SQL、代码位置、真实截图及 AI 协作取舍。
- [独立 DDL](schema.sql)：初始化程序读取的唯一建表定义。
- [整改验收](docs/IMPLEMENTATION_VERIFICATION.md)：测试结果和与原核查的对应关系。
- [附加功能与相关研究](docs/ADDITIONAL_FEATURES.md)：消息定位、人工整理、逐次复习历史，论文/项目来源及待扩展方向。
- [七项可用性增强](docs/USABILITY_FEATURES.md)：复习揭晓、待办、备份、排版、卡片管理、收藏与阅读状态、导入预览和进度。
- [实施计划](docs/USABILITY_PLAN.md)：本轮实施顺序及验收条件。
- `docs/HW2_REQUIREMENTS_REVIEW.md` 及两份 `HW2_*EVIDENCE.json` 保留整改前基线，不能作为当前失败状态判断。
- `staging/structure_and_sync.py` 为历史处理脚本，依赖未附带的旧输入；标准恢复请使用 `--restore-bundled`。

## 学习闭环接口

- 会话库支持按知识点、批注状态（未解决/已解决）和复习状态（到期/已有卡片/尚无卡片）筛选；`GET /api/conversations` 与 `GET /api/search` 共用同一组筛选条件，`/api/search` 还会结合 SQLite FTS5（`message_fts`）返回匹配原因。
- 自动规则识别出的知识点关系以 `PROPOSED` 状态进入 `conversation_point`，并在 `evidence` 中记录命中的关键词；用户可在会话详情中确认或拒绝（`POST /api/conversations/{id}/points/{point_id}`），也可手动添加（`POST /api/conversations/{id}/points`）。
- 消息批注支持编辑、删除，疑问/纠错/重点可在会话详情或“学习待办”中标记解决和重新打开（`PATCH /api/annotations/{id}`）；普通笔记不属于待办。
- `PATCH /api/conversations/{id}` 可人工修订课程、标题、摘要和标签，修订记录写入 `audit_log`，重复恢复附带数据时不会覆盖人工修订。
- 数据统计页的“知识漏洞与复习优先级”按知识点汇总未解决批注（×3）、逾期复习卡（×2）和 `review_log` 中的答错次数（×2）。
- `POST /api/import/scan` 的每个结果带 `result_code`，区分 `SUCCESS`、`SKIPPED_EMPTY`、`SKIPPED_DUPLICATE` 和 `FAILED_PARSE`，失败原因写入 `import_job`。

## 数据边界

数据保存在本机 SQLite。扫描仅面向配置的 Agent 会话文件，不扫描浏览器 Cookie、密码或网页缓存。SQLite 使用外键、唯一约束和检查约束；原始消息与后续学习批注分表保存。项目使用固定本地演示账户，不提供多用户登录和权限系统。详细说明见 [DATA_POLICY.md](DATA_POLICY.md)。
