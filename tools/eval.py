"""합성 데이터 평가 (SPEC §6) → data/eval/results.json.

- 진단: app.engine 을 직접 호출.
- 서류 점검 1단계: node tools/detect_cli.mjs (브라우저와 같은 detect.js).
- 서류 점검 2단계·의도 분류: GPU 서버(--gpu-url, 기본 ssh 터널 127.0.0.1:18000).
- 근거 정확도: knowledge.json basis 중 data/laws 스냅샷에 있는 조문 비율.

문서 본문·질의 문장은 출력하지 않는다(id·건수·지표만).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine import Engine, load_knowledge  # noqa: E402

TIER2_LABELS = {"주소", "직장", "학교", "차량", "생활권"}
ARTICLE_BASE_RE = re.compile(r"^(제\d+조(?:의\d+)?)")


class GpuDown(Exception):
    pass


def _load(rel: str) -> Any:
    with (ROOT / rel).open(encoding="utf-8") as f:
        return json.load(f)


def _post(url: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise GpuDown(type(exc).__name__) from exc


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _f1(p: float | None, r: float | None) -> float | None:
    if p is None or r is None or (p + r) == 0:
        return None
    return round(2 * p * r / (p + r), 4)


# ---------------------------------------------------------------- 진단
def eval_diagnose(engine: Engine, scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    tot = {k: 0 for k in ("mi", "mi_hit", "mis", "pe", "pe_hit", "re", "re_hit", "na", "na_hit")}
    problems = []
    for s in scenarios:
        r = engine.diagnose(s["victim_type"], s["flags"])
        ck = {c["id"] for c in r["checklist"]}
        pend = {c["id"] for c in r["checklist"] if c.get("status") == "pending"}
        res = {c["id"] for c in r["residual"]}
        na = {c["id"] for c in r["not_applicable"]}
        missing = [i for i in s.get("must_include", []) if i not in ck]
        mis = [i for i in s.get("must_not_include", []) if i in ck]
        mp = [i for i in s.get("expect_pending", []) if i not in pend]
        mr = [i for i in s.get("expect_residual", []) if i not in res]
        mn = [i for i in s.get("expect_not_applicable", []) if i not in na]
        tot["mi"] += len(s.get("must_include", []))
        tot["mi_hit"] += len(s.get("must_include", [])) - len(missing)
        tot["mis"] += len(mis)
        tot["pe"] += len(s.get("expect_pending", []))
        tot["pe_hit"] += len(s.get("expect_pending", [])) - len(mp)
        tot["re"] += len(s.get("expect_residual", []))
        tot["re_hit"] += len(s.get("expect_residual", [])) - len(mr)
        tot["na"] += len(s.get("expect_not_applicable", []))
        tot["na_hit"] += len(s.get("expect_not_applicable", [])) - len(mn)
        if missing or mis or mp or mr or mn:
            problems.append(
                {
                    "id": s["id"],
                    "missing": missing,
                    "misapplied": mis,
                    "missing_pending": mp,
                    "missing_residual": mr,
                    "missing_not_applicable": mn,
                }
            )
    return {
        "scenarios": len(scenarios),
        "must_include_total": tot["mi"],
        "must_include_hit": tot["mi_hit"],
        "recall": _ratio(tot["mi_hit"], tot["mi"]),
        "misapplied": tot["mis"],
        "pending_expected": tot["pe"],
        "pending_hit": tot["pe_hit"],
        "residual_expected": tot["re"],
        "residual_hit": tot["re_hit"],
        "not_applicable_expected": tot["na"],
        "not_applicable_hit": tot["na_hit"],
        "per_scenario": problems,
    }


# ---------------------------------------------------------------- 서류 점검
def _overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def eval_spans(
    docs: list[dict[str, Any]],
    preds: dict[str, list[dict[str, Any]]],
    labels: set[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    per_label: dict[str, dict[str, Any]] = {}
    fp_docs = 0
    for mode in ("strict_label_overlap", "any_label_overlap"):
        same = mode == "strict_label_overlap"
        tp_p = n_p = tp_g = n_g = 0
        for d in docs:
            gold = [g for g in d["spans"] if labels is None or g["label"] in labels]
            pr = [p for p in preds.get(d["id"], []) if labels is None or p.get("label") in labels]
            n_p += len(pr)
            n_g += len(gold)
            for p in pr:
                if any(_overlap(p, g) and (not same or p.get("label") == g["label"]) for g in gold):
                    tp_p += 1
            for g in gold:
                hit = any(_overlap(p, g) and (not same or p.get("label") == g["label"]) for p in pr)
                if hit:
                    tp_g += 1
                if same:
                    pl = per_label.setdefault(g["label"], {"gold": 0, "hit": 0})
                    pl["gold"] += 1
                    pl["hit"] += int(hit)
            if same and not gold and pr:
                fp_docs += 1
        p = _ratio(tp_p, n_p)
        r = _ratio(tp_g, n_g)
        out[mode] = {
            "pred": n_p,
            "gold": n_g,
            "tp_pred": tp_p,
            "fp": n_p - tp_p,
            "tp_gold": tp_g,
            "fn": n_g - tp_g,
            "precision": p,
            "recall": r,
            "f1": _f1(p, r),
        }
    for v in per_label.values():
        v["recall"] = _ratio(v["hit"], v["gold"])
    out["per_label"] = dict(sorted(per_label.items()))
    out["docs_without_gold_false_positive_docs"] = fp_docs
    out["docs"] = len(docs)
    return out


def run_tier1(docs: list[dict[str, Any]], node: str) -> dict[str, list[dict[str, Any]]]:
    preds = {}
    for d in docs:
        p = subprocess.run(
            [node, str(ROOT / "tools" / "detect_cli.mjs")],
            input=json.dumps({"text": d["text"]}, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        preds[d["id"]] = json.loads(p.stdout).get("spans") or []
    return preds


def run_tier2(docs: list[dict[str, Any]], gpu: str) -> dict[str, list[dict[str, Any]]]:
    return {d["id"]: _post(f"{gpu}/ner", {"text": d["text"]}).get("spans") or [] for d in docs}


# ---------------------------------------------------------------- 의도 분류
def _guard_metrics(rt: list[dict[str, Any]], refused: list[bool]) -> dict[str, Any]:
    exp = [x["expect_refuse"] for x in rt]
    n_ref = sum(exp)
    n_allow = len(exp) - n_ref
    ok = sum(1 for e, r in zip(exp, refused) if e and r)
    fr = sum(1 for e, r in zip(exp, refused) if not e and r)
    cats: dict[str, dict[str, int]] = {}
    for x, r in zip(rt, refused):
        c = cats.setdefault(x.get("category", "?"), {"n": 0, "refused": 0})
        c["n"] += 1
        c["refused"] += int(r)
    return {
        "refuse_expected": n_ref,
        "refused_correct": ok,
        "refusal_rate": _ratio(ok, n_ref),
        "allow_expected": n_allow,
        "false_refusals": fr,
        "false_refusal_rate": _ratio(fr, n_allow),
        "per_category": dict(sorted(cats.items())),
    }


def eval_intent(rt: list[dict[str, Any]], gpu: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "note": "운영 앱은 GPU /intent 결과만 쓰고, GPU 에 닿지 못할 때만 로컬 정규식으로 폴백한다.",
        "queries": len(rt),
    }
    local: list[bool] | None
    try:
        from app.retrieval import local_locate_intent

        local = [bool(local_locate_intent(x["q"])) for x in rt]
        out["local_regex"] = _guard_metrics(rt, local)
    except ImportError as exc:
        local = None
        out["local_regex"] = {"skipped": True, "reason": type(exc).__name__}
    if gpu is None:
        out["gpu"] = {"skipped": True, "reason": "--no-gpu"}
        return out
    try:
        res = [_post(f"{gpu}/intent", {"q": x["q"]}) for x in rt]
    except GpuDown as exc:
        out["gpu"] = {"skipped": True, "reason": str(exc)}
        return out
    g = [bool(r.get("locate_intent")) for r in res]
    out["gpu"] = _guard_metrics(rt, g)
    scores = [float(r.get("score", 0.0)) for r in res]
    out["gpu"]["score_min_refuse_expected"] = round(min(s for s, x in zip(scores, rt) if x["expect_refuse"]), 4)
    out["gpu"]["score_max_allow_expected"] = round(max(s for s, x in zip(scores, rt) if not x["expect_refuse"]), 4)
    if local is not None:
        out["gpu_or_local"] = _guard_metrics(rt, [a or b for a, b in zip(g, local)])
    return out


# ---------------------------------------------------------------- 근거 정확도
def eval_basis(knowledge: dict[str, Any]) -> dict[str, Any]:
    snap: dict[tuple[str, str], dict[str, Any]] = {}
    with (ROOT / "data" / "laws" / "articles.jsonl").open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            m = ARTICLE_BASE_RE.match(rec.get("article") or "")
            if m:
                snap[(rec.get("law"), m.group(1))] = rec
    total = matched = eff_ok = no_art = 0
    mismatched: set[str] = set()
    for item in knowledge.get("items", []):
        for b in item.get("basis") or []:
            art = b.get("article") or ""
            if not art:
                no_art += 1
                continue
            total += 1
            m = ARTICLE_BASE_RE.match(art)
            rec = snap.get((b.get("law"), m.group(1))) if m else None
            if rec is None:
                continue
            matched += 1
            if (b.get("effective") or "") == (rec.get("effective") or ""):
                eff_ok += 1
            else:
                mismatched.add(b.get("law") or "")
    return {
        "basis_total": total,
        "basis_in_snapshot": matched,
        "ratio": _ratio(matched, total),
        "effective_match": eff_ok,
        "effective_match_ratio": _ratio(eff_ok, matched),
        "mismatched_laws": sorted(mismatched),
        "basis_without_article": no_art,
        "snapshot_articles": len(snap),
    }


# ---------------------------------------------------------------- main
def _fmt(m: dict[str, Any] | None) -> str:
    if not m or m.get("skipped"):
        return f"skipped ({(m or {}).get('reason', '')})"
    s = m["strict_label_overlap"]
    return f"P={s['precision']} R={s['recall']} F1={s['f1']} (pred {s['pred']}, gold {s['gold']})"


def _fmt_g(m: dict[str, Any] | None) -> str:
    if not m or m.get("skipped"):
        return f"skipped ({(m or {}).get('reason', '')})"
    return (
        f"refusal {m['refusal_rate']} ({m['refused_correct']}/{m['refuse_expected']}) "
        f"false_refusal {m['false_refusal_rate']} ({m['false_refusals']}/{m['allow_expected']})"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gpu-url", default=os.environ.get("GPU_URL", "http://127.0.0.1:18000"))
    ap.add_argument("--no-gpu", action="store_true")
    ap.add_argument("--out", default="data/eval/results.json")
    ap.add_argument("--node", default="node")
    a = ap.parse_args()
    gpu = None if a.no_gpu else a.gpu_url.rstrip("/")

    knowledge = load_knowledge(ROOT / "data" / "knowledge.json")
    engine = Engine(knowledge)
    scenarios = _load("data/eval/scenarios.json")
    docs = _load("data/eval/docs.json")
    rt = _load("data/eval/redteam.json")

    res: dict[str, Any] = {
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "knowledge_version": engine.version,
        "gpu_url": gpu,
    }
    res["diagnose"] = eval_diagnose(engine, scenarios)

    t1 = run_tier1(docs, a.node)
    res["doccheck_tier1"] = eval_spans(docs, t1)
    t2 = None
    if gpu is None:
        res["doccheck_tier2"] = {"skipped": True, "reason": "--no-gpu"}
    else:
        try:
            t2 = run_tier2(docs, gpu)
            res["doccheck_tier2"] = eval_spans(docs, t2, TIER2_LABELS)
        except GpuDown as exc:
            res["doccheck_tier2"] = {"skipped": True, "reason": str(exc)}
    if t2 is not None:
        comb = {k: t1.get(k, []) + t2.get(k, []) for k in t1}
        res["doccheck_combined"] = eval_spans(docs, comb)
    else:
        res["doccheck_combined"] = {"skipped": True, "reason": "tier2 unavailable"}

    res["intent"] = eval_intent(rt, gpu)
    res["basis_accuracy"] = eval_basis(knowledge)

    out = ROOT / a.out
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    d = res["diagnose"]
    b = res["basis_accuracy"]
    print(f"diagnose.recall          {d['recall']} ({d['must_include_hit']}/{d['must_include_total']})")
    print(f"diagnose.misapplied      {d['misapplied']}")
    print(f"diagnose.pending/residual/na  {d['pending_hit']}/{d['pending_expected']}  "
          f"{d['residual_hit']}/{d['residual_expected']}  {d['not_applicable_hit']}/{d['not_applicable_expected']}")
    print(f"tier1.strict             {_fmt(res['doccheck_tier1'])}")
    print(f"tier2.strict             {_fmt(res['doccheck_tier2'])}")
    print(f"combined.strict          {_fmt(res['doccheck_combined'])}")
    print(f"intent.gpu               {_fmt_g(res['intent'].get('gpu'))}")
    print(f"intent.local_regex       {_fmt_g(res['intent'].get('local_regex'))}")
    print(f"intent.gpu_or_local      {_fmt_g(res['intent'].get('gpu_or_local'))}")
    print(f"basis.ratio              {b['ratio']} ({b['basis_in_snapshot']}/{b['basis_total']})")
    print(f"basis.effective_match    {b['effective_match_ratio']} ({b['effective_match']}/{b['basis_in_snapshot']})")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
