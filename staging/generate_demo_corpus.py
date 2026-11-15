"""Generate the bundled SYNTHETIC demo corpus (one student, one semester, four courses).

Nothing here comes from real conversations. Two stages:

1. ``generate``: for each hand-written topic spec below, ask an LLM (DeepSeek, key from
   ``.env.local``) to write the dialogue and suggest annotations. Raw replies are cached in
   ``staging/demo_corpus_raw.jsonl`` so the assemble stage is reproducible offline.
2. ``assemble``: deterministically (seeded) turn the raw dialogues into the files
   ``--restore-bundled`` / ``--demo`` read: dates across the semester, which doubts are still
   open, which AI mistakes were caught, review cards and a simulated review history.

Topic specs are where the content is controlled: every ``error`` is a known misconception the
AI states and the student later catches, so the "AI 答错" items in the demo are real mistakes.

    uv run python staging/generate_demo_corpus.py            # generate missing + assemble
    uv run python staging/generate_demo_corpus.py --assemble # assemble from cache only
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import text2sql  # noqa: E402  (reuses the .env.local loader and LLM config)

STAGING = ROOT / "staging"
RAW = STAGING / "demo_corpus_raw.jsonl"
REJECTED = STAGING / "demo_corpus_rejected.jsonl"  # verifier findings, kept for the record
MANIFEST = STAGING / "structured_manifest_final.jsonl"
ANNOTATIONS = STAGING / "manual_annotations_all.jsonl"
ACTIVITY = STAGING / "demo_activity.jsonl"
REPORT = STAGING / "structured_final_report.json"
ANNOTATION_REPORT = STAGING / "manual_annotations_all_report.json"
ANCHOR = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)  # "today" of the simulated semester

DB, OS, ML, ENG = "数据库系统概论", "操作系统", "机器学习与数据分析", "程序设计与工程实践"

# (course, point, window start, window end, [(topic, kind, wrong claim or None)])
# kind: concept 讲解 / error AI 说错被学生指出 / confused 学到最后仍有疑问 / practice 做题或调试
SYLLABUS = [
    (DB, "关系模型与关系代数", "09-02", "09-12", [
        ("关系代数里的选择、投影和连接分别在做什么", "concept", None),
        ("投影运算会不会保留重复元组", "error", "关系代数的投影运算会保留重复元组，和 SQL 的 SELECT 一样"),
        ("除运算到底在求什么：选了全部课程的学生", "confused", None),
        ("自然连接和等值连接的区别", "concept", None),
        ("用关系代数写出没有选过某门课的学生", "practice", None),
    ]),
    (DB, "SQL 与查询", "09-13", "09-30", [
        ("WHERE 和 HAVING 的区别与执行顺序", "error", "HAVING 在 GROUP BY 之前执行，所以它是先筛选再分组"),
        ("LEFT JOIN 的右表条件放 ON 还是 WHERE", "error", "LEFT JOIN 时右表的过滤条件放在 ON 里还是 WHERE 里，结果完全一样"),
        ("COUNT(*) 和 COUNT(列名) 的区别", "error", "COUNT(列名) 同样会把该列为 NULL 的行计算在内"),
        ("相关子查询是怎么一行一行执行的", "confused", None),
        ("NOT IN 遇到 NULL 为什么查不出结果", "error", "NOT IN 子查询结果里含有 NULL 时，结果和 NOT EXISTS 完全一样"),
        ("写 SQL：查出每门课成绩最高的学生", "practice", None),
        ("窗口函数 ROW_NUMBER、RANK、DENSE_RANK 的区别", "concept", None),
        ("写 SQL：找出连续三天都有登录记录的用户", "practice", None),
        ("UNION 和 UNION ALL 怎么选", "concept", None),
    ]),
    (DB, "数据库完整性", "10-01", "10-08", [
        ("外键到底在保证什么，和参照完整性的关系", "concept", None),
        ("主键、唯一约束和实体完整性", "error", "唯一约束和主键一样，都不允许列中出现 NULL"),
        ("CHECK 约束和触发器分别适合做什么", "concept", None),
        ("给订单和订单明细设计外键与级联删除", "practice", None),
    ]),
    (DB, "规范化与模式设计", "10-08", "10-18", [
        ("给定函数依赖怎么求候选码", "confused", None),
        ("3NF 和 BCNF 的区别", "error", "只要关系模式满足 3NF，它就一定也满足 BCNF"),
        ("模式分解怎样才算无损连接", "confused", None),
        ("把多对多联系的 E-R 图转成关系模式", "practice", None),
        ("第二范式在消除什么问题", "concept", None),
    ]),
    (DB, "索引与查询优化", "10-18", "10-26", [
        ("为什么数据库索引用 B+ 树而不是 B 树", "confused", None),
        ("复合索引的最左前缀原则", "error", "建了复合索引 (a, b) 之后，只写 WHERE b = ? 也能利用最左前缀走这个索引"),
        ("看懂一条查询的 EXPLAIN 执行计划", "practice", None),
        ("哪些写法会让索引失效", "concept", None),
        ("聚簇索引和非聚簇索引", "concept", None),
    ]),
    (DB, "事务与并发", "10-26", "11-06", [
        ("事务的 ACID 分别指什么", "concept", None),
        ("隔离级别和幻读", "error", "按 SQL 标准，可重复读（REPEATABLE READ）隔离级别下不会出现幻读"),
        ("两阶段锁协议和死锁", "error", "两阶段锁协议（2PL）可以保证不会发生死锁"),
        ("怎么用前驱图判断冲突可串行化", "confused", None),
        ("死锁检测里的等待图", "concept", None),
        ("用版本号实现乐观锁防止超卖", "practice", None),
    ]),
    (DB, "故障恢复", "11-06", "11-12", [
        ("WAL 先写日志原则", "error", "WAL 要求先把修改过的数据页写回磁盘，再写对应的日志记录"),
        ("检查点（checkpoint）有什么用", "concept", None),
        ("系统崩溃后哪些事务 UNDO、哪些 REDO", "confused", None),
        ("事务故障、系统故障、介质故障的区别", "concept", None),
    ]),
    (OS, "进程与线程", "09-04", "09-19", [
        ("进程和线程的区别", "error", "同一进程里的多个线程各自拥有独立的地址空间"),
        ("fork 之后父子进程的变量互不影响吗", "practice", None),
        ("用户级线程和内核级线程", "concept", None),
        ("上下文切换的开销来自哪里", "concept", None),
    ]),
    (OS, "CPU 调度", "09-20", "10-04", [
        ("短作业优先调度会不会饥饿", "error", "非抢占式的短作业优先（SJF）调度不会让任何进程饥饿"),
        ("算时间片轮转调度的平均周转时间", "practice", None),
        ("多级反馈队列是怎么工作的", "confused", None),
        ("什么是优先级反转", "concept", None),
    ]),
    (OS, "同步与死锁", "10-05", "10-20", [
        ("信号量的 P、V 操作", "error", "信号量初值为 0 时，第一个执行 P 操作的进程不会被阻塞"),
        ("用信号量写生产者消费者", "practice", None),
        ("银行家算法怎么找安全序列", "confused", None),
        ("死锁的四个必要条件", "concept", None),
        ("哲学家就餐问题的几种解法", "concept", None),
    ]),
    (OS, "内存管理", "10-20", "11-03", [
        ("FIFO 页面置换和 Belady 异常", "error", "FIFO 页面置换中增加物理页框数，缺页次数一定不会增加"),
        ("算有 TLB 时的有效访问时间", "practice", None),
        ("多级页表为什么能省内存", "confused", None),
        ("LRU 和 Clock 置换算法", "concept", None),
        ("什么是抖动，怎么缓解", "concept", None),
    ]),
    (OS, "文件系统", "11-03", "11-11", [
        ("inode、硬链接和软链接", "concept", None),
        ("算多级索引分配的最大文件大小", "practice", None),
        ("磁盘调度 SCAN 和 C-SCAN", "concept", None),
    ]),
    (ML, "模型评估与调优", "09-08", "09-25", [
        ("精确率和召回率的关系", "error", "精确率提高的时候，召回率一定也会随之提高"),
        ("交叉验证时数据泄漏是怎么发生的", "confused", None),
        ("ROC 曲线和 AUC 怎么理解", "concept", None),
        ("类别极不平衡时该看什么指标", "practice", None),
        ("网格搜索和随机搜索调参", "concept", None),
    ]),
    (ML, "过拟合与正则化", "09-25", "10-12", [
        ("L1 和 L2 正则化谁更容易得到稀疏解", "error", "L2 正则化比 L1 正则化更容易得到稀疏解"),
        ("偏差-方差权衡", "confused", None),
        ("dropout 为什么能防止过拟合", "concept", None),
        ("从学习曲线判断过拟合还是欠拟合", "practice", None),
    ]),
    (ML, "优化与梯度下降", "10-12", "10-26", [
        ("学习率怎么选", "error", "学习率越大收敛越快，所以学习率设得越大越好"),
        ("批量、随机和小批量梯度下降", "concept", None),
        ("手推线性回归均方误差的梯度", "practice", None),
        ("Adam 优化器到底做了什么", "confused", None),
    ]),
    (ML, "树模型与集成学习", "10-26", "11-10", [
        ("随机森林里的特征随机", "error", "随机森林里每棵树在每次分裂时都会使用全部特征"),
        ("手算信息增益和基尼指数", "practice", None),
        ("Bagging 和 Boosting 的区别", "concept", None),
        ("GBDT 里拟合残差是什么意思", "confused", None),
    ]),
    (ENG, "软件工程与测试", "09-02", "10-01", [
        ("单元测试和集成测试怎么分工", "concept", None),
        ("什么时候该用 mock", "confused", None),
        ("pytest fixture 写法", "practice", None),
        ("测试覆盖率高是不是就够了", "concept", None),
    ]),
    (ENG, "Git 与协作", "09-15", "10-20", [
        ("git rebase 和 merge", "error", "git rebase 不会改写提交历史，所以对已经推送的公共分支也可以随便 rebase"),
        ("解决一次 merge 冲突", "practice", None),
        ("git reset 和 git revert 的区别", "confused", None),
        ("功能分支和 Pull Request 流程", "concept", None),
    ]),
    (ENG, "Python 与调试", "09-01", "11-10", [
        ("可变默认参数的坑", "error", "Python 函数的默认参数在每次调用时都会重新创建"),
        ("is 和 == 的区别", "error", "在 Python 里 is 和 == 完全等价，只是两种写法"),
        ("读懂一段 Traceback 定位 KeyError", "practice", None),
        ("列表推导式和生成器表达式", "concept", None),
        ("用 pdb 设断点调试循环", "practice", None),
        ("深拷贝和浅拷贝", "confused", None),
    ]),
]

# Topics deliberately shipped without a knowledge point, to demo "AI 建议知识点".
UNCLASSIFIED = {"UNION 和 UNION ALL 怎么选", "什么是优先级反转", "dropout 为什么能防止过拟合", "测试覆盖率高是不是就够了", "磁盘调度 SCAN 和 C-SCAN", "聚簇索引和非聚簇索引"}

PLATFORM_BY_KIND = {
    "concept": [("ChatGPT", 5), ("DeepSeek", 3), ("Gemini CLI", 1)],
    "error": [("ChatGPT", 4), ("DeepSeek", 3), ("Gemini CLI", 1), ("Pi", 1)],
    "confused": [("ChatGPT", 4), ("DeepSeek", 3), ("Pi", 1)],
    "practice": [("Codex", 3), ("Claude Code", 3), ("ChatGPT", 2)],
}

KIND_RULES = {
    "concept": "学生想弄懂一个概念，AI 讲清楚，学生追问一两个细节，AI 解答。全程 AI 的说法都正确。",
    "error": "AI 在第一次回答里明确说出了下面这个【错误说法】（要像真的这么认为一样自然地说出来，不要暗示它是错的）。学生之后举出反例或引用课本质疑，AI 承认错误并给出正确解释。除了这个错误，其他内容都要正确。",
    "confused": "学生问一个较难的概念，AI 讲解，学生追问后仍然没完全弄懂。最后一条用户消息要具体说出还卡在哪里；最后一条 AI 回复只给出部分解释，没有把这个疑点彻底讲透。AI 的说法本身都正确。",
    "practice": "学生在做作业或写代码，给出具体的题目、SQL 或代码（可能有 bug），AI 帮忙分析并给出正确做法，学生确认或追问一次。AI 的说法都正确。",
}

PROMPT = """你在为一个数据库课程大作业生成【合成】演示数据：一个大学生和 AI 助手的学习对话。不要包含任何真实人名、学校、账号或个人信息。

