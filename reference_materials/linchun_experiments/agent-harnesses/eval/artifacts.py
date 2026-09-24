"""读取工厂交付的三份评测产物。"""
from __future__ import annotations

import json
from pathlib import Path


def load_run(run_dir: Path) -> tuple[dict, list, dict]:
    """返回 (corpus_obj, questions, about)."""
    run_dir = Path(run_dir)
    corpus = json.loads((run_dir / "05_corpus.json").read_text(encoding="utf-8"))
    raw_q = json.loads((run_dir / "06_grounded_questions.json").read_text(encoding="utf-8"))
    questions = raw_q if isinstance(raw_q, list) else raw_q.get("questions", [])
    about = json.loads((run_dir / "00_about.json").read_text(encoding="utf-8"))
    return corpus, questions, about


def sessions_from_corpus(corpus: dict) -> list[dict]:
    inner = corpus.get("corpus", corpus)
    return sorted(inner["sessions"], key=lambda s: int(s["session_id"]))


def render_protocol(about: dict) -> str:
    """与 eval.multi_system.load_protocol 同等注入的作答约定。"""
    ap = about.get("answer_protocol") or {}
    if not ap:
        return ""
    out = ["【答题约定(随题库交付,务必遵守)】"]
    for r in ap.get("rules", []):
        out.append(f"- {r}")
    sm = ap.get("gold_sentinel_map") or {}
    if sm:
        refusal = (f"- 拒答措辞:从未涉及→『{sm.get('INSUFFICIENT', '无此项')}』;"
                   f"已停统→『{sm.get('forgotten=true', '已停止统计')}』")
        if "out_of_scope" in sm:
            refusal += f";超出记录时间范围→『{sm['out_of_scope']}』"
        out.append(refusal + "。")
    out.append(
        "- 个人/角色不具备案件级属性;问及某实体它本身没有的属性 → 答『无此项/查无』,"
        "不得经关系链折算到关联实体的值。"
    )
    return "\n".join(out)


def qkey(i: int, q: dict) -> str:
    return f"{i}:{q.get('capability')}:{q.get('entity')}:{q.get('field')}"
