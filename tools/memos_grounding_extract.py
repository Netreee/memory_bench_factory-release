# -*- coding: utf-8 -*-
"""从 recall_logs.zip(35 用户 mysql 表 dump)提取干净的 grounding 语料。
产物:
  output/memos_grounding/memory_libraries.jsonl  每用户去重记忆库(memory + preference)
  output/memos_grounding/human_queries.jsonl      去重的人类 query→召回(带 relativity)
  output/memos_grounding/all_classified.jsonl     全部 query + 分类(透明可审)
  output/memos_grounding/REPORT.md / stats.json    统计 + 现象
死命门无关:这是 grounding 语料,不产 gold。
"""
import zipfile, json, re, os, hashlib
from collections import Counter, defaultdict

ZIP = "/home/ziqian/memory_bench_factory/recall_logs.zip"
OUT = "/home/ziqian/memory_bench_factory/output/memos_grounding"
os.makedirs(OUT, exist_ok=True)

# ---- 稳健的 JSON 花括号扫描(无视 mysql 表格边框)----
def scan_objs(text, prefix, start=0, limit=None):
    """返回 [(start_idx, end_idx, obj_str)],从 prefix 开始做带引号感知的括号配平。
    limit=N 时找到 N 个即停(response 只需 first_only,避免 O(n^2))。"""
    out = []
    i = start
    n = len(text)
    while True:
        s = text.find(prefix, i)
        if s < 0:
            break
        depth = 0; instr = False; esc = False; j = s; ok = False
        while j < n:
            ch = text[j]
            if esc: esc = False
            elif ch == '\\': esc = True
            elif ch == '"': instr = not instr
            elif not instr:
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        out.append((s, j + 1, text[s:j + 1])); ok = True; break
            j += 1
        if limit and len(out) >= limit:
            break
        i = (j + 1) if ok else (s + len(prefix))
    return out

# ---- query 分类器(从实测高频模式提炼)----
PATS = [
    ('heartbeat', re.compile(r'HEARTBEAT|workspace context', re.I)),
    ('cron', re.compile(r'\[cron:|health-check|perception-digest|home-patrol|git-auto|daily-lesson|招标监控|每30分钟|守护', re.I)),
    ('subagent', re.compile(r'\[Subagent Context\]|SUBAGENT_LABEL|running as a subagent|sessions_spawn', re.I)),
    ('protocol', re.compile(r'AUTOCLAW|OUTPUT_PROTOCOL|NO_REPLY', re.I)),
    ('scheduled', re.compile(r'scheduled reminder|A scheduled|每日新闻|每日个股|新闻简报|定时', re.I)),
    ('digest_tagprompt', re.compile(r'dream diary|运营沉淀|技术沉淀|工作沉淀|感知日志', re.I)),
]
TRIVIAL = re.compile(r'^(test|测试|测试一下|ping|你好|hi|hello|在吗|确认连接.*)$', re.I)

def classify(q):
    q = (q or '').strip()
    if not q:
        return 'empty'
    if TRIVIAL.match(q):
        return 'trivial'
    for name, pat in PATS:
        if pat.search(q):
            return name
    if len(q) > 600 and ('System:' in q or 'Write a' in q or '你是' in q[:20] or 'You are' in q[:20]):
        return 'agent_long'
    return 'human'

def norm_q(q):
    return re.sub(r'\s+', ' ', (q or '').strip().lower())[:200]

# ---- 主流程 ----
zf = zipfile.ZipFile(ZIP)
files = [n for n in zf.namelist() if n.endswith('.csv') and not n.startswith('__MACOSX')]

cls_counter = Counter()
src_counter = Counter()
memtype_counter = Counter()
preftype_counter = Counter()
relv_all = []
per_user_rows = Counter()
libs = defaultdict(lambda: {"memories": {}, "preferences": {}})  # user -> id -> item
human_seen = {}   # norm_q -> record (dedup 人类 query)
all_cls = []      # 透明分类流
n_req = n_resp_ok = 0
heartbeat_ct = 0

