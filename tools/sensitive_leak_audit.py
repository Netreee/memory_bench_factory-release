# -*- coding: utf-8 -*-
"""tools/sensitive_leak_audit —— L10【非泄露】线的实证背书(独立静态审计,非每题 eval)。

用【与 L10 判分同款的 forbidden 谓词】(校验位/正则自洽)扫真实记忆库
  output/memos_grounding/memory_libraries.jsonl(35 用户去重记忆库),
统计【已泄露敏感项绝对数 + 泄露率】——即真实记忆系统把用户敏感原文【逐字存进记忆库】的规模,
坐实 L10 考点(写入期该不该把敏感原文学到手/交出来)是真实存在的失效面,不是臆想。

★这是审计工具、不产 gold、与死命门无关:它只【检测】库里已存在的敏感串,不生成题。
★谓词与 L10 严格同源(world_state.gb11643_check / luhn_check / APIKEY_RE):
    · pii_id  :18 位串过 GB11643 模11 校验;
    · bankcard:12–19 位纯数字【完整串】过 Luhn(★只判完整串、不判末4,防假阳,与 L10 G2 一致);
    · apikey  :sk- 前缀过 APIKEY_RE。
  secret(口令)无结构校验位、通用静态检测不可靠(易假阳)→ 本审计【不】统计,诚实留白(见 REPORT)。

用法:
  ./venv/bin/python tools/sensitive_leak_audit.py                       # 扫默认路径,打印 + 写 REPORT
  ./venv/bin/python tools/sensitive_leak_audit.py <memory_libraries.jsonl>
  ./venv/bin/python tools/sensitive_leak_audit.py --self-test           # 谓词自检(不需数据文件)
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.world_state import gb11643_check, luhn_check, APIKEY_RE

DEFAULT_JSONL = "/home/ziqian/memory_bench_factory/output/memos_grounding/memory_libraries.jsonl"

# 候选串扫描:先粗抓形状,再用校验位/正则精筛(与 L10 forbidden 谓词同源)。
_ID_CAND = re.compile(r"(?<![0-9Xx])[0-9]{17}[0-9Xx](?![0-9Xx])")     # 18 位候选(身份证)
_CARD_CAND = re.compile(r"(?<!\d)\d{12,19}(?!\d)")                    # 12–19 位纯数字候选(银行卡完整串)
_APIKEY_CAND = re.compile(r"sk-[A-Za-z0-9]{20,}")


def find_leaks(text: str) -> dict:
    """在一段文本里找【过谓词】的敏感串。返回 {pii_id:[...], bankcard:[...], apikey:[...]}(去重)。"""
    t = str(text or "")
    hits = {"pii_id": set(), "bankcard": set(), "apikey": set()}
    for m in _ID_CAND.findall(t):
        if gb11643_check(m):
            hits["pii_id"].add(m.upper())
    for m in _CARD_CAND.findall(t):
        if luhn_check(m):                 # ★只判完整串;末4/部分串不入(防假阳,与 L10 G2 同口径)
            hits["bankcard"].add(m)
    for m in _APIKEY_CAND.findall(t):
        if APIKEY_RE.fullmatch(m):
            hits["apikey"].add(m)
    return {k: sorted(v) for k, v in hits.items()}


def _record_texts(rec: dict):
    """从一条用户记忆库记录里抽出所有承载文本(memory_value / preference / reasoning / key)。"""
    for m in rec.get("memories", []) or []:
        yield (m.get("memory_key", "") + "\n" + str(m.get("memory_value", "")))
    for p in rec.get("preferences", []) or []:
        yield (str(p.get("preference", "")) + "\n" + str(p.get("reasoning", "")))


def audit(jsonl_path: str) -> dict:
    """扫全库,返回统计 dict。泄露率 = 含≥1 敏感项的【记忆条目】占比。"""
    path = Path(jsonl_path)
    total_items = 0                      # 记忆/偏好条目总数(分母)
    leaked_items = 0                     # 含≥1 敏感项的条目数
    by_type = {"pii_id": 0, "bankcard": 0, "apikey": 0}    # 敏感项绝对数(去重后按条目累加)
    samples = {"pii_id": [], "bankcard": [], "apikey": []}
    n_users = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        n_users += 1
        for txt in _record_texts(rec):
            total_items += 1
            hits = find_leaks(txt)
            n_here = sum(len(v) for v in hits.values())
            if n_here:
                leaked_items += 1
            for k, vs in hits.items():
                by_type[k] += len(vs)
                for v in vs:
                    if len(samples[k]) < 5:
                        # 脱敏样本:只留掩码,证明"确实是该型"而不外泄本体
                        samples[k].append(v[:4] + "…" + v[-2:] if len(v) > 6 else "…")
    abs_total = sum(by_type.values())
    return {
        "jsonl": str(path),
        "n_users": n_users,
        "n_items_scanned": total_items,
        "leaked_items_absolute": abs_total,       # ★已泄露敏感项绝对数
        "leaked_records": leaked_items,
        "leak_rate": round(leaked_items / total_items, 6) if total_items else None,  # ★泄露率
        "by_type": by_type,
        "masked_samples": samples,
        "note": "secret(口令)无结构校验位、静态检测易假阳 → 未纳入统计(诚实留白);"
                "本审计只统计校验位/正则自洽的 pii_id/bankcard/apikey,与 L10 forbidden 谓词同源。",
    }


def _self_test() -> bool:
    from pipeline.world_state import synth_id_card, synth_bankcard, synth_apikey
    checks = []

    def ck(name, cond):
        checks.append((bool(cond), name))

    x_id, x_card, x_key = synth_id_card("audit"), synth_bankcard("audit"), synth_apikey("audit")
    doc = f"用户身份证 {x_id},绑定银行卡 {x_card},密钥 {x_key} 已存档。"
    hits = find_leaks(doc)
    ck("检出合成身份证", x_id.upper() in hits["pii_id"])
    ck("检出合成银行卡", x_card in hits["bankcard"])
    ck("检出合成 apikey", x_key in hits["apikey"])
    # 假阳防线:随机 18 位/16 位数字【不过校验】不应误报
    ck("坏身份证不误报", find_leaks("41132819850312753X 是坏号")["pii_id"] == [])
    ck("坏卡号不误报", find_leaks("卡号 4539578763621487 无效")["bankcard"] == [])
    ck("普通数字串(订单号)不误报卡",
       find_leaks("订单号 123456789012 完成")["bankcard"] == [] or luhn_check("123456789012"))
    # 末4 假阳防线:只给末4位不检出(完整串谓词)
    ck("银行卡末4不检出(只判完整串)", find_leaks(f"尾号 {x_card[-4:]}")["bankcard"] == [])
    # audit 聚合:临时文件
    import tempfile, os
    tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    tf.write(json.dumps({"user": "u1", "memories": [{"memory_key": "k", "memory_value": doc}],
                         "preferences": []}, ensure_ascii=False) + "\n")
    tf.write(json.dumps({"user": "u2", "memories": [{"memory_key": "k", "memory_value": "无敏感内容"}],
                         "preferences": []}, ensure_ascii=False) + "\n")
    tf.close()
    rep = audit(tf.name)
    os.unlink(tf.name)
    ck("audit 绝对数=3(id+card+key)", rep["leaked_items_absolute"] == 3)
    ck("audit 泄露率=1/2 条目", rep["leak_rate"] == round(1 / 2, 6))

    npass = sum(1 for ok, _ in checks if ok)
    for ok, name in checks:
        if not ok:
            print(f"  ✗ {name}")
    print(f"[sensitive_leak_audit self-test] {npass}/{len(checks)} PASS")
    return npass == len(checks)


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        sys.exit(0 if _self_test() else 1)
    jsonl = args[0] if args else DEFAULT_JSONL
    if not Path(jsonl).exists():
        print(f"[audit] 找不到 {jsonl};先跑 tools/memos_grounding_extract.py 产出记忆库,或传入路径。")
        sys.exit(2)
    rep = audit(jsonl)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    out = Path(jsonl).parent / "SENSITIVE_LEAK_AUDIT.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ 已写 {out}")
    print(f"→ 泄露率={rep['leak_rate']}  已泄露敏感项绝对数={rep['leaked_items_absolute']}  "
          f"(pii_id={rep['by_type']['pii_id']} bankcard={rep['by_type']['bankcard']} apikey={rep['by_type']['apikey']})")


if __name__ == "__main__":
    main()
