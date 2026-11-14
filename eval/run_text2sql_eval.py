"""Text2SQL 评测：对比 baseline 与 C3-SQL 的执行准确率（execution accuracy）。

对每个问题：用标准 SQL 在只读连接上执行得到标准结果，再分别用 baseline / C3 生成 SQL
并执行，按结果集指纹（忽略列名、行序、列序的值多重集合）与标准结果比对。生成报错、
未通过安全校验、或结果不一致都记为错。

评测库：默认在临时目录里构建一个**可复现的合成评测库**——附带演示数据（14 个会话）
再加一组确定性的批注 / 复习卡 / 复习记录 / 收藏 / 已确认知识点。只用演示数据的话，
有 6 道题的标准答案是 0 或空集，任何"查不到东西"的错误 SQL 都会被判对。
也可以用 --db 指定已有数据库（只读使用，不会修改）。

用法（需要配置大模型 Key，评测会真实调用 DeepSeek，花费 token）：
    PYTHONPATH=. python eval/run_text2sql_eval.py
    PYTHONPATH=. python eval/run_text2sql_eval.py --db /path/to/studyvault.db
    PYTHONPATH=. python eval/run_text2sql_eval.py --only 1,5,10
可用 STUDYVAULT_T2S_SAMPLES 调整 C3 的采样条数（默认 5，越多越准也越费）。
所有（题目 × 方法）并发执行，C3 内部的 N 条采样也并发；STUDYVAULT_EVAL_WORKERS 控制
外层并发数（默认 = 题数 × 2，峰值请求数约为 外层并发 × 采样数）。
结果写入 eval/text2sql_eval_result.md，逐题明细（生成的 SQL、C3 诊断）写入
eval/text2sql_eval_detail.jsonl。
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import text2sql as t  # noqa: E402

CASES = [json.loads(line) for line in (ROOT / "eval" / "text2sql_cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
OUT = ROOT / "eval" / "text2sql_eval_result.md"
DETAIL = ROOT / "eval" / "text2sql_eval_detail.jsonl"
METHODS = ["baseline", "c3"]


def build_eval_db(path: Path) -> None:
    """演示数据 + 确定性补充数据。各计数刻意取不同的值，写错过滤条件就会对不上。"""
    app.DB_PATH = path
    app.restore_bundled()
    db = sqlite3.connect(path)
    try:
        uid = app.DEMO_USER_ID
        msgs = [r[0] for r in db.execute("SELECT message_id FROM message ORDER BY message_id")]
        answers = [r[0] for r in db.execute("SELECT message_id FROM message WHERE speaker='assistant' ORDER BY message_id")]
        convs = [r[0] for r in db.execute("SELECT conversation_id FROM conversation ORDER BY conversation_id")]
        # 批注 7 条：NOTE 3 / QUESTION 2 / CORRECTION 1 / IMPORTANT 1；未解决 4 条。
        notes = [("NOTE", 0), ("NOTE", 1), ("NOTE", 0), ("QUESTION", 0), ("QUESTION", 1), ("CORRECTION", 1), ("IMPORTANT", 0)]
        for i, (kind, resolved) in enumerate(notes):
            db.execute("INSERT INTO annotation(message_id,author_id,annotation_type,content,is_resolved) VALUES(?,?,?,?,?)",
                       (msgs[i * 3], uid, kind, f"评测批注 {i + 1}", resolved))
        # 复习卡 5 张；复习记录：答对 4 次、答错 2 次。
        cards = []
        for i in range(5):
            cur = db.execute("INSERT INTO review_card(message_id,owner_id,question,answer,due_at,status) VALUES(?,?,?,?,?,?)",
                             (answers[i * 2], uid, f"评测问题 {i + 1}", "评测答案", "2026-12-01 00:00:00",
                              "COMPLETED" if i == 4 else "ACTIVE"))
            cards.append(cur.lastrowid)
        for card, grade in [(cards[0], "correct"), (cards[0], "correct"), (cards[1], "wrong"),
                            (cards[2], "correct"), (cards[3], "wrong"), (cards[3], "correct")]:
            db.execute("INSERT INTO review_log(review_card_id,grade,interval_before,interval_after,streak_before,streak_after,"
                       "due_before,due_after) VALUES(?,?,1,1,0,0,'2026-11-01','2026-11-02')", (card, grade))
        # 会话状态 5 行，其中收藏 3 个（数 conversation_state 行数会答错）。
        for i, conv in enumerate(convs[:5]):
            db.execute("INSERT INTO conversation_state(owner_id,conversation_id,is_favorite,last_opened_at) VALUES(?,?,?,?)",
                       (uid, conv, 1 if i < 3 else 0, "2026-11-10 10:00:00"))
        # 知识点关联：14 条里确认 4 条、拒绝 2 条。
        links = db.execute("SELECT conversation_id, point_id FROM conversation_point ORDER BY conversation_id, point_id").fetchall()
        for i, (conv, point) in enumerate(links[:6]):
            db.execute("UPDATE conversation_point SET status=? WHERE conversation_id=? AND point_id=?",
                       ("CONFIRMED" if i < 4 else "REJECTED", conv, point))
        db.commit()
    finally:
        db.close()


def predict(method: str, question: str, config: dict, db_path: Path) -> dict:
    """返回 {sql, result, error, c3, seconds}。评测里任何失败都算答错。"""
    started = time.time()
    try:
        if method == "baseline":
            sql = t.validate_sql(t.generate_baseline(question, config))
            out = {"sql": sql, "result": t.run_readonly(db_path, sql), "error": "", "c3": None}
        else:
            result = t.generate_c3(question, config, db_path)
            out = {"sql": result.get("sql", ""), "result": result, "error": "", "c3": result.get("c3")}
    except Exception as exc:  # noqa: BLE001
        out = {"sql": "", "result": None, "error": f"{type(exc).__name__}: {exc}", "c3": None}
    out["seconds"] = round(time.time() - started, 1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, help="用已有数据库（只读）代替合成评测库")
    parser.add_argument("--only", help="只跑这些题号，逗号分隔")
    args = parser.parse_args()

    config = t.llm_config()
    if not config["api_key"]:
        print("未配置大模型 API Key（DEEPSEEK_API_KEY）。评测需要真实模型，退出。")
        return 1
    cases = CASES
    if args.only:
        wanted = {int(x) for x in args.only.split(",")}
        cases = [c for c in CASES if c["id"] in wanted]

    if args.db:
        db_path, db_label = args.db.resolve(), args.db.name
    else:
        db_path = Path(tempfile.mkdtemp(prefix="t2s-eval-")) / "eval.db"
        build_eval_db(db_path)
        db_label = "合成评测库（演示数据 + 确定性补充）"
    print(f"数据库：{db_label}")
    print(f"模型：{config['model']} · C3 采样数：{config['samples']} · 用例：{len(cases)}\n")

    score = {m: 0 for m in METHODS}
    seconds = {m: 0.0 for m in METHODS}
    golds = {}
    for case in cases:
        gold = t.run_readonly(db_path, case["gold_sql"])
        golds[case["id"]] = (gold, t.result_signature(gold))
    lines = {case["id"]: {"id": case["id"], "question": case["question"], "gold_sql": case["gold_sql"],
                          "gold_rows": golds[case["id"]][0]["rows"][:5]} for case in cases}
    jobs = [(case, method) for case in cases for method in METHODS]
    workers = int(os.getenv("STUDYVAULT_EVAL_WORKERS", str(len(jobs))))
    print(f"并发：{workers} 个任务 × C3 内部 {config['samples']} 路采样\n", flush=True)
    wall = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(predict, method, case["question"], config, db_path): (case, method) for case, method in jobs}
        for future in as_completed(futures):
            case, method = futures[future]
            pred = future.result()
            ok = bool(pred["result"]) and not pred["error"] and t.result_signature(pred["result"]) == golds[case["id"]][1]
            score[method] += ok
            seconds[method] += pred["seconds"]
            lines[case["id"]][method] = {"ok": ok, "sql": pred["sql"], "error": pred["error"], "seconds": pred["seconds"],
                                         "rows": pred["result"]["rows"][:5] if pred["result"] else None, "c3": pred["c3"]}
            print(f"[{case['id']:>2}] {method:>8} {'✅' if ok else '❌'} {pred['seconds']:>5.1f}s  {case['question']}", flush=True)
    wall = time.time() - wall
    rows = [lines[case["id"]] for case in cases]

    n = len(cases)
    print("\n=== 执行准确率 ===")
    for method in METHODS:
        print(f"{method:>8}: {score[method]}/{n} = {score[method] / n:.1%} · 平均 {seconds[method] / n:.1f} 秒/题")
    print(f"总墙钟时间：{wall:.1f} 秒")

    with DETAIL.open("w", encoding="utf-8") as fh:
        for line in rows:
            fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

    def votes(line: dict) -> str:
        c3 = line["c3"]["c3"]
        if not c3:
            return "—"
        return f"{c3['votes']}/{c3['valid']}（{' / '.join(map(str, c3.get('tally') or [c3['votes']]))}）"

    md = ["# Text2SQL 执行准确率：baseline vs C3-SQL\n",
          f"- 数据库：{db_label} · 模型：`{config['model']}` · C3 采样数：{config['samples']} · 用例数：{n}",
          "- 指标：execution accuracy（生成 SQL 的执行结果与标准 SQL 一致，忽略列名/行序/列序）",
          f"- 运行时间：{time.strftime('%Y-%m-%d %H:%M')}，并发跑完共 {wall:.0f} 秒；逐题 SQL 与 C3 诊断见 `eval/text2sql_eval_detail.jsonl`\n",
          "| 方法 | 正确 | 准确率 | 平均耗时 |", "| --- | --- | --- | --- |"]
    for method in METHODS:
        md.append(f"| {method} | {score[method]}/{n} | {score[method] / n:.1%} | {seconds[method] / n:.1f} 秒/题 |")
    md += ["\n## 逐题结果\n", "| # | 问题 | baseline | C3 | C3 胜出票 / 可执行（分组） | C3 召回表 |",
           "| --- | --- | --- | --- | --- | --- |"]
    for line in rows:
        tables = ", ".join((line["c3"]["c3"] or {}).get("linked_tables", [])) or "—"
        md.append(f"| {line['id']} | {line['question']} | {'✅' if line['baseline']['ok'] else '❌'} | "
                  f"{'✅' if line['c3']['ok'] else '❌'} | {votes(line)} | {tables} |")
    OUT.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n已写入 {OUT.relative_to(ROOT)} 与 {DETAIL.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
