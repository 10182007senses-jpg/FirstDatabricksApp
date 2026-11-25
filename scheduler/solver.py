"""割り当て案の作成(F4)、代わりの案(F9)、組めない理由(F5)。

AI は使わず、プログラムだけで作ります。
- Must(R1〜R4・R9)を満たす候補だけを並べる
- Should(R5・R6・R8・R10)は重み付きの点数にして、点数が一番上がる候補から順に入れる
同じ入力なら毎回同じ案になります。
"""
from __future__ import annotations

import uuid
from collections import Counter, defaultdict

from . import rules
from .models import BACKUP, MAIN, Assignment, Context


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def should_penalty(assignments: list[Assignment], ctx: Context) -> int:
    """Should の未充足を重みで合計した点数(小さいほど良い)。"""
    s = ctx.settings
    coverage = rules.genre_coverage(assignments, ctx)
    return (
        s.w_r5 * len(rules.r5_gaps(coverage, ctx))
        + s.w_r6 * sum(t - h for _, _, h, t in rules.r6_shortfalls(coverage, ctx))
        + s.w_r8 * len(rules.unassigned_teachers(assignments, ctx))
        + s.w_r10 * len(rules.r10_full_slots(assignments, ctx))
    )


class _State:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.assignments: list[Assignment] = []
        self.rooms: dict[str, set[str]] = defaultdict(set)
        self.teachers: dict[str, set[str]] = defaultdict(set)
        self.courses: dict[str, list[str]] = defaultdict(list)
        self.teacher_count: Counter = Counter()  # 講師が入った日数
        self.teacher_days: dict[str, set] = defaultdict(set)
        self.coverage: Counter = Counter()
        for p in ctx.previous:
            self.coverage[(p.week_key, p.genre_id)] += 1
        # 教室の「つぶしの利きやすさ」: 使える授業が少ない教室から先に使う
        days = sorted({s.day for s in ctx.slots.values()})
        ref = days[0] if days else None
        self.versatility = {
            r: sum(1 for c in ctx.courses if ref and not rules.missing_software(ctx, r, c, ref))
            for r in ctx.rooms
        }

    def pick_room(self, slot_id: str, course_id: str) -> str | None:
        slot = self.ctx.slots[slot_id]
        options = [
            r for r in rules.free_rooms(self.ctx, slot_id, self.rooms[slot_id])
            if not rules.missing_software(self.ctx, r, course_id, slot.day)
        ]
        if not options:
            return None
        # ルール一覧 No.1: 体験授業一覧の「優先して入れる教室」→「予備教室」→ その他の順に使う
        rank = {MAIN: 0, BACKUP: 1}
        return min(options, key=lambda r: (
            rank.get(self.ctx.room_priority(course_id, r, slot.day), 2), self.versatility[r], self.ctx.room_name(r)))

    def add(self, slot_id: str, teacher_id: str, course_id: str, room_id: str) -> None:
        self.assignments.append(Assignment(new_id(), slot_id, course_id, teacher_id, room_id))
        self.rooms[slot_id].add(room_id)
        self.teachers[slot_id].add(teacher_id)
        self.courses[slot_id].append(course_id)
        self.teacher_days[teacher_id].add(self.ctx.slots[slot_id].day)
        self.teacher_count[teacher_id] = len(self.teacher_days[teacher_id])
        genre = self.ctx.genre_of(course_id)
        self.coverage[(self.ctx.slots[slot_id].week_key, genre)] += 1


# 「Myスクール授業」は、早見表で通常の体験授業(1日最大2つ選べる枠)とは別の列に載る別枠の授業。
# 体験授業一覧に一緒に載っているが、割り当て案には入れない
SEPARATE_PREFIXES = ("Myスクール",)


def is_regular_course(name: str) -> bool:
    return not "".join(name.split()).startswith(SEPARATE_PREFIXES)


