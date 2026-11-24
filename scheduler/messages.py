"""連絡文の下書き作成(F8)。

日付・授業・教室の部分(日程ブロック)は、確定した割り当てからプログラムが差し込みます。
LLM に頼むのは前後のあいさつ文だけで、日程ブロックは [[SCHEDULE]] という目印のまま渡します。
こうすることで「割り当てと連絡文の食い違い」が起きません。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import llm
from .models import Assignment, Context
from .solver import new_id

PLACEHOLDER = "[[SCHEDULE]]"
KIND_LABEL = {"notice": "対応日のお知らせ", "thanks": "お礼と来月のお願い", "hp": "HP業者への連絡"}


@dataclass
class Message:
    message_id: str
    teacher_id: str | None  # HP業者宛ては None
    kind: str  # notice / thanks / hp
    subject: str
    body: str
    status: str = "draft"


def _month_label(ctx: Context) -> str:
    return f"{int(ctx.target_month[5:7])}月"


def _ordered(assignments: list[Assignment], ctx: Context) -> list[Assignment]:
    return sorted(assignments, key=lambda a: (ctx.slots[a.slot_id].day, ctx.slots[a.slot_id].period))


def teacher_schedule(teacher_id: str, assignments: list[Assignment], ctx: Context) -> list[str]:
    mine = [a for a in assignments if a.teacher_id == teacher_id and a.slot_id in ctx.slots]
    return [
        f"・{ctx.slots[a.slot_id].label} {ctx.course_name(a.course_id)}({ctx.room_name(a.room_id)}教室)"
        for a in _ordered(mine, ctx)
    ]


def hp_schedule(assignments: list[Assignment], ctx: Context) -> list[str]:
    rows = [a for a in assignments if a.slot_id in ctx.slots]
    def event(a: Assignment) -> str:
        name = ctx.event_name(ctx.slots[a.slot_id].day)
        return f"【{name}】" if name else ""

    return [
        f"・{ctx.slots[a.slot_id].label} {event(a)}{ctx.course_name(a.course_id)}"
        f"(担当:{ctx.teacher_name(a.teacher_id)})"
        for a in _ordered(rows, ctx)
    ]


def _templates(ctx: Context, sender: str) -> dict[str, tuple[str, str]]:
    m = _month_label(ctx)
    sign = f"広報 {sender}" if sender else "広報担当"
    return {
        "notice": (
            f"【体験授業】{m}のご担当日のお知らせ",
            "{name}先生\n\nお疲れさまです。" + sign + "です。\n"
            f"{m}のオープンキャンパス体験授業について、日程のご回答をありがとうございました。\n"
            "下記の日程でご担当をお願いいたします。\n\n"
            f"{PLACEHOLDER}\n\n"
            "ご都合が悪くなった場合や、内容に誤りがある場合はお知らせください。\n"
            "どうぞよろしくお願いいたします。\n\n" + sign,
        ),
        "thanks": (
            f"【体験授業】{m}の日程ご回答のお礼",
            "{name}先生\n\nお疲れさまです。" + sign + "です。\n"
            f"{m}のオープンキャンパス体験授業について、日程のご回答をありがとうございました。\n"
            "今回は教室や他の授業との兼ね合いで、ご担当をお願いできる枠をご用意できませんでした。\n"
            "せっかくご回答いただいたのに申し訳ありません。\n"
            "来月はぜひお願いしたいと考えておりますので、改めて日程を伺わせてください。\n"
            "どうぞよろしくお願いいたします。\n\n" + sign,
        ),
        "hp": (
            f"【体験授業】{m}の開催内容のご連絡",
            "ご担当者様\n\nいつもお世話になっております。" + sign + "です。\n"
            f"{m}のオープンキャンパス体験授業の内容が決まりましたので、HPへの掲載をお願いいたします。\n\n"
            f"{PLACEHOLDER}\n\n"
            "ご不明な点がありましたらお知らせください。\n"
            "どうぞよろしくお願いいたします。\n\n" + sign,
        ),
    }


def _polish(body: str, ctx: Context, client: llm.LLMClient | None) -> str:
    """あいさつ文だけを LLM で整える。日程の目印が欠けたら元の文を使う。"""
    if client is None or not client.available:
        return body
    system = (
        "次の連絡文の下書きを、専門学校の広報担当が同僚の講師や取引先に送る、"
        "丁寧で簡潔な日本語のメールとして整えてください。"
        f"{PLACEHOLDER} という目印は日程の表に置き換わるので、1回だけ、そのままの形で残してください。"
        "日付・教室・授業名などの新しい情報は足さないでください。本文だけを出力してください。"
    )
    answer = client.complete(system, body, [t.name for t in ctx.teachers.values()], max_tokens=1000)
    if not answer or answer.count(PLACEHOLDER) != body.count(PLACEHOLDER) \
            or answer.count("{name}") != body.count("{name}"):
        return body
    return answer.strip()


def build_messages(assignments: list[Assignment], ctx: Context, sender: str = "",
                   client: llm.LLMClient | None = None) -> list[Message]:
    """確定した割り当てから、講師ごと・HP業者宛ての下書きを作る。"""
    # あいさつ文は種類ごとに1回だけ整える(講師名と日程はそのあとで差し込む)
    templates = {k: (subj, _polish(body, ctx, client)) for k, (subj, body) in _templates(ctx, sender).items()}
    out: list[Message] = []
    placed = {a.teacher_id for a in assignments}
    offered = {t for (t, s) in ctx.availability if s in ctx.slots and t in ctx.teachers}

    for teacher_id in sorted(placed | offered, key=lambda t: ctx.teacher_name(t)):
        kind = "notice" if teacher_id in placed else "thanks"
        subject, body = templates[kind]
        body = body.replace("{name}", ctx.teacher_name(teacher_id).removesuffix("先生"))
        body = body.replace(PLACEHOLDER, "\n".join(teacher_schedule(teacher_id, assignments, ctx)))
        out.append(Message(new_id(), teacher_id, kind, subject, body))

    if assignments:
        subject, body = templates["hp"]
        body = body.replace(PLACEHOLDER, "\n".join(hp_schedule(assignments, ctx)))
        out.append(Message(new_id(), None, "hp", subject, body))
    return out


def verify_messages(messages: list[Message], assignments: list[Assignment], ctx: Context) -> list[str]:
    """確定した割り当てと連絡文を照合する。食い違いがあれば内容を返す(目標は0件)。"""
    problems: list[str] = []
    by_teacher = {m.teacher_id: m for m in messages if m.kind in ("notice", "thanks")}
    placed = {a.teacher_id for a in assignments}
    all_lines = {t: teacher_schedule(t, assignments, ctx) for t in placed}

    for teacher_id in placed:
        msg = by_teacher.get(teacher_id)
        if msg is None or msg.kind != "notice":
            problems.append(f"{ctx.teacher_name(teacher_id)}宛ての対応日のお知らせがありません")
            continue
        for line in all_lines[teacher_id]:
            if line not in msg.body:
                problems.append(f"{ctx.teacher_name(teacher_id)}宛て:「{line[1:]}」が連絡文にありません")
        if msg.body.count("\n・") != len(all_lines[teacher_id]):
            problems.append(f"{ctx.teacher_name(teacher_id)}宛て:連絡文の日程の数が割り当てと合いません")
    for teacher_id, msg in by_teacher.items():
        if msg.kind == "notice" and teacher_id not in placed:
            problems.append(f"{ctx.teacher_name(teacher_id)}宛て:割り当てがないのに対応日のお知らせがあります")

    hp = [m for m in messages if m.kind == "hp"]
    if assignments and not hp:
        problems.append("HP業者宛ての連絡文がありません")
    for m in hp:
        for line in hp_schedule(assignments, ctx):
            if line not in m.body:
                problems.append(f"HP業者宛て:「{line[1:]}」が連絡文にありません")
        if m.body.count("\n・") != len(assignments):
            problems.append("HP業者宛て:連絡文の日程の数が割り当てと合いません")
    return problems
