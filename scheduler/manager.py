"""割り当て管理。画面から呼ばれる手順をまとめます。

画面は「案を作る」「修正を保存する」「確定する」「連絡文を作る」を呼ぶだけで、計算はしません。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime

from . import llm, messages, reasons, rules, solver
from .data_access import Repo, month_range
from .models import LABEL_PERIOD, PERIOD_LABEL, Assignment, Context, slot_id_of


@dataclass
class Review:
    """割り当て案のチェック結果(画面にそのまま出す)。"""

    must: list[rules.Finding]
    should: list[rules.Finding]
    warnings: list[rules.Finding]
    unplaced: list[str]


def review(assignments: list[Assignment], ctx: Context) -> Review:
    return Review(
        rules.check_must(assignments, ctx),
        rules.check_should(assignments, ctx),
        rules.check_warnings(assignments, ctx),
        solver.explain_unplaced(assignments, ctx),
    )


# ---------------------------------------------------------------- F4 / F6: 案を作る

def create_plan(repo: Repo, month: str) -> int:
    ctx = repo.load(month).ctx
    plan = solver.generate(ctx)
    reasons.fill_reasons(plan, ctx)
    repo.save_assignments(month, plan)
    repo.save_messages(month, [])
    repo.set_status(month, "draft")
    return len(plan)


def polish_reasons(repo: Repo, month: str, client: llm.LLMClient) -> bool:
    bundle = repo.load(month)
    plan = [Assignment(**vars(a)) for a in bundle.assignments]
    if not reasons.polish_reasons(plan, bundle.ctx, client):
        return False
    repo.save_assignments(month, plan)
    return True


# ---------------------------------------------------------------- F7: 修正と再チェック

def describe(a: Assignment, ctx: Context) -> str:
    slot = ctx.slots.get(a.slot_id)
    return (f"{slot.label if slot else a.slot_id} {ctx.course_name(a.course_id)} "
            f"{ctx.teacher_name(a.teacher_id)} {ctx.room_name(a.room_id)}教室")


def save_edits(repo: Repo, month: str, edited: list[dict], change_reason: str = "") -> int:
    """先生が直した表を保存する。変わった行は記録し、理由を付け直す。変更件数を返す。

    edited の各行: {"assignment_id", "slot_id", "course_id", "teacher_id", "room_id"}
    """
    bundle = repo.load(month)
    ctx = bundle.ctx
    before = {a.assignment_id: a for a in bundle.assignments}
    after: list[Assignment] = []
    changes: list[tuple[str, str]] = []

    for row in edited:
        old = before.get(row.get("assignment_id") or "")
        new = Assignment(
            old.assignment_id if old else solver.new_id(),
            row["slot_id"], row["course_id"], row["teacher_id"], row["room_id"],
            "draft", old.reason if old else "", old.manual if old else True,
        )
        if old is None:
            changes.append((new.assignment_id, f"追加:{describe(new, ctx)}"))
        elif (old.slot_id, old.course_id, old.teacher_id, old.room_id) != \
                (new.slot_id, new.course_id, new.teacher_id, new.room_id):
            new.manual = True
            changes.append((new.assignment_id, f"変更:{describe(old, ctx)} → {describe(new, ctx)}"))
        after.append(new)

    kept = {a.assignment_id for a in after}
    for aid, old in before.items():
        if aid not in kept:
            changes.append((aid, f"削除:{describe(old, ctx)}"))

    if changes:
        reasons.fill_reasons(after, ctx)
        repo.save_assignments(month, after)
        repo.add_changes(month, changes, change_reason)
        repo.save_messages(month, [])
        repo.set_status(month, "draft")
    return len(changes)


# ---------------------------------------------------------------- F14: 確定

def confirm(repo: Repo, month: str) -> list[rules.Finding]:
    """Must 違反がなければ確定して保存する。違反があればその一覧を返し、確定しない。"""
    bundle = repo.load(month)
    violations = rules.check_must(bundle.assignments, bundle.ctx)
    if violations or not bundle.assignments:
        return violations
    plan = [Assignment(**{**vars(a), "status": "confirmed"}) for a in bundle.assignments]
    repo.save_assignments(month, plan)
    repo.set_status(month, "confirmed")
    return []


def unconfirm(repo: Repo, month: str) -> None:
    bundle = repo.load(month)
    plan = [Assignment(**{**vars(a), "status": "draft"}) for a in bundle.assignments]
    repo.save_assignments(month, plan)
    repo.save_messages(month, [])
    repo.set_status(month, "draft")


# ---------------------------------------------------------------- F8: 連絡文

def create_messages(repo: Repo, month: str, sender: str = "", client: llm.LLMClient | None = None) -> list[str]:
    """確定した割り当てから連絡文の下書きを作って保存する。照合で見つかった食い違いを返す。"""
    bundle = repo.load(month)
    if bundle.status not in ("confirmed", "contacted"):
        raise ValueError("連絡文は、割り当てを確定してから作ります")
    drafts = messages.build_messages(bundle.assignments, bundle.ctx, sender, client)
    problems = messages.verify_messages(drafts, bundle.assignments, bundle.ctx)
    repo.save_messages(month, drafts)
    return problems


def mark_contacted(repo: Repo, month: str) -> None:
    bundle = repo.load(month)
    for m in bundle.messages:
        m.status = "sent"
    repo.save_messages(month, bundle.messages)
    repo.set_status(month, "contacted")


# ---------------------------------------------------------------- F1: 対応可能日の取り込み

def _norm_name(name: str) -> str:
    return "".join(str(name or "").split()).removesuffix("先生")


def _parse_day(text: str, month: str) -> date | None:
    text = str(text or "").strip().split("(")[0].split("(")[0].strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:  # 「12/6」「12月6日」のような年なしの書き方は、対象月の年とみなす
        m, d = text.replace("月", "/").replace("日", "").split("/")[:2]
        return date(int(month[:4]), int(m), int(d))
    except (ValueError, IndexError):
        return None


def _parse_period(text: str) -> list[str]:
    text = str(text or "").strip()
    if text in ("終日", "両方", "午前・午後", "午前午後"):
        return ["AM", "PM"]
    if text in LABEL_PERIOD:
        return [LABEL_PERIOD[text]]
    return [text.upper()] if text.upper() in ("AM", "PM") else []


def import_availability(repo: Repo, month: str, records: list[dict], replace: bool = True) -> tuple[int, list[str]]:
    """対応可能日を取り込む。records の各行: {"name", "day", "period"}。(取り込み件数, 注意点) を返す。"""
    bundle = repo.load(month)
    ctx = bundle.ctx
    by_name = {_norm_name(t.name): t.teacher_id for t in ctx.teachers.values()}
    first, last = month_range(month)
    pairs: set[tuple[str, str]] = set() if replace else set(ctx.availability)
    unknown: set[str] = set()
    no_slot: set[str] = set()
    bad = 0

    for rec in records:
        day = rec["day"] if isinstance(rec["day"], date) else _parse_day(rec["day"], month)
        periods = _parse_period(rec["period"])
        if day is None or not periods:
            bad += 1
            continue
        if not (first <= day <= last):
            continue
        teacher_id = by_name.get(_norm_name(rec["name"]))
        if teacher_id is None:
            unknown.add(str(rec["name"]))
            continue
        for period in periods:
            slot_id = slot_id_of(day, period)
            if slot_id in ctx.slots:
                pairs.add((teacher_id, slot_id))
            else:
                no_slot.add(f"{day.month}/{day.day} {PERIOD_LABEL[period]}")

    notes = []
    if unknown:
        notes.append("講師マスターにない名前を読み飛ばしました:" + "、".join(sorted(unknown)))
    if no_slot:
        notes.append("開催枠として登録されていない日時を読み飛ばしました:" + "、".join(sorted(no_slot)))
    if bad:
        notes.append(f"日付または時間帯を読み取れない行が{bad}件ありました")
    before = len(ctx.availability) if not replace else 0
    repo.save_availability(month, pairs)
    return len(pairs) - before, notes


def import_availability_from_form(repo: Repo, month: str) -> tuple[int, list[str]]:
    """Apps Script が書き込んだフォーム回答(availability_raw)から取り込む。"""
    first, last = month_range(month)
    raw = repo.read("availability_raw", "day >= ? AND day <= ?", [first, last])
    if not raw:
        return 0, ["この月のフォーム回答はまだ届いていません"]
    return import_availability(
        repo, month, [{"name": r["teacher_name"], "day": r["day"], "period": r["period"]} for r in raw])


def parse_availability_csv(text: str) -> list[dict]:
    """CSV(講師名, 日付, 時間帯)を読む。1行目が見出しなら読み飛ばす。"""
    out = []
    for i, row in enumerate(csv.reader(io.StringIO(text.lstrip("﻿")))):
        if len(row) < 3 or (i == 0 and _parse_day(row[1], "2000-01") is None):
            continue
        out.append({"name": row[0], "day": row[1], "period": row[2]})
    return out


# ---------------------------------------------------------------- 過去の割り当ての取り込み(検証用)

def import_assignments_csv(repo: Repo, month: str, text: str) -> tuple[int, list[str]]:
    """過去の実際の割り当て(日付, 時間帯, 体験授業, 講師, 教室)を取り込み、ルールをかけられるようにする。"""
    ctx = repo.load(month).ctx
    teachers = {_norm_name(t.name): t.teacher_id for t in ctx.teachers.values()}
    courses = {c.name.strip(): c.course_id for c in ctx.courses.values()}
    rooms = {name.strip().removesuffix("教室"): rid for rid, name in ctx.rooms.items()}
    plan: list[Assignment] = []
    notes: list[str] = []

    for i, row in enumerate(csv.reader(io.StringIO(text.lstrip("﻿"))), start=1):
        if len(row) < 5:
            continue
        day, periods = _parse_day(row[0], month), _parse_period(row[1])
        if day is None or len(periods) != 1:
            if i > 1:
                notes.append(f"{i}行目:日付または時間帯を読み取れません")
            continue
        slot_id = slot_id_of(day, periods[0])
        teacher_id = teachers.get(_norm_name(row[3]))
        course_id = courses.get(row[2].strip())
        room_id = rooms.get(row[4].strip().removesuffix("教室"))
        missing = [label for label, v in (("開催枠", slot_id in ctx.slots), ("体験授業", course_id),
                                          ("講師", teacher_id), ("教室", room_id)) if not v]
        if missing:
            notes.append(f"{i}行目:{'・'.join(missing)}が登録されていないため読み飛ばしました")
            continue
        plan.append(Assignment(solver.new_id(), slot_id, course_id, teacher_id, room_id, manual=True))

    if plan:
        reasons.fill_reasons(plan, ctx)
        repo.save_assignments(month, plan)
        repo.save_messages(month, [])
        repo.set_status(month, "draft")
    return len(plan), notes


def export_csv(assignments: list[Assignment], ctx: Context) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["日付", "時間帯", "体験授業", "講師", "教室", "理由"])
    for a in assignments:
        slot = ctx.slots.get(a.slot_id)
        if slot:
            w.writerow([slot.day.isoformat(), PERIOD_LABEL[slot.period], ctx.course_name(a.course_id),
                        ctx.teacher_name(a.teacher_id), ctx.room_name(a.room_id), a.reason])
    return "﻿" + buf.getvalue()
