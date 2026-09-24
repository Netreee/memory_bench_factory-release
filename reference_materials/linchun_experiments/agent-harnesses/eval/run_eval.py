#!/usr/bin/env python3
"""路径 1：复用工厂产物，用本地 agent harness 作答，再用 eval.judge.judge_answer 判分。

先接 OpenAI Agents SDK（单模型）。语料按 session 落成文件，agent 用只读记忆工具查阅。

  python eval/run_eval.py --factory-root /path/to/memory_bench_factory \\
    --run /path/to/run --harness openai-agents --limit 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parent
HARNESS_ROOT = EVAL_ROOT.parent
FACTORY_ROOT = None
SDK_DIR = HARNESS_ROOT / "openai-agents-sdk"

# 工厂路径在参数解析后配置，--help 不加载 SDK 或 API 环境。
sys.path.insert(0, str(SDK_DIR))
sys.path.insert(0, str(EVAL_ROOT))


# tmux / 父进程里的过期 key 必须让仓库 .env 覆盖，setdefault 不够。
_FORCE_ENV_KEYS = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_CHAT_COMPLETIONS_URL",
}


def _load_env(path: Path, *, force: bool = False) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        value = v.strip().strip("'").strip('"')
        if force or key in _FORCE_ENV_KEYS or key not in os.environ:
            os.environ[key] = value


def _ensure_experiment_env(dest: Path) -> None:
    """把工厂 API 配置复制到 runs/.env（不覆盖已有实验文件）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        return
    src = FACTORY_ROOT / ".env"
    keep = {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "MODEL",
        "LLM_DISABLE_THINKING",
        "LLM_CONCURRENCY",
        "LLM_DEADLINE_S",
    }
    lines = [
        "# 本实验 API。从 memory-bench-factory/.env 复制，不覆盖仓库根 .env（aiaiapi）。",
        "# 不要提交。",
        "",
    ]
    if src.is_file():
        for raw in src.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in keep:
                lines.append(f"{k.strip()}={v.strip().strip(chr(39)).strip(chr(34))}")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dest.chmod(0o600)


from artifacts import load_run, qkey, render_protocol, sessions_from_corpus  # noqa: E402
from costing import attach_cost, summarize_costs  # noqa: E402
from memory_store import MemoryStore  # noqa: E402
from experiment_state import (  # noqa: E402
    digest, find_factory_root, is_reusable, load_records, prepare_experiment,
    question_hash, save_records,
)


def aggregate(records: list[dict]) -> dict:
    from eval.grading import is_scored
    jr = [r for r in records if r.get("judgeable")]
    judged = [r for r in jr if is_scored(r)]

    def acc(rs):
        rs = [r for r in rs if r.get("judgeable") and is_scored(r)]
        if not rs:
            return {"n": 0, "correct": 0, "acc": None}
        c = sum(1 for r in rs if r["correct"])
        return {"n": len(rs), "correct": c, "acc": round(c / len(rs), 3)}

    by_line = {
        ln: acc([r for r in jr if r["line"] == ln])
        for ln in sorted({r["line"] for r in jr})
    }
    by_cap = {
        cap: acc([r for r in jr if r["capability"] == cap])
        for cap in sorted({r["capability"] for r in jr})
    }
    return {
        "n_all": len(records),
        "n_judgeable": len(jr),
        "n_judged": len(judged),
        "overall": acc(jr),
        "by_line": by_line,
        "by_cap": by_cap,
        "n_errors": sum(1 for r in records if r.get("error") or r.get("judge_error")),
        "n_incomplete": len(records) - len(judged),
    }


