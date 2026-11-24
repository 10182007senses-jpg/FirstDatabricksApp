"""デモ用のダミーデータ。講師名・教室・ソフトの対応はすべて架空です。

実データが届くまで、画面とルールの動きを確かめるために使います。
"""
from __future__ import annotations

import calendar
from datetime import date

from .data_access import MASTER_TABLES, Repo, month_range
from .models import slot_id_of

GENRES = [("g_illust", "イラスト"), ("g_prog", "プログラミング"), ("g_game", "ゲームプログラミング"),
          ("g_cg", "CG"), ("g_vfx", "VFX・エフェクト")]
SUBSTITUTES = [("g_cg", "g_vfx"), ("g_game", "g_prog")]
SOFTWARE = [("s_clip", "CLIP STUDIO"), ("s_unity", "Unity"), ("s_maya", "Maya"),
            ("s_ae", "After Effects"), ("s_vscode", "VS Code")]
ROOMS = [("r_401", "401"), ("r_402", "402"), ("r_403", "403"), ("r_404", "404")]
ROOM_SOFTWARE = {
    "r_401": ["s_vscode", "s_unity"],
    "r_402": ["s_maya", "s_ae"],
    "r_403": ["s_unity", "s_ae", "s_vscode"],
    "r_404": ["s_clip"],
}
COURSES = [
    ("c_illust", "デジタルイラスト体験", "g_illust", ["s_clip"]),
    ("c_anime", "デジタルアニメ体験", "g_illust", ["s_clip"]),
    ("c_python", "Pythonプログラミング体験", "g_prog", ["s_vscode"]),
    ("c_web", "Webサイト制作体験", "g_prog", ["s_vscode"]),
    ("c_unity", "Unityゲーム制作体験", "g_game", ["s_unity"]),
    ("c_cg", "3DCGモデリング体験", "g_cg", ["s_maya"]),
    ("c_vfx", "VFX体験", "g_vfx", ["s_ae"]),
    ("c_effect", "ゲームエフェクト体験", "g_vfx", ["s_unity"]),
]
TEACHERS = [
    ("t_01", "青木", ["c_illust", "c_anime"]),
    ("t_02", "井上", ["c_illust"]),
    ("t_03", "上田", ["c_python", "c_web"]),
    ("t_04", "遠藤", ["c_python"]),
    ("t_05", "小川", ["c_unity", "c_effect"]),
    ("t_06", "加藤", ["c_unity"]),
    ("t_07", "木村", ["c_cg"]),
    ("t_08", "工藤", ["c_vfx", "c_effect"]),
]


def seed_masters(repo: Repo, confirmed_on: date | None = None) -> None:
    confirmed_on = confirmed_on or date.today()
    data = {
        "genre": [{"genre_id": g, "name": n, "r5_target": g != "g_vfx"} for g, n in GENRES],
        "genre_substitute": [{"genre_id": g, "substitute_genre_id": s} for g, s in SUBSTITUTES],
        "software": [{"software_id": s, "name": n} for s, n in SOFTWARE],
        "room": [{"room_id": r, "name": n, "usable": True} for r, n in ROOMS],
        "room_software": [
            {"room_id": r, "software_id": s, "valid_from": None, "valid_to": None, "confirmed_on": confirmed_on}
            for r, softs in ROOM_SOFTWARE.items() for s in softs
        ],
        "course": [{"course_id": c, "name": n, "genre_id": g, "capacity": None, "note": ""}
                   for c, n, g, _ in COURSES],
        # ソフトがそろう教室のうち、最初の教室を「優先」、残りを「予備」にする
        "course_room": [
            {"course_id": c, "room_id": r, "priority": "main" if i == 0 else "backup"}
            for c, _, _, softs in COURSES
            for i, r in enumerate([r for r, have in ROOM_SOFTWARE.items() if set(softs) <= set(have)])
        ],
        "room_alternative": [],
        "course_software": [{"course_id": c, "software_id": s} for c, _, _, softs in COURSES for s in softs],
        "teacher": [{"teacher_id": t, "name": n, "active": True, "email": None} for t, n, _ in TEACHERS],
        "teacher_course": [{"teacher_id": t, "course_id": c} for t, _, cs in TEACHERS for c in cs],
    }
    for table in MASTER_TABLES:
        repo.replace(table, data[table])


def seed_month(repo: Repo, month: str) -> None:
    """デモ用に、日曜の午前・午後を開催枠にして、対応可能日と使えない教室を入れる。"""
    first, last = month_range(month)
    sundays = [date(first.year, first.month, d) for d in range(1, last.day + 1)
               if calendar.weekday(first.year, first.month, d) == 6]
    repo.save_slots(month, [(d, p) for d in sundays for p in ("AM", "PM")])

    # 講師ごとの対応可能日(週の番号, 時間帯)。木村(CG)は前半だけ、加藤は1枠だけ
    pattern = {
        "t_01": [(0, "AM"), (0, "PM"), (2, "AM"), (3, "PM")],
        "t_02": [(1, "PM"), (2, "PM"), (3, "AM")],
        "t_03": [(0, "AM"), (1, "AM"), (1, "PM"), (3, "AM")],
        "t_04": [(2, "AM"), (2, "PM"), (3, "PM")],
        "t_05": [(0, "PM"), (1, "AM"), (2, "PM"), (3, "AM")],
        "t_06": [(1, "PM")],
        "t_07": [(0, "AM"), (1, "AM")],
        "t_08": [(0, "PM"), (2, "AM"), (3, "AM"), (3, "PM")],
    }
    pairs = {
        (t, slot_id_of(sundays[w], p))
        for t, items in pattern.items() for w, p in items if w < len(sundays)
    }
    repo.save_availability(month, pairs)
    if len(sundays) > 1:
        repo.save_room_blocks(month, {("r_404", slot_id_of(sundays[1], "AM")): "保護者会"})
