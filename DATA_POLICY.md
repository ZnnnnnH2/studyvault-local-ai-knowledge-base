# StudyVault 数据边界

StudyVault 只读取本机 AI 工具的会话文件，并将清洗后的消息写入本地 SQLite。默认扫描目录由 `app.py` 的 `SOURCE_ROOTS` 定义，也可以用对应的 `*_DATA_ROOT` 环境变量覆盖。

导入时会移除运行时上下文，并对 password、token、authorization、账号、`*_KEY` 以及 `sk-`/`tp-` 形式的凭据做确定性脱敏。浏览器 Cookie、网页缓存、密码管理器和代理配置不属于扫描范围。

仓库附带的演示快照是**人工编写的合成数据**，不是任何真实对话：`staging/structured_manifest_final.jsonl`（14 个虚构学习问答）、`staging/manual_annotations_all.jsonl`（对应标注）及两份统计报告，供 `python app.py --restore-bundled` 在其他机器上复现。这些文件不含真实凭据、账号或私人对话，可以公开。

真实的会话清单、标注和由其构建的数据库**只保存在本机**，绝不提交到版本库：`studyvault.db`、`staging/` 下除上述演示文件和脚本外的一切（包括 `staging/private_real_backup/`）都被 `.gitignore` 排除。删除本地数据时，停止服务后删除 `studyvault.db`，再运行 `python app.py --init` 即可建立空库；需要演示数据时再执行 `--restore-bundled`。

更新演示快照前，应确认新内容同样是合成或已充分脱敏的，不要把真实对话写进这四个被跟踪的文件。

演示数据应使用不含真实凭据和私人对话的样例文件；导出接口生成的 JSON 也应按本地数据处理，不上传到公共位置。
