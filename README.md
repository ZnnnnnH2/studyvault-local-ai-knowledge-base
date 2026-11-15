# StudyVault：AI 对话错题本

学生跟 AI 来回问得最多的地方，往往就是自己没懂的地方。StudyVault 把散在 ChatGPT、DeepSeek 和各种 Agent 工具里的学习对话导入本地 SQLite，让你在读对话时随手记下**没弄懂的疑问**和**AI 答错的地方**，再把它们和**复习时答错过的卡**一起收进“错题本”，按知识点排出先后，复习到连续答对为止。

《数据库系统概论》课程大作业（参考选题 2“个人学习空间”）。FastAPI + SQLite，默认只在 `127.0.0.1` 运行。

## 运行

需要 Python 3.11 或更新版本和 [uv](https://docs.astral.sh/uv/)。

```powershell
uv sync
uv run python app.py --demo
uv run uvicorn app:app --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765>，首页就是错题本。`--demo` 做两件事：

1. 恢复仓库附带的 90 段合成学习对话（4 门课、6 个来源平台、一学期的时间跨度），等同于 `--restore-bundled`。
2. 在其上补一学期的模拟学习记录：疑问、AI 答错的纠错、重点、复习卡和每次复习的结果、收藏和阅读进度。复习历史经由真实的调度函数重放，并整体平移到今天，所以复习队列里有当天到期的卡。

只想要对话、不要模拟记录时用 `--restore-bundled`。这些数据全部是合成的，生成和核查方式见 [DATA_POLICY.md](DATA_POLICY.md)。数据库默认写在项目目录的 `studyvault.db`（被 Git 忽略），可用环境变量 `STUDYVAULT_DB` 指定别处。

### 导入自己的对话

- ChatGPT 网页版：在“设置 → 数据控制 → 导出数据”拿到压缩包，在“导入”页上传其中的 `conversations.json`，预览后逐条勾选入库；也可以运行 `uv run python app.py --import-file conversations.json`。程序沿 `current_node` 只取你最后看到的那条分支，被编辑掉的旧提问、工具调用和代码执行结果不入库。
- Agent 工具：点右上角“扫描本机”或运行 `uv run python app.py --import-local`，扫描下表目录；也可以在“导入”页上传单个文件。

| 来源 | 默认扫描位置 | 可覆盖的环境变量 |
| --- | --- | --- |
| Codex | `%USERPROFILE%\.codex` 下的 `sessions`、`archived_sessions` | `CODEX_DATA_ROOT` |
| Claude Code | `%USERPROFILE%\.claude` 下的 `projects`、`transcripts` | `CLAUDE_DATA_ROOT` |
| Pi | `%USERPROFILE%\.pi\agent\sessions` | `PI_DATA_ROOT` |
| DeepSeek | `%USERPROFILE%\.dsh\sessions` | `DEEPSEEK_DATA_ROOT` |
| Gemini CLI | `%USERPROFILE%\.gemini\tmp` 下的 `chats` | `GEMINI_DATA_ROOT` |

`.jsonl.zst` 需要 PATH 里有 `zstd`。导入时做确定性的凭据脱敏，同一来源会话再次导入会跳过，同一会话后续新增的消息目前不会增量同步。

### 大模型（可选）

问答页（Text2SQL）和“AI 建议知识点”会调用大模型。在项目根目录建 `.env.local`，写一行 `DEEPSEEK_API_KEY=...`（已被 Git 忽略）。没配时两者都退回本地规则：问答用内置的 SQL 模板，知识点建议用关键词规则。Text2SQL 只发表结构和问题；AI 建议知识点会发送会话的标题、摘要和前约 3000 字，只在你点按钮时运行。

## 页面

- 错题本：三种错按知识点分组，组内优先级 = 疑问 ×3 + AI 答错 ×2 + 不稳的卡 ×2 + 逾期 ×1。疑问和 AI 答错可以标“弄懂了”、一键出复习卡、跳回原消息；答错过的卡连续答对 3 次才算攻克。
- 会话库：关键词、课程、来源、知识点、批注和复习状态筛选，后端分页；键盘 `j/k` 移动、`o` 打开、`f` 收藏、`/` 搜索。正文渲染 Markdown、公式和代码。每条消息下可以记疑问、AI 答错、重点或笔记，可以出复习卡；知识点可确认、拒绝，或让 AI 提建议。
- 复习：先看问题，空格揭晓，`1` 答错、`2` 答对。调度是 SM-2：答错次日再来并把这张卡的难度系数降 0.2，所以常错的卡以后间隔也长得慢。另有逐次复习记录和卡片管理。
- 总览、统计：数量、课程/来源/知识点分布、每周新记下和弄懂的错及复习答对率。
- 问答：自然语言转 SQL，只读执行。默认用 C3 方法（只给相关表、纠偏提示、采样 5 条投票），可切换到基线对比。
- 导入：上传预览、后台导入进度、完整 SQLite 备份与恢复。

## 验证

```powershell
uv run --group dev python -m unittest discover -s tests -v
uv run --group dev python tests/browser_check.py
uv run --group dev python tests/additional_browser_check.py
uv run --group dev python tests/usability_browser_check.py
```

单元测试和浏览器检查都用临时数据库，不读个人目录，不调用大模型（相关测试用替身）。浏览器检查默认使用本机 Microsoft Edge。`tests/browser_check.py --capture-report` 会在临时库里跑 `--demo` 并重拍 `docs/screenshots/`。

Text2SQL 评测会真实调用模型、消耗 token：

```powershell
uv run python eval/run_text2sql_eval.py
```

它在临时目录构建与 `--demo` 相同的库，对 40 道题分别跑基线和 C3，结果写入 `eval/text2sql_eval_result.md`。

## 文档

- [项目报告](docs/PROJECT_REPORT.md)：立意、E-R 图与关系模式、逐功能的输入输出/设计/实现位置/效果、关键 SQL、截图、评测和限制。
- [schema.sql](schema.sql)：唯一的建表定义，`init_db()` 直接执行它。
- [七项可用性增强](docs/USABILITY_FEATURES.md)、[附加功能与相关研究](docs/ADDITIONAL_FEATURES.md)：报告的附录。
- [docs/archive/](docs/archive/)：开发过程中的核查、计划和验收记录。
- [DATA_POLICY.md](DATA_POLICY.md)：什么数据留在本地、什么会发给大模型、演示数据怎么来的。
