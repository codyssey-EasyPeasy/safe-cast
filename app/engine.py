"""지식표 기반 규칙 진단 엔진.

사용자(피해자 본인)의 피해 유형과 상황 flag 를 받아
체크리스트 / 잔여 위험 / 대상 아님 항목을 계산한다.
I/O 는 load_knowledge 에서만 일어난다(읽기 전용, 저장 없음).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:  # 패키지(app.engine)·단독 모듈(tools 가 sys.path 로 부를 때) 둘 다 지원
    from .humanize import prepare_items
except ImportError:  # pragma: no cover
    from humanize import prepare_items  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent

VICTIM_TYPES: tuple[str, ...] = ("family", "stalking", "dating", "sexual")

VICTIM_LABEL: dict[str, str] = {
    "family": "가정폭력",
    "stalking": "스토킹",
    "dating": "교제폭력",
    "sexual": "성폭력",
}

AREA_ORDER: tuple[str, ...] = (
    "행정",
    "소송·수사",
    "학적·보육",
    "차량·우편·통신",
    "디지털·위치",
    "피신·지원",
)

_EMPTY_KNOWLEDGE: dict[str, Any] = {"version": "0", "flags": [], "items": []}


def load_knowledge(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """지식표를 읽는다. 파일이 없거나 깨졌으면 빈 지식표를 돌려준다."""
    if path is None:
        env = os.environ.get("KNOWLEDGE_PATH")
        p = Path(env) if env else REPO_ROOT / "data" / "knowledge.json"
    else:
        p = Path(path)

    data: Any
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return dict(_EMPTY_KNOWLEDGE)

    if not isinstance(data, dict):
        return dict(_EMPTY_KNOWLEDGE)
    data.setdefault("version", "0")
    data.setdefault("flags", [])
    data.setdefault("items", [])
    return data


def _id_sort_key(item_id: str) -> tuple[str, int]:
    m = re.match(r"^([A-Za-z]+)(\d+)$", item_id or "")
    if m:
        return (m.group(1), int(m.group(2)))
    return (item_id or "", 0)


def _triggers_met(triggers: dict[str, Any], flags: set[str]) -> bool:
    all_list = triggers.get("all") or []
    any_list = triggers.get("any") or []
    if all_list and not all(f in flags for f in all_list):
        return False
    if any_list and not any(f in flags for f in any_list):
        return False
    return True


def _victim_applicable(item: dict[str, Any], victim_type: str) -> bool:
    victim_types = item.get("victim_types") or []
    return victim_type in victim_types or "any" in victim_types


def _area_index(area: str) -> int:
    try:
        return AREA_ORDER.index(area)
    except ValueError:
        return len(AREA_ORDER)


def _sort_key(item: dict[str, Any]) -> tuple[int, int, tuple[str, int]]:
    return (
        int(item.get("urgency") or 3),
        _area_index(item.get("area", "")),
        _id_sort_key(item.get("id", "")),
    )


def _not_applicable_reason(victim_type: str) -> str:
    label = VICTIM_LABEL.get(victim_type, victim_type)
    return f"이 피해 유형({label})에는 해당 제도가 없습니다"


class Engine:
    """순수 함수 규칙 엔진. 상태를 저장하지 않는다."""

    def __init__(self, knowledge: dict[str, Any]) -> None:
        self.knowledge = knowledge
        self.flags: list[dict[str, Any]] = list(knowledge.get("flags") or [])
        # 사용자 문구 다듬기(내부 id → 제도 이름, 함께 신청하는 항목의 통지 경고). app/humanize.py
        self.items: list[dict[str, Any]] = prepare_items(list(knowledge.get("items") or []))
        self.version: str = str(knowledge.get("version", "0"))

    def flag_ids(self) -> list[str]:
        return [f.get("id", "") for f in self.flags if f.get("id")]

    def unknown_flags(self, flags: list[str]) -> list[str]:
        known = set(self.flag_ids())
        return [f for f in flags if f not in known]

    def diagnose(self, victim_type: str, flags: list[str]) -> dict[str, Any]:
        flag_set = set(flags or [])
        checklist: list[dict[str, Any]] = []
        residual: list[dict[str, Any]] = []
        not_applicable: list[dict[str, Any]] = []

        for item in self.items:
            triggers = item.get("triggers") or {}
            if not _triggers_met(triggers, flag_set):
                continue

            status = item.get("status", "in_force")
            applicable = _victim_applicable(item, victim_type)

            if status in ("in_force", "pending"):
                if applicable:
                    copy = dict(item)
                    copy.pop("conditional_notes", None)
                    note_parts: list[str] = []
                    if status == "pending":
                        eff = item.get("pending_effective") or ""
                        note_parts.append(f"{eff} 시행 예정, 지금은 쓸 수 없음")
                    for cn in item.get("conditional_notes") or []:
                        if_any = cn.get("if_any") or []
                        if any(f in flag_set for f in if_any):
                            text = cn.get("note") or ""
                            if text:
                                note_parts.append(text)
                    if note_parts:
                        copy["note"] = " ".join(note_parts)
                    checklist.append(copy)
                else:
                    not_applicable.append(
                        {
                            "id": item.get("id", ""),
                            "measure": item.get("measure", ""),
                            "reason": _not_applicable_reason(victim_type),
                        }
                    )
            elif status == "none":
                if applicable:
                    entry = {
                        k: v
                        for k, v in item.items()
                        if k not in ("exposure", "limits", "conditional_notes")
                    }
                    entry["support"] = (
                        "혼자 대비하기 어려우면 1366(24시간)이나 법률구조공단 132에 상담하세요. "
                        "위험이 급하면 112."
                    )
                    residual.append(entry)

        non_pending = [c for c in checklist if c.get("status") != "pending"]
        pending = [c for c in checklist if c.get("status") == "pending"]
        non_pending.sort(key=_sort_key)
        pending.sort(key=_sort_key)
        checklist = non_pending + pending
        residual.sort(key=_sort_key)

        summary = {
            "checklist": len(checklist),
            "residual": len(residual),
            "not_applicable": len(not_applicable),
            "pending": sum(1 for c in checklist if c.get("status") == "pending"),
            "urgent": sum(
                1
                for c in checklist
                if c.get("urgency") == 1 and c.get("status") == "in_force"
            ),
            "notify_warnings": sum(1 for c in checklist if c.get("notify_warning")),
        }

        return {
            "checklist": checklist,
            "residual": residual,
            "not_applicable": not_applicable,
            "summary": summary,
        }
