"""割り当てを「体験授業早見表」と同じ形の Excel にする。

早見表(広報と共有している Excel)の1か月分のシートと同じ並びにします。
- 3行目: 見出し(イベント名 / 1日最大2つ 選べる体験授業)
- 1日 = 4行のかたまり。B列 日付、C列 曜日、D列 イベント名・時間帯・集合時間
- E列から: 1列に体験授業を2つ(上下)。各授業は「授業名」の行と「講師名 教室」の行
- 午前か午後の片方だけの授業には「※午前のみ」「※午後のみ」を付ける
"""
from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .models import Assignment, Context

WEEKDAY = "月火水木金土日"
FONT = "メイリオ"
EVENT_FILL = PatternFill("solid", fgColor="FFA2ECD5")
COURSE_FILL = PatternFill("solid", fgColor="FFFED582")
THIN = Side(style="thin")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
TIME_TEXT = "集合時間　10:05／13:15\n授業時間　10:20～11:50\n授業時間　13:30～15:00"
PER_COLUMN = 2  # 1列に上下2つ
MIN_COLUMNS = 5  # 早見表は E〜I の5列


def day_courses(assignments: list[Assignment], ctx: Context) -> dict:
    """日付 -> [(授業名, 「講師 教室」, 時間帯の集合)]。同じ授業・講師・教室の午前と午後は1つにまとめる。"""
    grouped: dict = {}
    for a in assignments:
        slot = ctx.slots.get(a.slot_id)
        if slot is None:
            continue
        key = (a.course_id, a.teacher_id, a.room_id)
        grouped.setdefault(slot.day, {}).setdefault(key, set()).add(slot.period)
    out = {}
    for day, items in sorted(grouped.items()):
        rows = []
        for (course_id, teacher_id, room_id), periods in items.items():
            name = ctx.course_name(course_id)
            if periods == {"AM"}:
                name += "\n※午前のみ"
            elif periods == {"PM"}:
                name += "\n※午後のみ"
            who = f"{ctx.teacher_name(teacher_id)}先生 {ctx.room_name(room_id)}"
            rows.append((name, who, periods))
        # 午前午後の両方ある授業を先に、そのあと午前のみ・午後のみ
        rows.sort(key=lambda r: (len(r[2]) != 2, sorted(r[2]) != ["AM"], r[0]))
        out[day] = rows
    return out


def build_quickref(assignments: list[Assignment], ctx: Context) -> bytes:
    by_day = day_courses(assignments, ctx)
    # 枠だけあって割り当てのない日も、日付の行は出す
    for s in ctx.slots.values():
        by_day.setdefault(s.day, [])
    width = max([MIN_COLUMNS] + [-(-len(v) // PER_COLUMN) for v in by_day.values()])
    first, last = 5, 5 + width - 1  # E 列 = 5

    wb = Workbook()
    ws = wb.active
    ws.title = ctx.target_month.replace("-", "")

    def put(row, col, value, size=18, fill=None, border=BOX, fmt=None):
        c = ws.cell(row=row, column=col, value=value)
        c.font = Font(name=FONT, size=size, bold=True)
        c.alignment = CENTER
        if fill:
            c.fill = fill
        if border:
            c.border = border
        if fmt:
            c.number_format = fmt
        return c

    put(3, 2, "イベント名", size=22)
    ws.merge_cells(start_row=3, start_column=2, end_row=3, end_column=4)
    put(3, first, "1日最大2つ　選べる体験授業", size=26)
    ws.merge_cells(start_row=3, start_column=first, end_row=3, end_column=last)
    ws.row_dimensions[3].height = 69

    row = 4
    for day, courses in sorted(by_day.items()):
        put(row, 2, day, size=22, fmt="m/d;@")
        ws.merge_cells(start_row=row, start_column=2, end_row=row + 3, end_column=2)
        put(row, 3, WEEKDAY[day.weekday()], size=22)
        ws.merge_cells(start_row=row, start_column=3, end_row=row + 3, end_column=3)
        periods = {s.period for s in ctx.slots.values() if s.day == day}
        span = "（午前＋午後)" if periods == {"AM", "PM"} else "（午前のみ)" if periods == {"AM"} else "（午後のみ)"
        put(row, 4, ctx.event_name(day) or "", fill=EVENT_FILL)
        put(row + 1, 4, span, fill=EVENT_FILL)
        put(row + 2, 4, TIME_TEXT, size=14, fill=EVENT_FILL)
        ws.merge_cells(start_row=row + 2, start_column=4, end_row=row + 3, end_column=4)
        for i in range(width * PER_COLUMN):
            col = first + i % width
            top = row + (i // width) * 2
            if i < len(courses):
                name, who, _ = courses[i]
                put(top, col, name, fill=COURSE_FILL)
                put(top + 1, col, who, fill=COURSE_FILL)
            else:
                put(top, col, None)
                put(top + 1, col, None)
        for r, h in zip(range(row, row + 4), (87.75, 36, 87.75, 36)):
            ws.row_dimensions[r].height = h
        row += 4

    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 19.6
    ws.column_dimensions["C"].width = 12.4
    ws.column_dimensions["D"].width = 52.9
    for col in range(first, last + 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = 55
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
