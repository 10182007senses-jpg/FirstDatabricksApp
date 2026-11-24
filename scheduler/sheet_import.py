"""調整共有シートとフォーム回答の取り込み。

先生は Databricks を直接触らず、ふだん使っているスプレッドシートに入力します(要件定義書 10章)。
ここでは、そのスプレッドシートの行を読み取り、マスターのテーブルの形に直します。

読む場所は2通りあり、中身の処理は同じです。
- Apps Script(gas/sheet_to_databricks.gs)が sheet_raw テーブルに書き込んだ行
- 画面からアップロードした Excel ファイル(.xlsx)

読み取る表(調整共有シート):
- 「体験授業一覧」: No. / 体験授業名 / 講師名 / 必要な教室環境/ソフト / 優先して入れる教室 / 予備教室 / 定員目安 / 備考・準備事項
- 「教室」       : No. / 教室名 / 必要な教室環境/ソフト(その教室に入っているソフト。「使用しない」なら使わない教室)
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from .models import BACKUP, MAIN

COURSE_SHEET = "体験授業一覧"
ROOM_SHEET = "教室"
FORM_SHEET = "フォームの回答"

NONE_WORDS = {"", "なし", "-", "ー", "―", "使用しない"}


def stable_id(prefix: str, text: str) -> str:
    """名前から毎回同じ ID を作る(取り込み直しても、割り当てや系統の設定が外れないように)。"""
    return f"{prefix}_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]}"


def norm(text) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


def norm_name(text) -> str:
    return norm(text).removesuffix("先生")


def soft_key(name: str) -> str:
    """ソフト名の表記揺れをそろえる(「AfterEffects」と「aftereffect」を同じものにする)。"""
    key = re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龠]", "", name.lower())
    return key.removesuffix("s") if len(key) > 3 else key


def split_list(text) -> list[str]:
    """「Fusion,3Dプリンター」「micro:bit、LED」→ 項目のリスト。"""
    items = [norm(x) for x in re.split(r"[,、，/／\n]", str(text or ""))]
    return [x for x in items if x and x not in NONE_WORDS]


def split_rooms(text, known: list[str]) -> list[str]:
    """「303301501」のように続けて書かれた教室番号を、教室シートの教室名で区切る。"""
    rest = norm(text)
    if rest in NONE_WORDS:
        return []
    names = sorted(known, key=len, reverse=True)
    out: list[str] = []
    while rest:
        rest = rest.lstrip(",、/・")
        hit = next((n for n in names if rest.lower().startswith(n.lower())), None)
        if hit is None:
            if rest:
                out.append(rest)  # 教室シートにない教室。注意点として知らせる
            break
        out.append(hit)
        rest = rest[len(hit):]
    return out


def find_header(rows: list[list], first_cell: str = "No.") -> int:
    for i, row in enumerate(rows):
        if row and norm(row[0]) == first_cell:
            return i
    raise ValueError(f"見出しの行(先頭が「{first_cell}」の行)が見つかりません")


def cell(row: list, i: int):
    return row[i] if i < len(row) else None


@dataclass
class MasterImport:
    """取り込んだ結果。tables はテーブル名 -> 行のリスト。"""

    tables: dict[str, list[dict]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def parse_shared_sheet(course_rows: list[list], room_rows: list[list],
                       existing: dict[str, list[dict]] | None = None) -> MasterImport:
    """調整共有シートの「体験授業一覧」「教室」の行から、マスターの行を作る。

    existing は今のマスター。系統(体験授業一覧に列がない)と講師のメールアドレスは、ここから引き継ぐ。
    """
    existing = existing or {}
    result = MasterImport()
    notes = result.notes

    # ---- 教室とソフト
    rooms, room_software, software = [], [], {}
    h = find_header(room_rows)
    for row in room_rows[h + 1:]:
        name = norm(cell(row, 1))
        if not name:
            continue
        spec = norm(cell(row, 2))
        usable = spec != "使用しない"
        rid = stable_id("r", name)
        rooms.append({"room_id": rid, "name": name, "usable": usable})
        if not usable or spec.endswith("のみ使用"):
            continue  # 「機械工作のみ使用」のように、ソフトではなく用途が書かれている
        for soft in split_list(spec):
            sid = stable_id("s", soft_key(soft))
            software.setdefault(sid, soft)
            room_software.append({"room_id": rid, "software_id": sid, "valid_from": None, "valid_to": None,
                                  "confirmed_on": date.today()})
    room_by_name = {r["name"]: r["room_id"] for r in rooms}
    room_soft_ids = {r["software_id"] for r in room_software}

    # ---- 体験授業と講師
    old_genre = {norm(c["name"]): c.get("genre_id") for c in existing.get("course", [])}
    old_email = {norm_name(t["name"]): t.get("email") for t in existing.get("teacher", [])}
    courses: dict[str, dict] = {}
    teachers: dict[str, dict] = {}
    teacher_course, course_software, course_room = set(), set(), {}
    unchecked: set[str] = set()
    unknown_rooms: set[str] = set()

    h = find_header(course_rows)
    for row in course_rows[h + 1:]:
        title = str(cell(row, 1) or "").strip()
        if not title:
            continue
        key = norm(title)
        cid = stable_id("c", key)
        if cid not in courses:
            cap = norm(cell(row, 6))
            courses[cid] = {"course_id": cid, "name": re.sub(r"\s+", " ", title), "genre_id": old_genre.get(key),
                            "capacity": int(cap) if cap.isdigit() else None,
                            "note": str(cell(row, 7) or "").strip()}
        for person in split_list(cell(row, 2)):
            name = norm_name(person)
            tid = stable_id("t", name)
            teachers[tid] = {"teacher_id": tid, "name": name, "active": True, "email": old_email.get(name)}
            teacher_course.add((tid, cid))
        for soft in split_list(cell(row, 3)):
            sid = stable_id("s", soft_key(soft))
            if sid in room_soft_ids:
                course_software.add((cid, sid))
            else:
                unchecked.add(soft)  # 教室シートに載っていないソフト・機材は、R2 で判定できない
        for col, priority in ((4, MAIN), (5, BACKUP)):
            for room in split_rooms(cell(row, col), list(room_by_name)):
                rid = room_by_name.get(room)
                if rid is None:
                    unknown_rooms.add(room)
                elif (cid, rid) not in course_room:
                    course_room[(cid, rid)] = priority

    # 前は登録されていて、今回の一覧にない講師は、消さずに「在籍していない」扱いにする
    for t in existing.get("teacher", []):
        if t["teacher_id"] not in teachers:
            teachers[t["teacher_id"]] = {**t, "active": False}

    result.tables = {
        "room": rooms,
        "software": [{"software_id": k, "name": v} for k, v in sorted(software.items(), key=lambda x: x[1])],
        "room_software": room_software,
        "course": list(courses.values()),
        "teacher": list(teachers.values()),
        "teacher_course": [{"teacher_id": t, "course_id": c} for t, c in sorted(teacher_course)],
        "course_software": [{"course_id": c, "software_id": s} for c, s in sorted(course_software)],
        "course_room": [{"course_id": c, "room_id": r, "priority": p} for (c, r), p in sorted(course_room.items())],
    }
    if unchecked:
        notes.append("教室シートに載っていないため、教室の判定(R2)に使えないソフト・機材:"
                     + "、".join(sorted(unchecked)) + "。教室シートに足すと判定に使えます")
    if unknown_rooms:
        notes.append("教室シートにない教室名を読み飛ばしました:" + "、".join(sorted(unknown_rooms)))
    no_genre = sum(1 for c in courses.values() if not c["genre_id"])
    if no_genre:
        notes.append(f"系統が決まっていない体験授業が{no_genre}件あります。「マスター」画面で系統を選んでください(R5に使います)")
    return result


# ---------------------------------------------------------------- フォーム回答

_DAY = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})")


def parse_answer(text) -> list[str]:
    """「午前午後 対応可」「午前/午後 対応可」「午前のみ 対応可」「対応不可」→ ["AM", "PM"] など。"""
    t = norm(text)
    if not t or "不可" in t:
        return []
    if "午前のみ" in t:
        return ["AM"]
    if "午後のみ" in t:
        return ["PM"]
    if "午前" in t or "午後" in t or "可" in t:
        return ["AM", "PM"]
    return []


def parse_form_rows(rows: list[list], month: str) -> tuple[list[dict], list[str]]:
    """フォームの回答の表(1行目が質問、1行1人)を、{name, email, day, period} の行にする。

    日付の列の見出しは「11/8(日) 【午前・午後】 講師朝礼…」の形。年は対象月から決める
    (対象月が1月で、見出しが12月なら前の年)。
    """
    if not rows:
        return [], ["回答がありません"]
    head = [str(c or "") for c in rows[0]]
    year, target = int(month[:4]), int(month[5:7])
    email_col = next((i for i, c in enumerate(head) if "メール" in c), None)
    name_col = next((i for i, c in enumerate(head) if "講師名" in c or "名前" in c), None)
    if name_col is None:
        raise ValueError("講師名の列(見出しに「講師名」か「名前」を含む列)が見つかりません")
    day_cols = []
    for i, c in enumerate(head):
        m = _DAY.search(c)
        if m and i not in (email_col, name_col):
            mon, d = int(m.group(1)), int(m.group(2))
            y = year - 1 if (target == 1 and mon == 12) else year + 1 if (target == 12 and mon == 1) else year
            day_cols.append((i, date(y, mon, d)))
    if not day_cols:
        raise ValueError("日付の列(見出しに「11/8」のような日付を含む列)が見つかりません")

    out, notes = [], []
    for row in rows[1:]:
        name = norm_name(cell(row, name_col))
        if not name:
            continue
        email = str(cell(row, email_col) or "").strip().lower() if email_col is not None else ""
        for i, day in day_cols:
            for period in parse_answer(cell(row, i)):
                out.append({"name": name, "email": email, "day": day, "period": period})
    months = sorted({f"{d.year}-{d.month:02d}" for _, d in day_cols})
    if month not in months:
        notes.append(f"このフォームの日付は{'・'.join(months)}のものです(表示中の対象月は{month})")
    return out, notes


# ---------------------------------------------------------------- Excel ファイル

def read_workbook(content: bytes) -> dict[str, list[list]]:
    """アップロードされた .xlsx を {シート名: 行のリスト} にする。"""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    out = {}
    for ws in wb.worksheets:
        out[ws.title] = [[(v.date() if isinstance(v, datetime) else v) for v in row]
                         for row in ws.iter_rows(values_only=True)]
    return out


def pick_sheet(book: dict[str, list[list]], name: str) -> list[list] | None:
    return next((rows for title, rows in book.items() if name in title), None)