课程：{course}
知识点：{point}
话题：{topic}
场景：{rule}
{error_line}
要求：
- 4 到 8 条消息，user 和 assistant 交替，user 先说。
- 学生说话像真人：口语、简短，有时直接贴题目或代码，不要客套。
- AI 回复 80–350 字，可用 Markdown、代码块，公式用 $...$。
- 中文。

只输出一个 JSON 对象，字段：
{{
  "title": "像聊天软件自动起的会话标题，12–24 字",
  "summary": "一句话摘要",
  "tags": ["2–3 个短标签"],
  "messages": [{{"speaker": "user|assistant", "content": "..."}}],
  "wrong_seq": 出错那条 AI 消息的序号（从 1 开始；非出错场景填 null）,
  "correction": "学生给出错 AI 消息写的纠错批注，一两句话，说清错在哪、正确是什么（非出错场景填 null）",
  "doubt_seq": 学生仍有疑问时，疑问所在消息的序号（非疑问场景填 null）,
  "doubt": "学生写给自己的疑问批注，一句话，具体（非疑问场景填 null）",
  "key_seq": 最值得收藏复习的那条 AI 消息序号,
  "key_note": "学生给这条消息写的重点批注，一句话",
  "card_question": "根据对话出一道复习题（问句）",
  "card_answer": "复习题的标准答案，2–4 句话",
  "intent": "概念澄清|作业求助|代码调试|纠错讨论 中选一个",
  "topics": ["2–3 个主题词"],
  "key_points": ["2–3 条要点"],
  "unresolved_questions": ["学生还没弄懂的问题，没有就空列表"],
  "action_items": ["0–2 条学生接下来要做的事"]
}}"""


def specs() -> list[dict]:
    out, index = [], 0
    for course, point, start, end, topics in SYLLABUS:
        for topic, kind, error in topics:
            index += 1
            out.append({"id": f"demo-{index:03}", "course": course, "point": point, "topic": topic, "kind": kind, "error": error, "window": (start, end)})
    return out


def call_llm(spec: dict, config: dict) -> dict:
    error_line = f"【错误说法】{spec['error']}\n" if spec["error"] else ""
    prompt = PROMPT.format(course=spec["course"], point=spec["point"], topic=spec["topic"], rule=KIND_RULES[spec["kind"]], error_line=error_line)
    payload = {"model": config["model"], "messages": [{"role": "user", "content": prompt}], "temperature": 0.9,
               "response_format": {"type": "json_object"}, "stream": False}
    request = urllib.request.Request(f"{config['base_url']}/chat/completions", data=json.dumps(payload).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                body = json.loads(response.read().decode("utf-8"))
            data = json.loads(body["choices"][0]["message"]["content"])
            check(spec, data)
            return {"id": spec["id"], **data}
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last = exc
    raise RuntimeError(f"{spec['id']} {spec['topic']}: {last}")


def check(spec: dict, data: dict) -> None:
    messages = data["messages"]
    assert 4 <= len(messages) <= 10, "message count"
    assert [m["speaker"] for m in messages] == ["user", "assistant"] * (len(messages) // 2) + (["user"] if len(messages) % 2 else []), "alternation"
    assert all(isinstance(m["content"], str) and m["content"].strip() for m in messages)
    seq_ok = lambda seq, speaker: isinstance(seq, int) and 1 <= seq <= len(messages) and messages[seq - 1]["speaker"] == speaker
    assert seq_ok(data["key_seq"], "assistant"), "key_seq"
    if spec["kind"] == "error":
        assert seq_ok(data["wrong_seq"], "assistant") and data["correction"], "wrong_seq"
    if spec["kind"] == "confused":
        assert isinstance(data["doubt_seq"], int) and 1 <= data["doubt_seq"] <= len(messages) and data["doubt"], "doubt"
    for field in ("title", "summary", "card_question", "card_answer", "intent", "key_note"):
        assert isinstance(data[field], str) and data[field].strip(), field


def generate(only_missing: bool = True) -> None:
    cached = {}
    if RAW.exists():
        cached = {row["id"]: row for row in map(json.loads, RAW.read_text(encoding="utf-8").splitlines()) if row}
    todo = [spec for spec in specs() if not (only_missing and spec["id"] in cached)]
    config = text2sql.llm_config()
    if todo and not config["api_key"]:
        raise SystemExit("需要 DEEPSEEK_API_KEY（.env.local）才能生成新对话；已有缓存可用 --assemble")
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = {spec["id"]: pool.submit(call_llm, spec, config) for spec in todo}
        for key, future in futures.items():
            try:
                cached[key] = future.result()
                print("ok", key)
            except Exception as exc:  # noqa: BLE001
                print("FAILED", exc)
    RAW.write_text("".join(json.dumps(cached[s["id"]], ensure_ascii=False) + "\n" for s in specs() if s["id"] in cached), encoding="utf-8")


VERIFY_PROMPT = """下面是一段学习对话（课程：{course}，话题：{topic}）。请逐条检查 assistant 消息里的事实、推导和计算是否正确（自己动手算一遍，不要只看结论）。
{planted}
只输出 JSON：{{"ok": true/false, "problems": ["第 N 条：错在哪，正确是什么"]}}。没有问题时 problems 为空列表。