for fn in files:
    user_file = fn.split('recall_log_')[1].replace('.csv', '')
    txt = zf.read(fn).decode('utf-8', 'replace')
    reqs = scan_objs(txt, '{"user_id":')
    for (rs, re_, robj) in reqs:
        try:
            jq = json.loads(robj)
        except Exception:
            continue
        if 'query' not in jq:
            continue
        n_req += 1
        per_user_rows[user_file] += 1
        q = jq.get('query') or ''
        src = jq.get('source') or 'NULL'
        src_counter[src] += 1
        cls = classify(q)
        cls_counter[cls] += 1
        if cls == 'heartbeat':
            heartbeat_ct += 1
        # 找紧跟其后的 response {"code":(只取第一个,避免 O(n^2))
        resp = scan_objs(txt, '{"code":', re_, limit=1)
        recalled = []
        if resp:
            robj2 = resp[0][2]
            try:
                js = json.loads(robj2); d = js.get('data') or {}; n_resp_ok += 1
                for m in (d.get('memory_detail_list') or []):
                    if not isinstance(m, dict):
                        continue
                    mid = m.get('id')
                    memtype_counter[m.get('memory_type', '?')] += 1
                    if m.get('relativity') is not None:
                        relv_all.append(m['relativity'])
                    if mid:
                        libs[user_file]["memories"][mid] = {
                            "id": mid, "memory_key": m.get('memory_key'),
                            "memory_value": m.get('memory_value'), "memory_type": m.get('memory_type'),
                            "tags": m.get('tags'), "confidence": m.get('confidence'),
                            "create_time": m.get('create_time'), "conversation_id": m.get('conversation_id'),
                        }
                    recalled.append({"memory_id": mid, "memory_key": m.get('memory_key'),
                                     "memory_value": (m.get('memory_value') or '')[:500],
                                     "memory_type": m.get('memory_type'), "relativity": m.get('relativity')})
                for p in (d.get('preference_detail_list') or []):
                    if not isinstance(p, dict):
                        continue
                    pid = p.get('id'); preftype_counter[p.get('preference_type', '?')] += 1
                    if pid:
                        libs[user_file]["preferences"][pid] = {
                            "id": pid, "preference_type": p.get('preference_type'),
                            "preference": p.get('preference'), "reasoning": (p.get('reasoning') or '')[:400],
                            "create_time": p.get('create_time'),
                        }
            except Exception:
                pass
        rec = {"user": user_file, "source": src, "endpoint": jq.get('endpoint'),
               "query": q, "class": cls, "n_recalled": len(recalled)}
        all_cls.append({"user": user_file, "query": q[:160], "class": cls})
        if cls == 'human':
            nq = norm_q(q)
            if nq and nq not in human_seen:
                human_seen[nq] = {**rec, "recalled": recalled}

# ---- 写产物 ----
with open(f"{OUT}/memory_libraries.jsonl", "w") as f:
    for user, lib in sorted(libs.items(), key=lambda kv: -len(kv[1]["memories"])):
        f.write(json.dumps({"user": user, "n_memories": len(lib["memories"]),
                            "n_preferences": len(lib["preferences"]),
                            "memories": list(lib["memories"].values()),
                            "preferences": list(lib["preferences"].values())}, ensure_ascii=False) + "\n")

with open(f"{OUT}/human_queries.jsonl", "w") as f:
    for rec in human_seen.values():
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

with open(f"{OUT}/all_classified.jsonl", "w") as f:
    for r in all_cls:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

tot_mem = sum(len(l["memories"]) for l in libs.values())
tot_pref = sum(len(l["preferences"]) for l in libs.values())
stats = {
    "n_requests": n_req, "n_resp_ok": n_resp_ok, "n_users": len(per_user_rows),
    "class_breakdown": dict(cls_counter.most_common()),
    "human_kept_dedup": len(human_seen),
    "source_breakdown": dict(src_counter.most_common(12)),
    "memory_type": dict(memtype_counter), "preference_type": dict(preftype_counter),
    "unique_memories_total": tot_mem, "unique_preferences_total": tot_pref,
    "heartbeat_requests": heartbeat_ct,
    "relativity": {"n": len(relv_all),
                   "min": round(min(relv_all), 3) if relv_all else None,
                   "max": round(max(relv_all), 3) if relv_all else None,
                   "mean": round(sum(relv_all) / len(relv_all), 3) if relv_all else None},
}
json.dump(stats, open(f"{OUT}/stats.json", "w"), ensure_ascii=False, indent=2)

hb_pct = 100 * heartbeat_ct // max(1, n_req)
human_pct = 100 * cls_counter['human'] // max(1, n_req)
rep = f"""# memOS recall_logs · grounding 提取报告

来源:`recall_logs.zip` 35 用户 / {n_req} 检索请求。**产物为 grounding 语料,非 benchmark(无 gold)。**

## 分类breakdown(query 性质)
{chr(10).join(f'- {k}: {v} ({100*v//max(1,n_req)}%)' for k,v in cls_counter.most_common())}

→ **人类 query 保留(去重后)= {len(human_seen)} 条**(占原始 {human_pct}%,去重前 human={cls_counter['human']})。

## 现象(可写进论文)
- **系统噪声挤占**:heartbeat 类请求 {heartbeat_ct} 条 = **全量 {hb_pct}%**;单条 "Read HEARTBEAT.md" 是最高频。
- 自动化/系统/心跳/cron/subagent 合计压过真人。

## 记忆库
- 去重唯一记忆项 **{tot_mem}** 条 + 偏好 **{tot_pref}** 条(across {len(per_user_rows)} 用户)。
- memory_type: {dict(memtype_counter)}
- preference_type: {dict(preftype_counter)}
- relativity 分数:n={len(relv_all)} 范围 {stats['relativity']['min']}~{stats['relativity']['max']} 均值 {stats['relativity']['mean']} → 可算 recall@k/NDCG。

## 产物文件
- `memory_libraries.jsonl` — {len(libs)} 用户的去重记忆库(grounding 主力:真实 memory_key/value/type/tags/时间戳)
- `human_queries.jsonl` — {len(human_seen)} 条去重人类 query→召回(带 relativity)
- `all_classified.jsonl` — 全 {len(all_cls)} 条 query 的分类(可审计清洗是否公道)
"""
open(f"{OUT}/REPORT.md", "w").write(rep)
print(rep)
print(f"✓ 产物写入 {OUT}")