def write_summary_md(path: Path, meta: dict, agg: dict) -> None:
    o = agg["overall"]
    acc = "n/a" if o["acc"] is None else f"{o['acc']:.1%} ({o['correct']}/{o['n']})"
    lines = [
        f"# Agent 评测 {meta['run_name']}",
        "",
        f"- harness: `{meta['harness']}`",
        f"- model: `{meta['model']}`",
        f"- 可判分准确率: **{acc}**",
        f"- 耗时: {meta['elapsed_s']}s",
        "",
        "## 按能力线",
        "",
        "| line | n | correct | acc |",
        "|---|---:|---:|---:|",
    ]
    if (meta.get("release") or {}).get("override"):
        lines += ["> 历史研究模式：显式绕过发布资格，结果不能作为正式成绩。", ""]
    for ln, d in agg["by_line"].items():
        a = "—" if d["acc"] is None else f"{d['acc']:.3f}"
        lines.append(f"| {ln} | {d['n']} | {d['correct']} | {a} |")
    lines += ["", "## 按 capability", "", "| capability | n | correct | acc |", "|---|---:|---:|---:|"]
    for cap, d in agg["by_cap"].items():
        a = "—" if d["acc"] is None else f"{d['acc']:.3f}"
        lines.append(f"| {cap} | {d['n']} | {d['correct']} | {a} |")
    cost = meta.get("cost")
    if cost:
        lines += [
            "",
            "## 成本",
            "",
            f"- 已跑 {cost.get('n_scored')} 题，有 usage 的 {cost.get('n_with_cost')} 题",
            f"- tokens: prompt={cost.get('prompt_tokens')} completion={cost.get('completion_tokens')} "
            f"cached={cost.get('cached_tokens')} total={cost.get('total_tokens')}",
            f"- 已花费（低/高）: {cost.get('spent_cny_low')} / {cost.get('spent_cny_high')} 元",
            f"- 单题均价（低/高）: {cost.get('per_question_cny_low')} / {cost.get('per_question_cny_high')} 元",
            f"- 预估全量 {cost.get('estimate_n')} 题（低/高）: "
            f"**{cost.get('estimate_full_cny_low')} / {cost.get('estimate_full_cny_high')} 元**",
            f"- 单价口径: {cost.get('price_cny_per_m')}（元/百万 token）",
            f"- {cost.get('note')}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    global FACTORY_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--factory-root", default="", help="工厂根目录；默认 MEMORY_BENCH_FACTORY_ROOT 或向上自动发现")
    ap.add_argument("--run", required=True, help="工厂 run 目录（含 05/06/00 json）")
    ap.add_argument("--allow-unverified", action="store_true", help="仅历史研究：显式允许未取得发布资格的输入")
    ap.add_argument(
        "--harness",
        default="openai-agents",
        choices=["openai-agents", "claude", "codex", "dsh"],
    )
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 道可判分题（烟测）")
    ap.add_argument("--max-turns", type=int, default=12)
    ap.add_argument("--timeout", type=int, default=180, help="CLI harness 单题超时秒")
    ap.add_argument(
        "--out",
        default="",
        help="结果目录；默认 runs/<场景>__<模型>/<harness>",
    )
    ap.add_argument(
        "--env",
        default="",
        help="实验 .env；默认复制工厂 API 到 runs/.env",
    )
    ap.add_argument("--model", default="", help="覆盖 MODEL；不写则用环境变量 / .env")
    ap.add_argument(
        "--keys",
        default="",
        help="只跑这些题 key，逗号分隔，例如 4:KU:铁冠粮仓:角色等级",
    )
    args = ap.parse_args()
    if args.limit < 0 or args.max_turns <= 0 or args.timeout <= 0:
        ap.error("limit 必须非负，max-turns 和 timeout 必须为正数")
    try:
        FACTORY_ROOT = find_factory_root(args.factory_root)
    except ValueError as exc:
        ap.error(str(exc))
    sys.path.insert(0, str(FACTORY_ROOT))
    _load_env(HARNESS_ROOT / ".env")
    env_path = Path(args.env) if args.env else HARNESS_ROOT / "runs" / ".env"
    _ensure_experiment_env(env_path)
    os.environ["HARNESS_ENV_LOCKED"] = "1"
    _load_env(env_path, force=True)
    if args.model:
        os.environ["MODEL"] = args.model
    base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    if base:
        os.environ["OPENAI_CHAT_COMPLETIONS_URL"] = base + "/chat/completions"
    # 工厂 .env 的 LLM_DISABLE_THINKING 会让 judge 带 enable_thinking，aiaiapi 会 400。
    # 阿里云 DeepSeek-V4 必须保留，否则思考占满输出。
    if "aiaiapi.com" in os.environ.get("OPENAI_BASE_URL", ""):
        os.environ.pop("LLM_DISABLE_THINKING", None)

    # API 配置已就绪后才导入判分器；CLI 路径无需安装 Agents SDK。
    from harness_cli import answer_cli, apply_cli_env, ensure_workspace_instructions
    from eval.judge import classify_refusal, gold_display, is_judgeable, judge_record, judgement, literal_match, judge_spec
    from eval.grading import JUDGE_VERSION
    from pipeline.quality import require_release

    run_dir = Path(args.run).resolve()
    corpus, questions, about = load_run(run_dir)
    release = require_release(run_dir / "06_grounded_questions.json", allow_unverified=args.allow_unverified,
                              corpus_path=run_dir / "05_corpus.json")
    protocol = render_protocol(about)
    sessions = sessions_from_corpus(corpus)
    judgeable = [q for q in questions if is_judgeable(q)]
    skipped = len(questions) - len(judgeable)
    indexed = list(enumerate(judgeable))
    if args.keys:
        want = {k.strip() for k in args.keys.split(",") if k.strip()}
        indexed = [(i, q) for i, q in indexed if qkey(i, q) in want]
        missing = want - {qkey(i, q) for i, q in indexed}
        if missing:
            print(f"warn: --keys 未命中 {sorted(missing)}")
    elif args.limit:
        indexed = indexed[: args.limit]

    model_name = os.environ.get("MODEL", "gpt-5.6-sol-r1-staff")
    out_dir = Path(args.out) if args.out else (
        HARNESS_ROOT / "runs" / f"{run_dir.name}__{model_name}" / args.harness
    )
    source_files = [p for p in sorted(EVAL_ROOT.glob("*.py")) if not p.name.startswith("test_")]
    source_files += sorted(SDK_DIR.glob("*.py"))
    source_files += [FACTORY_ROOT / "eval" / "judge.py", FACTORY_ROOT / "eval" / "grading.py", FACTORY_ROOT / "config.py"]
    experiment_config = {
        "schema": 2, "harness": args.harness, "model": model_name, "judge_version": JUDGE_VERSION,
        "allow_unverified": args.allow_unverified,
        "max_turns": args.max_turns, "timeout": args.timeout,
        "api_endpoint_hash": digest(os.environ.get("OPENAI_BASE_URL", "")),
        "disable_thinking": os.environ.get("LLM_DISABLE_THINKING", ""),
        "inputs_hash": digest({"corpus": corpus, "questions": questions, "about": about}),
        "source_hashes": {str(p.relative_to(HARNESS_ROOT)) if p.is_relative_to(HARNESS_ROOT) else "factory/" + str(p.relative_to(FACTORY_ROOT)): digest(p.read_text(encoding="utf-8")) for p in source_files},
    }
    try:
        fingerprint = prepare_experiment(out_dir, experiment_config)
        current_records = load_records(out_dir / "results.jsonl", fingerprint)
    except ValueError as exc:
        ap.error(str(exc))
    workspace = out_dir / "memory_files"
    store = MemoryStore(sessions, workspace)
    (workspace / "PROTOCOL.md").write_text(protocol, encoding="utf-8")
    ensure_workspace_instructions(workspace)

    apply_cli_env()
    model = os.environ.get("MODEL", "gpt-5.6-sol-r1-staff")
    provider = None
    agent = None
    if args.harness == "openai-agents":
        from bootstrap import configure_sdk
        from harness_openai import answer_one, make_agent
        client, model, provider = configure_sdk(use_responses=True)
        agent = make_agent(store, model)
        _ = client

    results_path = out_dir / "results.jsonl"
    done = {key: rec for key, rec in current_records.items() if is_reusable(rec, JUDGE_VERSION)}
    print(
        f"run={run_dir.name} model={model} sessions={len(sessions)} "
        f"docs={len(store.docs)} questions={len(indexed)} "
        f"(skip_unjudgeable={skipped}) resumed={len(done)}"
    )

    t0 = time.monotonic()
    # 历次尝试单独保留，标准结果每题只有一条，筛题不受重试影响。
    with (out_dir / "attempts.jsonl").open("a", encoding="utf-8") as fh:
        for i, q in indexed:
            key = qkey(i, q)
            qhash = question_hash(q)
            if qhash in done:
                rec = done[qhash]
                mark = "✓" if rec.get("correct") else "✗"
                print(f"  {mark} [resume] [{q['line'][:2]}/{q['capability']}] pred={str(rec.get('pred',''))[:28]!r}")
                continue
            mode, _, _ = judge_spec(q)
            rec = {
                "key": key,
                "_question_hash": qhash,
                "_experiment": fingerprint,
                "qid": q.get("qid"),
                "strict_scoring": q.get("strict_scoring"),
                "question_contract": q.get("question_contract"),
                "i": i,
                "line": q["line"],
                "capability": q["capability"],
                "entity": q.get("entity"),
                "field": q.get("field"),
                "question": q["question"],
                "gt": q.get("gt"),
                "aux": q.get("aux"),
                "gold_set": gold_display(q),
                "mode": mode,
                "judgeable": True,
            }
            previous = current_records.get(qhash) or {}
            if (previous.get("judge_error") and not previous.get("error")
                    and isinstance(previous.get("pred"), str) and previous["pred"].strip()):
                ans = {key: previous[key] for key in ("pred", "elapsed_s", "usage") if key in previous}
                ans["_prediction_resumed"] = True
            elif args.harness == "openai-agents":
                ans = answer_one(
                    agent,
                    provider,
                    protocol,
                    q["question"],
                    max_turns=args.max_turns,
                    session_id=key,
                )
            else:
                ans = answer_cli(
                    args.harness,
                    workspace,
                    protocol,
                    q["question"],
                    timeout_s=args.timeout,
                    max_turns=args.max_turns,
                    entity=q.get("entity"),
                    field=q.get("field"),
                )
            rec.update(ans)
            attach_cost(rec, model)
            pred = rec.get("pred") or ""
            if rec.get("error"):
                grade = judgement("error", "solver", str(rec["error"])[:160])
            else:
                try:
                    grade = judge_record(q, pred, use_llm=True)
                    if q["capability"] == "L6_refusal":
                        lure = ((q.get("aux") or {}).get("lure") or {}).get("value")
                        rec["refusal_bucket"] = classify_refusal(pred, lure)
                    if q["capability"] == "L2_multihop":
                        rec["partial"] = (1.0 if grade["correct"] is True else
                                          0.5 if grade["correct"] is False and literal_match(pred, [(q.get("aux") or {}).get("bridge")]) else
                                          0.0 if grade["correct"] is False else None)
                except Exception as e:
                    grade = judgement("error", "judge_exception", f"{type(e).__name__}:{str(e)[:160]}")
            rec["judgement"] = grade
            rec["correct"] = grade["correct"]
            if grade["verdict"] == "error" and not rec.get("error"):
                rec["judge_error"] = grade["reason"]
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            current_records[qhash] = rec
            save_records(results_path, current_records)
            mark = "∅" if rec.get("correct") is None else ("✓" if rec["correct"] else "✗")
            print(
                f"  {mark} [{q['line'][:2]}/{q['capability']}] "
                f"{rec['elapsed_s']}s pred={pred[:36]!r} gold={str(rec['gold_set'])[:40]}"
            )

    elapsed = round(time.monotonic() - t0, 1)
    all_recs = list(current_records.values())
    agg = aggregate(all_recs)
    cost = summarize_costs(all_recs, model, n_full=len(judgeable))
    meta = {
        "run_dir": str(run_dir),
        "run_name": run_dir.name,
        "harness": args.harness,
        "model": model,
        "experiment_fingerprint": fingerprint,
        "factory_root": str(FACTORY_ROOT),
        "release": release, "judge_version": JUDGE_VERSION,
        "result_scope": "research_only" if release.get("override") else "release_eligible",
        "n_sessions": len(sessions),
        "n_docs": len(store.docs),
        "elapsed_s": elapsed,
        "cost": cost,
        "base_host": os.environ.get("OPENAI_BASE_URL", "").split("//", 1)[-1].split("/", 1)[0],
    }
    summary = {"meta": meta, "aggregate": agg}
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_summary_md(out_dir / "summary.md", meta, agg)
    o = agg["overall"]
    print(
        f"\nDONE acc={o['acc']} ({o['correct']}/{o['n']}) "
        f"errors={agg['n_errors']} elapsed_s={elapsed} out={out_dir} "
        f"cost_cny={cost.get('spent_cny_low')}/{cost.get('spent_cny_high')} "
        f"est_full={cost.get('estimate_full_cny_low')}/{cost.get('estimate_full_cny_high')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