对话：
{dialogue}"""


def verify(spec: dict, data: dict, config: dict) -> list[str]:
    """Second opinion from a reasoning model: any AI mistake other than the planted one is a defect."""
    planted = (f"注意：第 {data['wrong_seq']} 条 assistant 消息里故意说了这个错误：「{spec['error']}」，学生随后纠正了它。这一处不算问题，后面 AI 的纠正如果正确也不算问题。" if spec["error"] else "")
    dialogue = "\n\n".join(f"[{i}] {m['speaker']}: {m['content']}" for i, m in enumerate(data["messages"], 1))
    verdict = text2sql._extract_json(reasoner(VERIFY_PROMPT.format(course=spec["course"], topic=spec["topic"], planted=planted, dialogue=dialogue), config))
    return [] if verdict.get("ok") and not verdict.get("problems") else [str(p) for p in verdict.get("problems") or ["未通过核查"]]


REPAIR_PROMPT = """下面这段 JSON 是一段学习对话的合成数据，核查发现 assistant 消息里有以下错误：
{problems}
{planted}
请只修正这些错误（自己重新推导/计算一遍），其余内容、消息条数和说话顺序保持不变；如果修正影响到后面的消息、复习题答案或要点，一并改对。
只输出修正后的完整 JSON，字段和原来完全一样。

