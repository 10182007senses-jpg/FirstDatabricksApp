"""割り当て管理。画面から呼ばれる手順をまとめます。

画面は「案を作る」「修正を保存する」「確定する」「連絡文を作る」を呼ぶだけで、計算はしません。
"""
from __future__ import annotations

import csv
import re
import io
from dataclasses import dataclass
from datetime import date, datetime

import json

from . import llm, messages, reasons, rules, sheet_import, solver
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

# 名前の異体字(フォームとマスターで書き方が違うことが多い)
_KANJI = str.maketrans({"齋": "斎", "齊": "斎", "斉": "斎", "髙": "高", "﨑": "崎", "邊": "辺", "邉": "辺", "濵": "浜",
                        "濱": "浜", "廣": "広", "德": "徳"})
_COMPANY = re.compile(r"(有限会社|株式会社|合同会社|一般社団法人|\(有\)|\(株\)|（有）|（株）)")


def _norm_name(name: str) -> str:
    """名前を照合用にそろえる。空白・「先生」・会社の種類・「_」から後ろのメモを落とし、異体字をそろえる。"""
    text = "".join(str(name or "").split()).split("_")[0].removesuffix("先生")
    return _COMPANY.sub("", text).translate(_KANJI)


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


def _match_name(name: str, by_name: dict[str, str], ambiguous: set[str]) -> str | None:
    """フォームの名前(フルネーム)と、マスターの名前(名字だけのことが多い)を照合する。"""
    if name in by_name:
        return by_name[name]
    hits = {tid for key, tid in by_name.items() if key and (name.startswith(key) or key.startswith(name))}
    # 「会社名+担当者名」と書かれていて、会社名のあとが別の講師の名前なら、その講師にする
    for key, tid in by_name.items():
        rest = name[len(key):] if key and name.startswith(key) else ""
        inner = {t for k, t in by_name.items() if k and rest and (rest.startswith(k) or k.startswith(rest))}
        if len(inner) == 1:
            return inner.pop()
    if not hits:  # 「(マスターにない会社名)+担当者名」のように、後ろに講師の名前があるとき
        hits = {tid for key, tid in by_name.items() if len(key) >= 2 and name.endswith(key)}
    if len(hits) == 1:
        return hits.pop()
    if len(hits) > 1:
        ambiguous.add(name)
    return None


def import_availability(repo: Repo, month: str, records: list[dict], replace: bool = True) -> tuple[int, list[str]]:
    """対応可能日を取り込む。records の各行: {"name", "day", "period", "email"(任意)}。

    講師はメールアドレスで照合し、なければ名前で照合する(フォームの名前は自由記述で表記が揺れるため)。
    名前で照合できた講師のメールアドレスは覚えておき、次回からはメールアドレスで照合する。
    (取り込み件数, 注意点) を返す。
    """
    bundle = repo.load(month)
    ctx = bundle.ctx
    by_name = {_norm_name(t.name): t.teacher_id for t in ctx.teachers.values()}
    by_email = {t.email.lower(): t.teacher_id for t in ctx.teachers.values() if t.email}
    learned: dict[str, str] = {}
    ambiguous: set[str] = set()
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
        email = str(rec.get("email") or "").strip().lower()
        teacher_id = by_email.get(email) if email else None
        if teacher_id is None:
            teacher_id = _match_name(_norm_name(rec["name"]), by_name, ambiguous)
            if teacher_id and email and not ctx.teachers[teacher_id].email:
                learned[teacher_id] = email
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
    if ambiguous:
        notes.append("同じ名字の講師が複数いて決められなかった名前:" + "、".join(sorted(ambiguous))
                     + "。マスターの講師名をフルネームにするか、メールアドレスを登録してください")
    unknown -= ambiguous
    if unknown:
        notes.append("講師マスターにない名前を読み飛ばしました:" + "、".join(sorted(unknown)))
    if no_slot:
        notes.append("開催枠として登録されていない日時を読み飛ばしました:" + "、".join(sorted(no_slot)))
    if bad:
        notes.append(f"日付または時間帯を読み取れない行が{bad}件ありました")
    before = len(ctx.availability) if not replace else 0
    if learned:
        rows = repo.read("teacher")
        for r in rows:
            if r["teacher_id"] in learned:
                r["email"] = learned[r["teacher_id"]]
        repo.replace("teacher", rows)
    repo.save_availability(month, pairs)
    return len(pairs) - before, notes


def import_availability_from_form(repo: Repo, month: str) -> tuple[int, list[str]]:
    """Apps Script が書き込んだフォーム回答(availability_raw)から取り込む。"""
    first, last = month_range(month)
    raw = repo.read("availability_raw", "day >= ? AND day <= ?", [first, last])
    if not raw:
        return 0, ["この月のフォーム回答はまだ届いていません"]
    return import_availability(
        repo, month, [{"name": r["teacher_name"], "day": r["day"], "period": r["period"],
                       "email": r.get("teacher_email")} for r in raw])


