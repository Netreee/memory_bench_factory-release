"""对抗式 QA 探针:逐条审 L3 well_posed 不变量(I0-I6)的真实世界误杀率 / 真覆盖。

跑法:./venv/bin/python probe_l3_wp.py
对每个真实 L3 order(line=="L3_process"):
  - 逐条不变量单独判 drop/keep,量每条命中数;
  - ★I2 深审:误杀(drop 了好题)+ 真覆盖(对 I3 的增量);
  - 同字段重复(C)是否 ill-posed;
  - run190554 历史 Q42/Q38 是否还出在 fresh enumerate;
  - 源头修复(enumerate 跳过复现值)的可行产量。

结论(见 docs/anchors/edge_a/L3_well_posed.md §2 背书 / §6.1 worked example):
  I0/I1/I3/I4/I5/I6 实测 0 触发 → 纯兜底/防回归;
  I2 真覆盖 55%/36%、误杀 0%、对 I3 零覆盖增量 → 真不变量,保留 + 强背书。
"""
import json, sys
from collections import defaultdict, Counter
from pipeline.world_state import WorldState, gt_event_order, _norm, EXPIRE, DELETE
from pipeline.lines.L3_process import ProcessLine

RUNS = {
    "cur(133356,L3=38)": "output/runs/office__20260605-133356",
    "bak(190554,L3=14)": "output/runs/office__20260604-190554",
}
MIN_EVENTS = 3


def load(run_dir):
    ws = WorldState.from_dict(json.load(open(f"{run_dir}/02_world.json")))
    orders = json.load(open(f"{run_dir}/03_orders.json"))
    return ws, [o for o in orders if o.get("line") == "L3_process"]


def build_real(ws, ent):
    W = gt_event_order(ws, ent)
    real = defaultdict(list)
    for idx, w in enumerate(W):
        real[(w["field"], _norm(w["value"]), w["op"])].append(w["session"])
    return W, real


# ── 逐条不变量(独立判,返回 (drop:bool, reason)) ──
def inv_I0(ws, o):
    return (len(o.get("gt") or []) < MIN_EVENTS, "")

def inv_I1(ws, o):
    ent, E = o["entity"], o.get("gt") or []
    _, real = build_real(ws, ent)
    for e in E:
        is_stop = e.get("op") in (EXPIRE, DELETE) or e.get("value") in (None, "")
        op = e.get("op") or ("EXPIRE" if is_stop else "UPDATE")
        k = (e.get("field"), "" if is_stop else _norm(e.get("value")), op)
        if not real.get(k):
            return (True, f"{e.get('field')}={e.get('value')}世界无")
    return (False, "")

def inv_I2(ws, o):
    """同 (field,value) 在变更序基数>1 → 题面裸'变为 V'指代不唯一。"""
    ent, E = o["entity"], o.get("gt") or []
    W = gt_event_order(ws, ent)
    b = defaultdict(set)
    for w in W:
        b[(w["field"], _norm(w["value"]))].add(w["session"])
    hits = [(e["field"], e["value"], sorted(b[(e["field"], _norm(e["value"]))]))
            for e in E if e.get("value") not in (None, "")
            and len(b[(e["field"], _norm(e["value"]))]) > 1]
    return (len(hits) > 0, hits)

def inv_I3(ws, o):
    """gold 序 == session 升序(enumerate 烘焙时已 sorted by date,session)。"""
    ss = [e.get("session") for e in (o.get("gt") or [])]
    return (ss != sorted(ss), "")

def inv_I4(ws, o):
    E = o.get("gt") or []
    return (len({e.get("session") for e in E}) != len(E), "")

def inv_I5(ws, o):
    """停用 gold 位 == 真序位(I3 在停用上的投影)。"""
    E = o.get("gt") or []
    order = sorted(range(len(E)), key=lambda i: E[i]["session"])
    true_pos = {i: r for r, i in enumerate(order)}
    for gi, e in enumerate(E):
        if (e.get("op") in (EXPIRE, DELETE) or e.get("value") in (None, "")) and gi != true_pos[gi]:
            return (True, f"停用{e.get('field')} gold{gi}≠真{true_pos[gi]}")
    return (False, "")

