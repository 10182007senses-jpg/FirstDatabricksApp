"""ルール判定(要件定義書 6章 R1〜R11)。

R11(工事中の代わりの教室)は Context.room_priority で、代わりの教室を元の教室と同じ扱いにしています。

純粋な関数だけで、DB にも画面にも触りません。
割り当て案の作成後(F5)、先生の修正後(F7)、過去データの検証(S1)で同じ判定を使います。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from .models import Assignment, Context, Slot

MUST = "must"
SHOULD = "should"
WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    rule: str  # "R1" など
    level: str  # must / should / warning
    message: str
    assignment_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------- 部品

def missing_software(ctx: Context, room_id: str, course_id: str, day: date) -> list[str]:
    """その日付の時点で、教室に入っていない必要ソフトの ID を返す(R2)。"""
    installed = {
        rs.software_id
        for rs in ctx.room_software
        if rs.room_id == room_id
        and (rs.valid_from is None or rs.valid_from <= day)
        and (rs.valid_to is None or day <= rs.valid_to)
    }
    return sorted(ctx.course_software.get(course_id, set()) - installed)


def stale_software(ctx: Context, room_id: str, course_id: str, day: date) -> list[str]:
    """判定に使ったソフト情報のうち、確認日が古い(または未確認の)ものを返す(R10)。"""
    needed = ctx.course_software.get(course_id, set())
    stale = []
    for rs in ctx.room_software:
        if rs.room_id != room_id or rs.software_id not in needed:
            continue
        if rs.confirmed_on is None or (day - rs.confirmed_on).days > ctx.settings.stale_days:
            stale.append(rs.software_id)
    return sorted(set(stale))


def genre_coverage(assignments: list[Assignment], ctx: Context) -> dict[tuple[str, str], int]:
    """(週, 系統) ごとの授業数。前月の確定分も含める。"""
    count: Counter = Counter()
    for p in ctx.previous:
        count[(p.week_key, p.genre_id)] += 1
    for a in assignments:
        slot = ctx.slots.get(a.slot_id)
        genre = ctx.genre_of(a.course_id)
        if slot and genre:
            count[(slot.week_key, genre)] += 1
    return dict(count)


def week_sequence(ctx: Context) -> tuple[list[str], set[str]]:
    """R5 で見る週の並びと、今月の週の集合。

    「続く2週」は体験授業を開催する週どうしで数える【要確認】。
    """
    current = {s.week_key for s in ctx.slots.values()}
    weeks = sorted(current | {p.week_key for p in ctx.previous})
    return weeks, current


def is_covered(coverage: dict, ctx: Context, week: str, genre_id: str) -> bool:
    """その週に系統が「入っている」か。代わりにできる系統が入っていれば入っているとみなす。"""
    if coverage.get((week, genre_id), 0) > 0:
        return True
    return any(coverage.get((week, sub), 0) > 0 for sub in ctx.substitutes.get(genre_id, ()))


def r5_gaps(coverage: dict, ctx: Context) -> list[tuple[str, str, str]]:
    """2週続けて空いている (系統, 前の週, 後の週) を返す。後の週が今月のものだけを見る。"""
    weeks, current = week_sequence(ctx)
    gaps = []
    for genre_id, genre in ctx.genres.items():
        if not genre.r5_target:
            continue
        for w1, w2 in zip(weeks, weeks[1:]):
            if w2 not in current:
                continue
            if not is_covered(coverage, ctx, w1, genre_id) and not is_covered(coverage, ctx, w2, genre_id):
                gaps.append((genre_id, w1, w2))
    return gaps


def free_rooms(ctx: Context, slot_id: str, used: set[str]) -> list[str]:
    return [r for r in ctx.rooms if r not in used and (r, slot_id) not in ctx.room_blocks]


def week_label(ctx: Context, week_key: str) -> str:
    days = sorted({s.day for s in ctx.slots.values() if s.week_key == week_key})
    days += sorted({p.day for p in ctx.previous if p.week_key == week_key})
    if not days:
        return week_key
    first, last = min(days), max(days)
    if first == last:
        return f"{first.month}/{first.day}の週"
    return f"{first.month}/{first.day}〜{last.month}/{last.day}の週"


# ---------------------------------------------------------------- Must

def check_must(assignments: list[Assignment], ctx: Context) -> list[Finding]:
    findings: list[Finding] = []
    by_room: dict[tuple[str, str], list[Assignment]] = defaultdict(list)
    by_teacher: dict[tuple[str, str], list[Assignment]] = defaultdict(list)

    for a in assignments:
        slot: Slot | None = ctx.slots.get(a.slot_id)
        ids = (a.assignment_id,)
        if slot is None or a.teacher_id not in ctx.teachers or a.course_id not in ctx.courses \
                or a.room_id not in ctx.rooms:
            findings.append(Finding("データ", MUST, "枠・講師・授業・教室のいずれかがマスターに登録されていません", ids))
            continue
        who = f"{slot.label} {ctx.teacher_name(a.teacher_id)}「{ctx.course_name(a.course_id)}」"
        by_room[(a.slot_id, a.room_id)].append(a)
        by_teacher[(a.slot_id, a.teacher_id)].append(a)

        if (a.teacher_id, a.slot_id) not in ctx.availability:
            findings.append(Finding("R1", MUST, f"{who}:講師の対応可能日ではありません", ids))
        if a.course_id not in ctx.teacher_courses.get(a.teacher_id, set()):
            findings.append(Finding("担当", MUST, f"{who}:この講師が担当できる授業として登録されていません", ids))
        missing = missing_software(ctx, a.room_id, a.course_id, slot.day)
        if missing:
            names = "・".join(ctx.software.get(s, s) for s in missing)
            findings.append(Finding(
                "R2", MUST, f"{who}:{ctx.room_name(a.room_id)}教室に必要なソフト({names})が入っていません", ids))
        if (a.room_id, a.slot_id) in ctx.room_blocks:
            why = ctx.room_blocks[(a.room_id, a.slot_id)] or "教務側の行事"
            findings.append(Finding(
                "R9", MUST, f"{who}:{ctx.room_name(a.room_id)}教室は「{why}」で使えません", ids))

    for (slot_id, room_id), items in by_room.items():
        if len(items) > 1:
            findings.append(Finding(
                "R3", MUST,
                f"{ctx.slots[slot_id].label}:{ctx.room_name(room_id)}教室に{len(items)}つの授業が重なっています",
                tuple(a.assignment_id for a in items)))
    for (slot_id, teacher_id), items in by_teacher.items():
        if len(items) > 1:
            findings.append(Finding(
                "R4", MUST,
                f"{ctx.slots[slot_id].label}:{ctx.teacher_name(teacher_id)}が同じ時間に{len(items)}コマ入っています",
                tuple(a.assignment_id for a in items)))
    return findings


# ---------------------------------------------------------------- Should

def unassigned_teachers(assignments: list[Assignment], ctx: Context) -> list[str]:
    """対応可能日を出したのに、今月1件も入っていない講師(R8)。"""
    offered = {t for (t, s) in ctx.availability if s in ctx.slots and t in ctx.teachers}
    placed = {a.teacher_id for a in assignments}
    return sorted(offered - placed, key=lambda t: ctx.teacher_name(t))


def r6_shortfalls(coverage: dict, ctx: Context) -> list[tuple[str, str, int, int]]:
    out = []
    for (week, genre_id), target in sorted(ctx.week_targets.items()):
        have = coverage.get((week, genre_id), 0)
        if target > 0 and have < target:
            out.append((week, genre_id, have, target))
    return out


def r10_full_slots(assignments: list[Assignment], ctx: Context) -> list[str]:
    """空き教室を残したい月(初期値は3月)で、空きが足りない枠。"""
    used: dict[str, set[str]] = defaultdict(set)
    for a in assignments:
        used[a.slot_id].add(a.room_id)
    out = []
    for slot_id, rooms in used.items():
        slot = ctx.slots.get(slot_id)
        if slot and slot.day.month in ctx.settings.reserve_months \
                and len(free_rooms(ctx, slot_id, rooms)) < ctx.settings.reserve_rooms:
            out.append(slot_id)
    return sorted(out)


def check_should(assignments: list[Assignment], ctx: Context) -> list[Finding]:
    findings: list[Finding] = []
    coverage = genre_coverage(assignments, ctx)

    for genre_id, w1, w2 in r5_gaps(coverage, ctx):
        findings.append(Finding(
            "R5", SHOULD,
            f"{ctx.genre_name(genre_id)}系が2週続けて空いています({week_label(ctx, w1)}・{week_label(ctx, w2)})"))
    for week, genre_id, have, target in r6_shortfalls(coverage, ctx):
        findings.append(Finding(
            "R6", SHOULD,
            f"{week_label(ctx, week)}:{ctx.genre_name(genre_id)}系が目安{target}コマに対して{have}コマです"))
    for teacher_id in unassigned_teachers(assignments, ctx):
        findings.append(Finding(
            "R8", SHOULD,
            f"{ctx.teacher_name(teacher_id)}は対応可能日を出していますが、今月の割り当てがありません"))
    for slot_id in r10_full_slots(assignments, ctx):
        findings.append(Finding(
            "R10", SHOULD, f"{ctx.slots[slot_id].label}:ソフト入れ替えに備えた空き教室が残っていません"))
    return findings


# ---------------------------------------------------------------- 警告

def check_warnings(assignments: list[Assignment], ctx: Context) -> list[Finding]:
    findings: list[Finding] = check_room_priority(assignments, ctx)
    seen: set[tuple[str, str]] = set()
    for a in assignments:
        slot = ctx.slots.get(a.slot_id)
        if slot is None:
            continue
        for software_id in stale_software(ctx, a.room_id, a.course_id, slot.day):
            if (a.room_id, software_id) in seen:
                continue
            seen.add((a.room_id, software_id))
            findings.append(Finding(
                "R10", WARNING,
                f"{ctx.room_name(a.room_id)}教室の{ctx.software.get(software_id, software_id)}は"
                f"確認日が古い(または未確認)です。機材担当に確認してください"))
    return findings


def check_room_priority(assignments: list[Assignment], ctx: Context) -> list[Finding]:
    """ルール一覧 No.1: 優先・予備教室以外に入れたときは、講師に動作確認をお願いする。"""
    findings: list[Finding] = []
    for a in assignments:
        slot = ctx.slots.get(a.slot_id)
        if slot is None or not ctx.course_rooms.get(a.course_id):
            continue  # 優先教室が登録されていない授業は判定しない
        if ctx.room_priority(a.course_id, a.room_id, slot.day) is None:
            findings.append(Finding(
                "教室", WARNING,
                f"{slot.label}「{ctx.course_name(a.course_id)}」:{ctx.room_name(a.room_id)}教室は優先・予備教室ではありません。"
                f"事前に講師へ動作確認を依頼してください", (a.assignment_id,)))
    return findings


def check_all(assignments: list[Assignment], ctx: Context) -> list[Finding]:
    return check_must(assignments, ctx) + check_should(assignments, ctx) + check_warnings(assignments, ctx)
