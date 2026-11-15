# StudyVault 数据边界

StudyVault 把会话写进本地 SQLite，默认只在 `127.0.0.1` 提供服务。数据进来有两条路：扫描本机 AI 工具的会话目录（由 `app.py` 的 `SOURCE_ROOTS` 定义，可用 `*_DATA_ROOT` 环境变量覆盖），或由用户自己上传文件，包括 ChatGPT 网页版导出的 `conversations.json`。浏览器 Cookie、网页缓存、密码管理器和代理配置不在扫描范围内。

导入时会移除运行时上下文，并对 password、token、authorization、账号、`*_KEY` 以及 `sk-`/`tp-` 形式的凭据做确定性脱敏。ChatGPT 导出里的工具调用、代码执行结果和隐藏的系统消息不会入库。

## 什么会发给大模型

只有两个功能会联网，都需要在 `.env.local` 里配置 `DEEPSEEK_API_KEY`（或 `STUDYVAULT_LLM_*`），没配时都退回本地规则：

- 问答（Text2SQL）：发送的是表结构说明和用户输入的问题，不发送任何会话内容。生成的 SQL 在只读连接上执行，查询结果不回传给模型。
- AI 建议知识点：发送的是**这段会话的标题、摘要和前约 3000 字的消息**。它只在用户点击“AI 建议”或“让 AI 给没归类的对话提建议”时运行，导入时从不自动调用；按钮旁写明了会发送什么。

`.env.local` 被 `.gitignore` 排除，不要把 key 写进任何被跟踪的文件。

## 仓库附带的数据

仓库附带的演示数据是**合成数据**，不是任何真实对话：

- `staging/structured_manifest_final.jsonl`、`staging/manual_annotations_all.jsonl` 和两份统计报告：90 段学习对话及其会话级标注，供 `--restore-bundled` 恢复。
- `staging/demo_activity.jsonl`：一学期的模拟学习记录（批注、复习卡、复习历史、收藏、阅读进度），供 `--demo` 在恢复后补上。
- `staging/demo_corpus_raw.jsonl` 与 `staging/demo_corpus_rejected.jsonl`：生成过程的原始结果和核查记录。

这些对话由 `staging/generate_demo_corpus.py` 生成：课程、知识点、话题以及每段“AI 答错”对话里 AI 要说错的那句话由人手写，对话正文由大模型按话题写出，再由推理模型逐段核查，有计划外错误的会被修正后重新核查。它们不含真实人名、账号或私人对话，可以公开。

真实的会话清单、标注和由其构建的数据库**只保存在本机**，绝不提交到版本库：`studyvault.db`、`staging/` 下除上述演示文件和脚本外的一切（包括 `staging/private_real_backup/`）都被 `.gitignore` 排除。删除本地数据时，停止服务后删除 `studyvault.db`，再运行 `python app.py --init` 建立空库；需要演示数据时执行 `--demo`。

更新演示数据前，应确认新内容同样是合成的或已充分脱敏，不要把真实对话写进上述被跟踪的文件。导出接口生成的 JSON 和完整备份也应按本地数据处理，不上传到公共位置。