{data}"""


def reasoner(prompt: str, config: dict) -> str:
    """Call deepseek-reasoner with streaming.

    A non-streaming call sends nothing while the model reasons (often a minute or more), and the
    silent connection tends to be dropped on the way, leaving the client to wait out its timeout.
    Streaming keeps bytes flowing; the timeout only fires if the stream itself goes quiet.
    """
    payload = {"model": "deepseek-reasoner", "messages": [{"role": "user", "content": prompt}], "stream": True}
    request = urllib.request.Request(f"{config['base_url']}/chat/completions", data=json.dumps(payload).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}, method="POST")
    parts = []
    with urllib.request.urlopen(request, timeout=120) as response:
        for line in response:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue  # blank lines and ": keep-alive" comments
            if line == b"data: [DONE]":
                break
            parts.append(json.loads(line[5:])["choices"][0]["delta"].get("content") or "")
    return "".join(parts)


def repair(spec: dict, data: dict, problems: list[str], config: dict) -> dict:
    """Fix only what the verifier flagged; falls back to a fresh dialogue if the repair is malformed."""
    planted = f"（第 {data['wrong_seq']} 条 AI 消息里的「{spec['error']}」是故意安排的错误，必须保留，学生随后会纠正它。）" if spec["error"] else ""
    body = {k: v for k, v in data.items() if k not in {"id", "verified"}}
    try:
        fixed = text2sql._extract_json(reasoner(REPAIR_PROMPT.format(problems="\n".join(problems), planted=planted, data=json.dumps(body, ensure_ascii=False)), config))
        check(spec, fixed)
        return {"id": spec["id"], **fixed}
    except Exception:  # noqa: BLE001
        return call_llm(spec, config)


def verify_all(rounds: int = 5) -> None:
    """Check every unverified dialogue; repair the ones with unplanned mistakes and check again."""
    config = text2sql.llm_config()
    by_id = {spec["id"]: spec for spec in specs()}
    for round_no in range(1, rounds + 1):
        cached = {row["id"]: row for row in map(json.loads, RAW.read_text(encoding="utf-8").splitlines()) if row}
        todo = [key for key, row in cached.items() if not row.get("verified")]
        if not todo:
            break
        print(f"核查第 {round_no} 轮：{len(todo)} 段", flush=True)
        with ThreadPoolExecutor(max_workers=64) as pool:
            verdicts = {key: pool.submit(verify, by_id[key], cached[key], config) for key in todo}
            redo = {}
            for key, future in verdicts.items():
                try:
                    problems = future.result()
                except Exception as exc:  # noqa: BLE001 - a timeout says nothing about the dialogue; check it again next round
                    print("  核查未完成，下轮重试", key, exc, flush=True)
                    continue
                if problems:
                    print("  有问题", key, by_id[key]["topic"], problems[:2], flush=True)
                    with REJECTED.open("a", encoding="utf-8") as log:
                        log.write(json.dumps({"id": key, "round": round_no, "topic": by_id[key]["topic"], "problems": problems}, ensure_ascii=False) + "\n")
                    redo[key] = pool.submit(repair, by_id[key], cached[key], problems, config)
                else:
                    print("  通过", key, flush=True)
                    cached[key]["verified"] = True
            for key, future in redo.items():
                try:
                    cached[key] = future.result()  # fresh dialogue, unverified until the next round
                except Exception as exc:  # noqa: BLE001
                    print("  重新生成失败", key, exc)
        RAW.write_text("".join(json.dumps(cached[s["id"]], ensure_ascii=False) + "\n" for s in specs() if s["id"] in cached), encoding="utf-8")
    left = [row["id"] for row in map(json.loads, RAW.read_text(encoding="utf-8").splitlines()) if row and not row.get("verified")]
    if left:
        raise SystemExit(f"仍未通过核查：{left}")


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def assemble() -> None:
    raw = {row["id"]: row for row in map(json.loads, RAW.read_text(encoding="utf-8").splitlines()) if row}
    rng = random.Random(20261113)
    manifest, annotations, activity = [], [], []
    for spec in specs():
        data = raw.get(spec["id"])
        if not data:
            raise SystemExit(f"缺少 {spec['id']} 的生成结果，先运行 generate")
        # Topics are spread evenly through their syllabus window, at a plausible study hour.
        start = datetime.fromisoformat(f"2026-{spec['window'][0]}T00:00:00+00:00")
        end = datetime.fromisoformat(f"2026-{spec['window'][1]}T00:00:00+00:00")
        siblings = [s for s in specs() if s["course"] == spec["course"] and s["point"] == spec["point"]]
        position = siblings.index(spec)
        day = start + (end - start) * (position + 0.5) / len(siblings)
        began = day.replace(hour=rng.choice([1, 2, 3, 6, 7, 11, 12, 13, 14, 15]), minute=rng.randrange(60))
        moment, messages = began, []
        for message in data["messages"]:
            messages.append({"speaker": message["speaker"], "content": message["content"].strip(), "created_at": iso(moment)})
            moment += timedelta(minutes=rng.randint(1, 6))
        platforms, weights = zip(*PLATFORM_BY_KIND[spec["kind"]])
        platform = rng.choices(platforms, weights)[0]
        source_id = f"{platform.lower().replace(' ', '-')}-{spec['id']}"
        manifest.append({
            "platform": platform, "source_id": source_id, "course": spec["course"], "title": data["title"].strip(),
            "started_at": iso(began), "summary": data["summary"].strip(), "messages": messages,
            "tags": list(dict.fromkeys(t.strip() for t in data["tags"] if t.strip()))[:3] or [spec["point"]],
            "knowledge_points": [] if spec["topic"] in UNCLASSIFIED else [spec["point"]],
            "message_count": len(messages), "user_message_count": sum(m["speaker"] == "user" for m in messages),
            "assistant_message_count": sum(m["speaker"] == "assistant" for m in messages),
        })
        annotations.append({
            "platform": platform, "source_id": source_id, "summary_manual": data["summary"].strip(), "intent": data["intent"],
            "topics": data.get("topics") or [], "key_points": data.get("key_points") or [],
            "unresolved_questions": data.get("unresolved_questions") or [], "action_items": data.get("action_items") or [],
            "quality": "有效" if spec["kind"] != "confused" else "混合", "confidence": "high" if spec["kind"] in {"concept", "practice"} else "medium",
        })
        activity.append(simulate(spec, data, platform, source_id, began, rng))
    write_jsonl(MANIFEST, manifest)
    write_jsonl(ANNOTATIONS, annotations)
    write_jsonl(ACTIVITY, activity)
    REPORT.write_text(json.dumps(report(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ANNOTATION_REPORT.write_text(json.dumps({"note": "合成演示数据，非真实对话。", "annotations": len(annotations)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report(manifest), ensure_ascii=False))


def simulate(spec: dict, data: dict, platform: str, source_id: str, began: datetime, rng: random.Random) -> dict:
    """What the student did with this conversation afterwards, relative to ANCHOR."""
    after = lambda days: min(ANCHOR, began + timedelta(days=days, hours=rng.randint(0, 6)))
    marks, cards = [], []
    if spec["kind"] == "error":
        # Most caught mistakes are settled once the AI corrected itself; some still need checking.
        marks.append({"seq_no": data["wrong_seq"], "type": "CORRECTION", "content": data["correction"].strip(),
                      "created_at": iso(after(0)), "resolved_at": iso(after(rng.randint(1, 6))) if rng.random() < 0.65 else None})
    if spec["kind"] == "confused":
        marks.append({"seq_no": data["doubt_seq"], "type": "QUESTION", "content": data["doubt"].strip(),
                      "created_at": iso(after(0)), "resolved_at": iso(after(rng.randint(3, 12))) if rng.random() < 0.25 else None})
    elif rng.random() < 0.18:
        # An occasional follow-up doubt on otherwise clear conversations.
        seq = data["key_seq"]
        marks.append({"seq_no": seq, "type": "QUESTION", "content": f"还想确认：{data['card_question'].strip().rstrip('？?')}，换个例子还成立吗？",
                      "created_at": iso(after(1)), "resolved_at": iso(after(rng.randint(2, 8))) if rng.random() < 0.5 else None})
    if rng.random() < 0.45:
        marks.append({"seq_no": data["key_seq"], "type": "IMPORTANT", "content": data["key_note"].strip(), "created_at": iso(after(0)), "resolved_at": None})
    card_rate = {"error": 0.9, "confused": 0.55, "concept": 0.45, "practice": 0.5}[spec["kind"]]
    if rng.random() < card_rate:
        seq = data["wrong_seq"] + 2 if spec["kind"] == "error" and data["wrong_seq"] + 2 <= len(data["messages"]) else data["key_seq"]
        if data["messages"][seq - 1]["speaker"] != "assistant":
            seq = data["key_seq"]
        created = after(rng.randint(0, 2))
        # Harder material is answered wrong more often; each review happens near its due date.
        p_correct = {"error": 0.6, "confused": 0.55, "concept": 0.82, "practice": 0.75}[spec["kind"]]
        reviews, interval, streak, ease, due = [], 0, 0, 2.5, created
        while True:
            at = due + timedelta(days=rng.choice([0, 0, 0, 1, 1, 2]), hours=rng.randint(0, 5))
            if at >= ANCHOR - timedelta(hours=2):
                break
            grade = "correct" if rng.random() < p_correct + 0.05 * streak else "wrong"
            reviews.append({"at": iso(at), "grade": grade})
            # Mirror app.schedule_next so the replay lands on the same due dates.
            if grade == "wrong":
                interval, streak, ease = 1, 0, round(max(1.3, ease - 0.2), 2)
            else:
                streak += 1
                interval = 3 if streak == 1 else max(interval + 1, round(interval * ease))
            due = at + timedelta(days=interval)
        cards.append({"seq_no": seq, "question": data["card_question"].strip(), "answer": data["card_answer"].strip(), "created_at": iso(created), "reviews": reviews})
    opened = rng.random() < 0.7
    return {
        "platform": platform, "source_id": source_id, "marks": marks, "cards": cards,
        "favorite": spec["kind"] in {"error", "confused"} and rng.random() < 0.3 or rng.random() < 0.06,
        "last_read_seq": rng.randint(1, len(data["messages"])) if opened else None,
        "last_opened_at": iso(after(rng.randint(0, 20))) if opened else None,
        "confirm_point": rng.random() < 0.7,
    }


def report(manifest: list[dict]) -> dict:
    count = lambda key: {k: sum(1 for row in manifest if row[key] == k) for k in sorted({row[key] for row in manifest})}
    messages = [m for row in manifest for m in row["messages"]]
    return {"note": "合成演示数据，非真实对话。由 staging/generate_demo_corpus.py 生成（话题与错误说法人工编写，对话由大模型按话题生成）。",
            "sessions": len(manifest), "messages": len(messages),
            "user_messages": sum(m["speaker"] == "user" for m in messages), "assistant_messages": sum(m["speaker"] == "assistant" for m in messages),
            "by_course": count("course"), "by_platform": count("platform")}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--assemble", action="store_true", help="只用缓存组装，不调用大模型")
    parser.add_argument("--regenerate", action="store_true", help="忽略缓存，全部重新生成")
    args = parser.parse_args()
    if not args.assemble:
        generate(only_missing=not args.regenerate)
        verify_all()
    assemble()
