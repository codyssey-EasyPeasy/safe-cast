#!/usr/bin/env python3
"""Validate data/knowledge.json against docs/SPEC.md section 2."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

AREAS = {"행정", "소송·수사", "학적·보육", "차량·우편·통신", "디지털·위치", "피신·지원"}
PREFIX_AREA = {
    "A": "행정", "J": "소송·수사", "S": "학적·보육",
    "L": "차량·우편·통신", "D": "디지털·위치", "P": "피신·지원",
}
EXPECTED_IDS = (
    {f"A{i}" for i in range(1, 24)} | {f"J{i}" for i in range(1, 29)}  # J28: 조사 §3-12 잔여 위험
    | {f"S{i}" for i in range(1, 9)} | {f"L{i}" for i in range(1, 9)}
    | {f"D{i}" for i in range(1, 9)} | {f"P{i}" for i in range(1, 10)}
)
REQUIRED_KEYS = {
    "id", "area", "path", "exposure", "measure", "basis", "victim_types",
    "triggers", "urgency", "timing", "where", "documents", "limits",
    "notify_warning", "status", "pending_effective", "confidence",
    "plain", "plain_reviewed",
}
OPTIONAL_KEYS = {"conditional_notes"}  # [{"if_any": [flag], "note": str}]
STR_FIELDS = ("path", "exposure", "measure", "timing", "where", "documents", "limits")
VICTIM_TYPES = {"family", "stalking", "dating", "sexual", "any"}
STATUSES = {"in_force", "pending", "none"}
CONFIDENCES = {"확인", "부분확인", "미확인"}
FLAG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ARTICLE_BASE_RE = re.compile(r"^(제\d+조(?:의\d+)?)")
BANNED = (
    "조회하는 방법", "찾는 방법", "알아내는 방법", "알아내려면", "찾으려면",
    "위치를 찾", "주소를 찾", "추적하는 방법", "열람하려면", "발급받으려면",
)


def is_nonempty_str(v) -> bool:
    return isinstance(v, str) and len(v) > 0


class Validator:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def err(self, item_id: str, msg: str) -> None:
        self.errors.append(f"ERROR {item_id}: {msg}")

    def run(self, data, articles):  # noqa: C901
        flags = self._check_top(data)
        if flags is None:
            return
        items = data.get("items")
        if not isinstance(items, list):
            self.err("items", "items must be a list")
            return
        if len(items) != len(EXPECTED_IDS):
            self.err("items", f"expected {len(EXPECTED_IDS)} items, got {len(items)}")
        seen_ids: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                self.err("item", "item must be an object")
                continue
            iid = item.get("id") if isinstance(item.get("id"), str) else "<missing>"
            self._check_item(iid, item, flags, seen_ids)
        got = seen_ids
        missing = EXPECTED_IDS - got
        extra = got - EXPECTED_IDS
        if missing:
            self.err("ids", f"missing ids: {sorted(missing)}")
        if extra:
            self.err("ids", f"unexpected ids: {sorted(extra)}")
        if articles is not None:
            self._check_coverage(items, articles)

    def _check_top(self, data):
        if not isinstance(data, dict):
            self.err("root", "top-level must be an object")
            return None
        version = data.get("version")
        if not is_nonempty_str(version):
            self.err("version", "must be a non-empty string")
        flags_list = data.get("flags")
        flags: dict[str, dict] = {}
        if not isinstance(flags_list, list):
            self.err("flags", "flags must be a list")
        else:
            for i, f in enumerate(flags_list):
                fid = f.get("id") if isinstance(f, dict) else None
                label = str(fid) if isinstance(fid, str) else f"flag[{i}]"
                if not isinstance(f, dict):
                    self.err(label, "flag must be an object")
                    continue
                for k in ("id", "label", "group"):
                    if not is_nonempty_str(f.get(k)):
                        self.err(label, f"{k} must be non-empty string")
                if isinstance(fid, str):
                    if not FLAG_RE.match(fid):
                        self.err(label, f"id does not match pattern: {fid}")
                    if fid in flags:
                        self.err(label, f"duplicate flag id: {fid}")
                    else:
                        flags[fid] = f
        if not isinstance(data.get("items"), list):
            self.err("items", "items must be a list")
        return flags

    def _check_item(self, iid: str, item: dict, flags: dict, seen_ids: set) -> None:
        keys = set(item.keys())
        missing = REQUIRED_KEYS - keys
        extra = keys - REQUIRED_KEYS - OPTIONAL_KEYS
        if missing:
            self.err(iid, f"missing keys: {sorted(missing)}")
        if extra:
            self.err(iid, f"unexpected keys: {sorted(extra)}")
        if isinstance(item.get("id"), str):
            if item["id"] in seen_ids:
                self.err(iid, f"duplicate item id: {item['id']}")
            else:
                seen_ids.add(item["id"])
        area = item.get("area")
        if area not in AREAS:
            self.err(iid, f"invalid area: {area!r}")
        elif iid[0] in PREFIX_AREA and PREFIX_AREA[iid[0]] != area:
            self.err(iid, f"area {area!r} does not match prefix {iid[0]!r}")
        for field in STR_FIELDS:
            if not is_nonempty_str(item.get(field)):
                self.err(iid, f"{field} must be non-empty string")
        self._check_victims(iid, item.get("victim_types"))
        self._check_triggers(iid, item.get("triggers"), flags)
        urgency = item.get("urgency")
        if isinstance(urgency, bool) or urgency not in (1, 2, 3):
            self.err(iid, f"urgency invalid: {urgency!r}")
        status = item.get("status")
        pe = item.get("pending_effective")
        if status not in STATUSES:
            self.err(iid, f"status invalid: {status!r}")
        elif status == "pending":
            if not (isinstance(pe, str) and DATE_RE.match(pe)):
                self.err(iid, f"pending_effective must be YYYY-MM-DD: {pe!r}")
        else:
            if pe is not None:
                self.err(iid, f"pending_effective must be null for status {status}")
        if item.get("confidence") not in CONFIDENCES:
            self.err(iid, f"confidence invalid: {item.get('confidence')!r}")
        nw = item.get("notify_warning")
        if nw is not None and not is_nonempty_str(nw):
            self.err(iid, "notify_warning must be null or non-empty string")
        if not isinstance(item.get("plain"), str):
            self.err(iid, "plain must be a string")
        if not isinstance(item.get("plain_reviewed"), bool):
            self.err(iid, "plain_reviewed must be bool")
        self._check_basis(iid, item.get("basis"))
        self._check_safety(iid, item)

    def _check_victims(self, iid: str, vt) -> None:
        if not isinstance(vt, list) or not vt:
            self.err(iid, "victim_types must be non-empty list")
            return
        if len(set(map(id, vt))) != len(vt):
            # compare by equality too
            pass
        if len(set(vt)) != len(vt):
            self.err(iid, "victim_types contains duplicates")
        for v in vt:
            if v not in VICTIM_TYPES:
                self.err(iid, f"invalid victim_type: {v!r}")
        if "any" in vt and len(vt) > 1:
            self.err(iid, "victim_types 'any' must be the only element")

    def _check_triggers(self, iid: str, tr, flags: dict) -> None:
        if not isinstance(tr, dict):
            self.err(iid, "triggers must be an object")
            return
        if set(tr.keys()) != {"all", "any"}:
            self.err(iid, f"triggers keys must be exactly all,any: {sorted(tr.keys())}")
            return
        for key in ("all", "any"):
            lst = tr[key]
            if not isinstance(lst, list):
                self.err(iid, f"triggers.{key} must be a list")
                continue
            for v in lst:
                if not isinstance(v, str):
                    self.err(iid, f"triggers.{key} contains non-str: {v!r}")
                elif v not in flags:
                    self.err(iid, f"triggers.{key} unknown flag: {v}")
            if len(set(lst)) != len(lst):
                self.err(iid, f"triggers.{key} has duplicates")

    def _check_basis(self, iid: str, basis) -> None:
        if not isinstance(basis, list) or not basis:
            self.err(iid, "basis must be non-empty list")
            return
        primaries = 0
        for j, b in enumerate(basis):
            tag = f"basis[{j}]"
            if not isinstance(b, dict):
                self.err(iid, f"{tag} must be object")
                continue
            law = b.get("law")
            if not is_nonempty_str(law):
                self.err(iid, f"{tag}.law must be non-empty string")
            article = b.get("article")
            if not isinstance(article, str):
                self.err(iid, f"{tag}.article must be string")
            eff = b.get("effective")
            if not isinstance(eff, str) or (eff and not DATE_RE.match(eff)):
                self.err(iid, f"{tag}.effective must be '' or YYYY-MM-DD")
            url = b.get("url")
            if not isinstance(url, str) or (url and not url.startswith("https://")):
                self.err(iid, f"{tag}.url must be '' or https://")
            primary = b.get("primary")
            if not isinstance(primary, bool):
                self.err(iid, f"{tag}.primary must be bool")
            elif primary:
                primaries += 1
        if primaries < 1:
            self.err(iid, "basis needs at least one primary=true")

    def _check_safety(self, iid: str, item: dict) -> None:
        for field in ("exposure", "plain"):
            val = item.get(field)
            if not isinstance(val, str):
                continue
            for bad in BANNED:
                if bad in val:
                    self.err(iid, f"{field} contains banned substring: {bad!r}")

    def _check_coverage(self, items: list, articles: dict) -> None:
        total = matched = 0
        partial = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            for b in item.get("basis") or []:
                if not isinstance(b, dict):
                    continue
                art = b.get("article")
                law = b.get("law")
                if not isinstance(art, str) or not art:
                    continue
                total += 1
                m = ARTICLE_BASE_RE.match(art)
                if m and (law, m.group(1)) in articles:
                    matched += 1
        pct = (matched * 100 // total) if total else 0
        print(f"basis coverage: {matched}/{total} ({pct}%)")
        partial = sum(1 for r in articles.values() if r.get("partial") is True)
        print(f"articles with partial==true: {partial}")


def load_articles(path: Path, v: Validator) -> dict | None:
    if not path.exists():
        return None
    out: dict[tuple[str, str], dict] = {}
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            tag = f"articles:{lineno}"
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                v.err(tag, f"invalid JSON: {exc.msg}")
                continue
            if not isinstance(rec, dict):
                v.err(tag, "record must be an object")
                continue
            for k in ("law", "article", "text"):
                if not is_nonempty_str(rec.get(k)):
                    v.err(tag, f"{k} must be non-empty string")
            law, art = rec.get("law"), rec.get("article")
            if isinstance(law, str) and isinstance(art, str):
                m = ARTICLE_BASE_RE.match(art)
                if m:
                    out[(law, m.group(1))] = rec
            if rec.get("partial") is True:
                pass
    return out


def print_summary(items: list) -> None:
    areas: Counter = Counter()
    statuses: Counter = Counter()
    urgencies: Counter = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        areas[it.get("area")] += 1
        statuses[it.get("status")] += 1
        urgencies[it.get("urgency")] += 1
    print("summary:")
    print("  by area:", dict(sorted(areas.items(), key=lambda kv: str(kv[0]))))
    print("  by status:", dict(sorted(statuses.items(), key=lambda kv: str(kv[0]))))
    print("  by urgency:", dict(sorted(urgencies.items(), key=lambda kv: str(kv[0]))))


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate knowledge.json")
    ap.add_argument("--knowledge", default=str(REPO / "data/knowledge.json"))
    ap.add_argument("--articles", default=str(REPO / "data/laws/articles.jsonl"))
    args = ap.parse_args()

    v = Validator()
    kpath = Path(args.knowledge)
    try:
        data = json.loads(kpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR root: cannot read {kpath}: {exc}")
        return 1
    articles = load_articles(Path(args.articles), v)
    v.run(data, articles if articles is not None else None)
    for line in v.errors:
        print(line)
    items = data.get("items") if isinstance(data, dict) else []
    if isinstance(items, list):
        print_summary(items)
    print(f"errors: {len(v.errors)}")
    return 1 if v.errors else 0


if __name__ == "__main__":
    sys.exit(main())