def import_availability_from_book(repo: Repo, month: str, book: dict[str, list[list]]) -> tuple[int, list[str]]:
    """フォームの回答のスプレッドシート(Excel)から取り込む。"""
    rows = sheet_import.pick_sheet(book, sheet_import.FORM_SHEET) or next(iter(book.values()), [])
    records, notes = sheet_import.parse_form_rows(rows, month)
    n, more = import_availability(repo, month, records)
    return n, notes + more


# ---------------------------------------------------------------- F2: 調整共有シートからマスターを取り込む

def import_masters(repo: Repo, course_rows: list[list], room_rows: list[list]) -> tuple[dict[str, int], list[str]]:
    existing = {t: repo.read(t) for t in ("course", "teacher")}
    result = sheet_import.parse_shared_sheet(course_rows, room_rows, existing)
    for table, rows in result.tables.items():
        repo.replace(table, rows)
    return {t: len(r) for t, r in result.tables.items()}, result.notes


def import_masters_from_book(repo: Repo, book: dict[str, list[list]]) -> tuple[dict[str, int], list[str]]:
    course_rows = sheet_import.pick_sheet(book, sheet_import.COURSE_SHEET)
    room_rows = sheet_import.pick_sheet(book, sheet_import.ROOM_SHEET)
    if course_rows is None or room_rows is None:
        raise ValueError("「体験授業一覧」と「教室」のシートが見つかりません。調整共有シートを選んでください")
    return import_masters(repo, course_rows, room_rows)


def import_masters_from_sheet_raw(repo: Repo) -> tuple[dict[str, int], list[str]]:
    """Apps Script が sheet_raw に書き込んだ調整共有シートの行から取り込む。"""
    book: dict[str, list] = {}
    for r in sorted(repo.read("sheet_raw"), key=lambda r: (r["sheet_name"], r["row_no"] or 0)):
        book.setdefault(r["sheet_name"], []).append(json.loads(r["cells_json"] or "[]"))
    if not book:
        raise ValueError("調整共有シートの内容がまだ届いていません(Apps Script の設定を確認してください)")
    return import_masters_from_book(repo, book)


def parse_availability_csv(text: str) -> list[dict]:
    """CSV(講師名, 日付, 時間帯)を読む。1行目が見出しなら読み飛ばす。"""
    out = []
    for i, row in enumerate(csv.reader(io.StringIO(text.lstrip("﻿")))):
        if len(row) < 3 or (i == 0 and _parse_day(row[1], "2000-01") is None):
            continue
        out.append({"name": row[0], "day": row[1], "period": row[2]})
    return out


# ---------------------------------------------------------------- 過去の早見表(実績)の取り込み

def import_history_from_book(repo: Repo, book: dict[str, list[list]]) -> tuple[int, list[str]]:
    """体験授業早見表の Excel の各月のシートから、実際の割り当てを取り込む(取り込んだ月は置き換える)。

    案を作るとき「その講師がいつも受け持つ授業」「よく開く授業」を優先するのに使う。
    """
    teachers = repo.read("teacher")
    by_name = {_norm_name(t["name"]): t["teacher_id"] for t in teachers}
    courses = {c["course_id"]: c["name"] for c in repo.read("course")}
    rooms = {sheet_import.norm(r["name"]).lower(): r["room_id"] for r in repo.read("room")}
    titles = [t for t in book if sheet_import.is_quickref_sheet(t)]
    if not titles:
        raise ValueError("「202611」のような年月の名前のシートが見つかりません。体験授業早見表を選んでください")
    total, unknown_t, unknown_c = 0, set(), set()
    for title in sorted(titles):
        rows = []
        for rec in sheet_import.parse_quickref(book[title], title):
            tid = _match_name(_norm_name(rec["teacher"]), by_name, set())
            cid = sheet_import.match_course(rec["course"], courses)
            if tid is None:
                unknown_t.add(rec["teacher"])
            if cid is None:
                unknown_c.add(rec["course"])
            if tid and cid:
                rows.append({"day": rec["day"], "period": rec["period"], "course_id": cid, "teacher_id": tid,
                             "room_id": rooms.get(sheet_import.norm(rec["room"]).lower())})
        ym = title.strip()
        first, last = month_range(f"{ym[:4]}-{ym[4:]}")
        repo.replace("history", rows, "day >= ? AND day <= ?", [first, last])
        total += len(rows)
    notes = []
    if unknown_t:
        notes.append(f"講師マスターと照合できない講師名が{len(unknown_t)}件あり、その行は読み飛ばしました")
    if unknown_c:
        notes.append("体験授業マスターと照合できない授業名を読み飛ばしました:" + "、".join(sorted(unknown_c)))
    return total, notes


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