def _candidates(ctx: Context) -> list[tuple[str, str, str]]:
    """R1 と「担当できる授業」を満たす (枠, 講師, 授業) の一覧。"""
    out = []
    for slot in ctx.sorted_slots():
        for teacher_id in sorted(ctx.teachers, key=lambda t: ctx.teacher_name(t)):
            if not ctx.teachers[teacher_id].active or (teacher_id, slot.slot_id) not in ctx.availability:
                continue
            for course_id in sorted(ctx.teacher_courses.get(teacher_id, ()), key=lambda c: ctx.course_name(c)):
                if course_id in ctx.courses and is_regular_course(ctx.course_name(course_id)):
                    out.append((slot.slot_id, teacher_id, course_id))
    return out


def _gain(st: _State, slot_id: str, teacher_id: str, course_id: str, room_id: str,
          gaps_now: int, gap_cache: dict) -> float:
    ctx, s = st.ctx, st.ctx.settings
    week = ctx.slots[slot_id].week_key
    genre = ctx.genre_of(course_id)
    n_slot = len(st.rooms[slot_id])
    gain = 0.0

    # R8: どの講師も月に1回
    if st.teacher_count[teacher_id] == 0:
        gain += s.w_r8
    gain -= s.w_balance * st.teacher_count[teacher_id]

    # R5 / F9: 2週続けて空く系統を減らす(代わりにできる系統でも減る)
    key = (week, genre)
    if key not in gap_cache:
        st.coverage[key] += 1
        gap_cache[key] = len(rules.r5_gaps(st.coverage, ctx))
        st.coverage[key] -= 1
    gain += s.w_r5 * (gaps_now - gap_cache[key])
    if genre in ctx.genres and ctx.genres[genre].r5_target and st.coverage[key] == 0:
        gain += s.w_r5 * 0.2  # 代わりの系統より、本来の系統が入るほうを少し優先

    # R6: 週ごとの目安
    target = ctx.week_targets.get(key)
    if target is not None and st.coverage[key] < target:
        gain += s.w_r6
    elif any(w == week for (w, _) in ctx.week_targets):
        gain -= s.w_r6 * 0.5

    # 枠を目安の数まで埋める。目安を超える分は R5・R6・R8 の得がある場合だけ
    gain += s.w_fill if n_slot < s.per_slot_target else -s.w_fill * 0.5
    if course_id in st.courses[slot_id]:
        gain -= s.w_fill * 2  # 同じ枠に同じ授業は並べない
    elif any(ctx.genre_of(c) == genre for c in st.courses[slot_id]):
        gain -= 1

    # R10: 3月は空き教室を残す
    if ctx.slots[slot_id].day.month in s.reserve_months:
        left = len(rules.free_rooms(ctx, slot_id, st.rooms[slot_id] | {room_id}))
        if left < s.reserve_rooms:
            gain -= s.w_r10
    return gain


def generate(ctx: Context) -> list[Assignment]:
    """1か月分の割り当て案を作る。Must を満たす候補しか入れない。"""
    st = _State(ctx)
    cands = _candidates(ctx)

    while True:
        gaps_now = len(rules.r5_gaps(st.coverage, ctx))
        gap_cache: dict = {}
        best, best_gain = None, 0.0
        for slot_id, teacher_id, course_id in cands:
            if teacher_id in st.teachers[slot_id] or len(st.rooms[slot_id]) >= ctx.settings.per_slot_max:
                continue
            room_id = st.pick_room(slot_id, course_id)
            if room_id is None:
                continue
            g = _gain(st, slot_id, teacher_id, course_id, room_id, gaps_now, gap_cache)
            if g > best_gain:
                best, best_gain = (slot_id, teacher_id, course_id, room_id), g
        if best is None:
            break
        st.add(*best)
        _add_other_period(st, *best)

    result = _swap_in_unassigned(st.assignments, ctx, cands)
    return sorted(result, key=lambda a: (ctx.slots[a.slot_id].day, a.slot_id, ctx.room_name(a.room_id)))


def _add_other_period(st: _State, slot_id: str, teacher_id: str, course_id: str, room_id: str) -> None:
    """講師は1日の午前と午後で、同じ授業を同じ教室で続けて受け持つのがふつう(実際の早見表の形)。
    同じ日のもう片方の枠にも対応できるなら、同じ授業・同じ教室で入れる。"""
    ctx = st.ctx
    slot = ctx.slots[slot_id]
    other = next((s.slot_id for s in ctx.slots.values() if s.day == slot.day and s.slot_id != slot_id), None)
    if other is None or (teacher_id, other) not in ctx.availability or teacher_id in st.teachers[other]:
        return
    if len(st.rooms[other]) >= ctx.settings.per_slot_max:
        return
    if room_id in st.rooms[other] or (room_id, other) in ctx.room_blocks:
        return
    st.add(other, teacher_id, course_id, room_id)


