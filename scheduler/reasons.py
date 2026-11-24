"""割り当て理由の説明(F6)。

理由の「中身」(事実)はプログラムが割り当て結果から組み立てます。
LLM が使えるときは、その事実を先生が読みやすい文に整えるだけです。
LLM が使えない・失敗したときは、事実をつなげた文をそのまま使います。
"""
from __future__ import annotations

import json
from collections import Counter

from . import llm, rules
from .models import MAIN, PERIOD_LABEL, Assignment, Context


def build_facts(a: Assignment, assignments: list[Assignment], ctx: Context) -> list[str]:
    slot = ctx.slots.get(a.slot_id)
    if slot is None or a.course_id not in ctx.courses:
        return []
    facts: list[str] = []
    genre = ctx.genre_of(a.course_id)
    week = slot.week_key

    # 講師の都合(R1)
    offered = sorted(s for (t, s) in ctx.availability if t == a.teacher_id and s in ctx.slots)
    same_day = [s for s in offered if ctx.slots[s].day == slot.day]
    if len(offered) == 1:
        facts.append("講師の対応可能枠がこの枠のみ")
    elif len(same_day) == 1:
        facts.append(f"講師がこの日は{PERIOD_LABEL[slot.period]}のみ対応可")
    else:
        facts.append(f"講師の対応可能{len(offered)}枠のうちの1つ")

    # 教室(R2・R3・R9)
    fit = [r for r in ctx.rooms if not rules.missing_software(ctx, r, a.course_id, slot.day)]
    soft = "・".join(ctx.software.get(s, s) for s in sorted(ctx.course_software.get(a.course_id, ())))
    lacking = rules.missing_software(ctx, a.room_id, a.course_id, slot.day)
    if lacking:
        names = "・".join(ctx.software.get(x, x) for x in lacking)
        facts.append(f"{ctx.room_name(a.room_id)}教室には{names}が入っていない(R2に違反)")
    elif soft and fit == [a.room_id]:
        facts.append(f"{soft}が入っている教室は{ctx.room_name(a.room_id)}のみ")
    elif soft:
        facts.append(f"{soft}が入っている教室から選択")
    priority = ctx.room_priority(a.course_id, a.room_id, slot.day)
    alt = [x for x in ctx.room_alternatives if x.alt_room_id == a.room_id
           and a.room_id not in ctx.course_rooms.get(a.course_id, {})]
    if priority and alt:
        facts.append(f"{ctx.room_name(alt[0].room_id)}教室の代わりの教室(R11)")
    elif priority == MAIN:
        facts.append("体験授業一覧の優先教室")
    elif priority:
        facts.append("優先教室が埋まっているため予備教室を使用")
    other = [x for x in assignments if x.room_id == a.room_id and x.slot_id != a.slot_id
             and x.slot_id in ctx.slots and ctx.slots[x.slot_id].day == slot.day]
    if other:
        facts.append(
            f"{PERIOD_LABEL[ctx.slots[other[0].slot_id].period]}の「{ctx.course_name(other[0].course_id)}」と"
            f"同じ教室を使い分け")

    # 講師の回数(R8)
    count = Counter(x.teacher_id for x in assignments)
    if count[a.teacher_id] == 1:
        facts.append("この講師の今月の割り当てを確保(R8)")

    # 系統(R5・F9)
    coverage = rules.genre_coverage(assignments, ctx)
    without = [x for x in assignments if x.assignment_id != a.assignment_id]
    gaps_without = set(rules.r5_gaps(rules.genre_coverage(without, ctx), ctx))
    if any(g == genre for g, _, _ in gaps_without - set(rules.r5_gaps(coverage, ctx))):
        facts.append(f"{ctx.genre_name(genre)}系が2週続けて空かないようにするため(R5)")
    for g, subs in sorted(ctx.substitutes.items()):
        if genre in subs and g in ctx.genres and ctx.genres[g].r5_target and coverage.get((week, g), 0) == 0:
            facts.append(
                f"{ctx.genre_name(g)}系の授業を入れられない週のため、"
                f"近い系統の{ctx.genre_name(genre)}系で代わりにした(F9)")

    # 週の目安(R6)
    target = ctx.week_targets.get((week, genre))
    if target:
        facts.append(f"この週の目安({ctx.genre_name(genre)}系{target}コマ)に合わせた(R6)")

    seen: set[str] = set()
    return [f for f in facts if not (f in seen or seen.add(f))]


def template_reason(a: Assignment, facts: list[str]) -> str:
    text = "。".join(facts)
    return f"先生が手動で修正。{text}" if a.manual else text


def fill_reasons(assignments: list[Assignment], ctx: Context) -> None:
    """全行の理由を、プログラムが組み立てた文で埋める。"""
    for a in assignments:
        a.reason = template_reason(a, build_facts(a, assignments, ctx))


def polish_reasons(assignments: list[Assignment], ctx: Context, client: llm.LLMClient) -> bool:
    """LLM で理由を読みやすい文にする。成功したら True。講師名は LLM に渡さない。"""
    if not client.available or not assignments:
        return False
    items = []
    for a in assignments:
        slot = ctx.slots.get(a.slot_id)
        if slot is None:
            continue
        items.append({
            "id": a.assignment_id,
            "枠": slot.label,
            "授業": ctx.course_name(a.course_id),
            "講師": ctx.teacher_name(a.teacher_id),
            "教室": ctx.room_name(a.room_id),
            "事実": build_facts(a, assignments, ctx),
            "手動修正": a.manual,
        })
    system = (
        "あなたは専門学校の広報担当を助けるアシスタントです。"
        "オープンキャンパスの体験授業の割り当てについて、各行の「事実」だけをもとに、"
        "なぜこの割り当てになったかを先生が読んですぐ分かる日本語1〜2文で説明してください。"
        "事実にないことは書かないでください。(R5) などのルール番号は残してください。"
        '出力は {"id": "説明文", ...} の JSON だけにしてください。'
    )
    names = [t.name for t in ctx.teachers.values()]
    answer = client.complete(system, json.dumps(items, ensure_ascii=False), names)
    if not answer:
        return False
    try:
        start, end = answer.index("{"), answer.rindex("}") + 1
        parsed = json.loads(answer[start:end])
    except (ValueError, json.JSONDecodeError):
        return False
    done = 0
    for a in assignments:
        text = parsed.get(a.assignment_id)
        if isinstance(text, str) and text.strip():
            a.reason = text.strip()
            done += 1
    return done > 0