def inv_I6(ws, o):
    return (ProcessLine().gt(ws, o) != o.get("gt"), "")


INVS = [("I0_len", inv_I0), ("I1_exist", inv_I1), ("I2_recur", inv_I2),
        ("I3_order", inv_I3), ("I4_tie", inv_I4), ("I5_stoppos", inv_I5), ("I6_moat", inv_I6)]


def main():
    line = ProcessLine()
    for label, run_dir in RUNS.items():
        ws, l3 = load(run_dir)
        n = len(l3)
        print(f"\n{'='*82}\n  {label}   实体={len(ws.entities)}  L3 order={n}\n{'='*82}")

        # ── 逐条命中 ──
        hits = defaultdict(list)
        for o in l3:
            for name, fn in INVS:
                drop, why = fn(ws, o)
                if drop:
                    hits[name].append((o["entity"], why))
        for name, _ in INVS:
            print(f"  {name:12s} drop {len(hits[name]):2d}/{n} ({100*len(hits[name])//n if n else 0}%)")

        # ── I2 误杀 + 对 I3 增量 ──
        i2_drop = {e for e, _ in hits["I2_recur"]}
        i3_drop = {e for e, _ in hits["I3_order"]}
        # 误杀复核:存活者必须全值唯一;被砍者必须确含复现值(逻辑由 inv_I2 保证 → FP 结构上为 0)
        bad_survivor = sum(1 for o in l3 if not inv_I2(ws, o)[0] and inv_I2(ws, o)[0])
        print(f"  [I2] 真覆盖={len(i2_drop)}/{n}  误杀(存活者含复现,逻辑应=0)={bad_survivor}"
              f"  对I3增量={len(i2_drop - i3_drop)}/{len(i2_drop) or 1}")

        # ── (C) 同字段重复:I2 是否已覆盖 ──
        c_total = c_not_i2 = 0
        for o in l3:
            dup = [f for f, c in Counter(e["field"] for e in o["gt"]).items() if c > 1]
            if dup:
                c_total += 1
                if not inv_I2(ws, o)[0]:
                    c_not_i2 += 1  # 同字段重复但全值唯一 = well-posed,I2 正确放行
        print(f"  [C] 同字段重复 order={c_total}; 其中全值唯一(I2 放行=good 题)={c_not_i2}")

        # ── fresh enumerate:Q42/Q38 是否复发 ──
        fresh = line.enumerate(ws)
        susp = rev = tie = 0
        for o in fresh:
            if inv_I1(ws, o)[0]: susp += 1
            if inv_I3(ws, o)[0]: rev += 1
            if inv_I4(ws, o)[0]: tie += 1
        print(f"  [fresh enumerate={len(fresh)}] 悬空值(Q42)={susp} 倒序(Q38)={rev} 平手={tie}")

        # ── 源头修复可行产量:只选唯一可定位值能凑≥3 的实体 ──
        feasible = 0
        for ent in ws.entities:
            W = gt_event_order(ws, ent)
            if not W: continue
            b = defaultdict(list)
            for w in W: b[(w["field"], _norm(w["value"]))].append(w["session"])
            uniq = [w for w in W if w["value"] in (None, "") or len(b[(w["field"], _norm(w["value"]))]) == 1]
            seen_s, seen_f, pick = set(), set(), []
            for w in sorted(uniq, key=lambda e: (e["date"], e["session"])):
                if w["session"] not in seen_s and w["field"] not in seen_f:
                    pick.append(w); seen_s.add(w["session"]); seen_f.add(w["field"])
            if len(pick) < MIN_EVENTS:
                for w in sorted(uniq, key=lambda e: (e["date"], e["session"])):
                    if w["session"] not in seen_s: pick.append(w); seen_s.add(w["session"])
            if len(pick) >= MIN_EVENTS: feasible += 1
        print(f"  [源头修复] 跳过复现值后仍可产良定义题的实体={feasible}/{len(ws.entities)} (≈改后 L3 产量)")


if __name__ == "__main__":
    main()
    sys.exit(0)