def _swap_in_unassigned(assignments: list[Assignment], ctx: Context, cands) -> list[Assignment]:
    """まだ入っていない講師を、2回以上入っている講師の枠と入れ替えられないか試す(R8)。"""
    current = list(assignments)
    for teacher_id in rules.unassigned_teachers(current, ctx):
        base = should_penalty(current, ctx)
        best, best_pen = None, base
        counts = Counter(a.teacher_id for a in current)
        for slot_id, t, course_id in cands:
            if t != teacher_id:
                continue
            slot = ctx.slots[slot_id]
            for i, a in enumerate(current):
                if a.slot_id != slot_id or counts[a.teacher_id] < 2:
                    continue
                if rules.missing_software(ctx, a.room_id, course_id, slot.day):
                    continue
                trial = current[:i] + current[i + 1:] + [
                    Assignment(new_id(), slot_id, course_id, teacher_id, a.room_id)]
                pen = should_penalty(trial, ctx)
                if pen < best_pen:
                    best, best_pen = trial, pen
        if best is not None:
            current = best
    return current


# ---------------------------------------------------------------- F5: 組めない理由

def explain_unplaced(assignments: list[Assignment], ctx: Context) -> list[str]:
    """入れられなかった講師・空のままの枠について、どのルールが原因かを返す。"""
    notes: list[str] = []
    used_rooms: dict[str, set[str]] = defaultdict(set)
    used_teachers: dict[str, set[str]] = defaultdict(set)
    for a in assignments:
        used_rooms[a.slot_id].add(a.room_id)
        used_teachers[a.slot_id].add(a.teacher_id)

    def blockers(teacher_id: str, slot_id: str) -> set[str]:
        slot = ctx.slots[slot_id]
        courses = [c for c in ctx.teacher_courses.get(teacher_id, ()) if c in ctx.courses]
        if not courses:
            return {"担当できる授業が登録されていない"}
        found: set[str] = set()
        for course_id in courses:
            ok_soft = [r for r in ctx.rooms if not rules.missing_software(ctx, r, course_id, slot.day)]
            if not ok_soft:
                found.add(f"「{ctx.course_name(course_id)}」に必要なソフトが入った教室がない(R2)")
                continue
            ok_block = [r for r in ok_soft if (r, slot_id) not in ctx.room_blocks]
            if not ok_block:
                found.add("使える教室が教務側の行事でふさがっている(R9)")
                continue
            if all(r in used_rooms[slot_id] for r in ok_block):
                found.add("使える教室が他の授業で埋まっている(R3)")
            elif len(used_rooms[slot_id]) >= ctx.settings.per_slot_max:
                found.add(f"1枠あたりの授業数の上限({ctx.settings.per_slot_max})に達している")
            else:
                found.add("他の講師・系統とのバランスを優先した")
        return found

    for teacher_id in rules.unassigned_teachers(assignments, ctx):
        slots = sorted(s for (t, s) in ctx.availability if t == teacher_id and s in ctx.slots)
        reasons: set[str] = set()
        for slot_id in slots:
            reasons |= blockers(teacher_id, slot_id)
        notes.append(
            f"{ctx.teacher_name(teacher_id)}を入れられませんでした"
            f"(対応可能{len(slots)}枠):{' / '.join(sorted(reasons))}")

    for slot in ctx.sorted_slots():
        if used_rooms[slot.slot_id]:
            continue
        offered = sorted(t for (t, s) in ctx.availability if s == slot.slot_id and t in ctx.teachers)
        if not offered:
            notes.append(f"{slot.label}は授業を入れられませんでした:対応可能な講師がいない(R1)")
            continue
        reasons = set()
        for teacher_id in offered:
            reasons |= blockers(teacher_id, slot.slot_id)
        notes.append(f"{slot.label}は授業を入れられませんでした:{' / '.join(sorted(reasons))}")
    return notes
