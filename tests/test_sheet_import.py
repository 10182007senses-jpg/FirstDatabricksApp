"""調整共有シート・フォーム回答の取り込みと、優先教室・R11 のテスト。名前や教室はすべて架空です。"""
import sqlite3
from datetime import date

import pytest

from scheduler import manager, rules, sample_data, sheet_import, solver
from scheduler.data_access import Repo, SqliteBackend
from scheduler.models import MAIN, Assignment, RoomAlternative, slot_id_of

MONTH = "2026-12"
D1 = date(2026, 12, 6)

COURSE_ROWS = [
    ["体験授業 マスター一覧"],
    ["※説明"],
    ["No.", "体験授業名", "講師名", "必要な教室環境/ソフト", "優先して入れる教室", "予備教室", "定員目安", "備考・準備事項"],
    [1, "ゲーム制作体験", "青木先生/井上先生", "Unity", "101102", "なし", "-", ""],
    [2, "手描きイラスト体験", "上田先生", "なし", "201", "なし", 8, "画用紙を用意"],
    [3, "ゲーム制作体験", "遠藤先生", "Unity", "102", "101", "-", ""],
    [4, "ロボット体験", "小川先生", "ロボットキット", "lab1", "なし", "-", ""],
]
ROOM_ROWS = [
    ["教室ソフト一覧"],
    ["No.", "教室名", "必要な教室環境/ソフト"],
    [1, "101", "Unity,Photoshop"],
    [2, "102", "Unity"],
    [3, "201", "Photoshop"],
    [4, "202", "使用しない"],
    [5, "lab1", "機械工作のみ使用"],
    [6, "3301A", "Unity"],
]


@pytest.fixture
def repo():
    r = Repo(SqliteBackend(":memory:"))
    r.init_schema()
    return r


def test_split_rooms_and_lists():
    assert sheet_import.split_rooms("303301501", ["301", "303", "501", "lab1"]) == ["303", "301", "501"]
    assert sheet_import.split_rooms("lab1", ["lab1", "301"]) == ["lab1"]
    assert sheet_import.split_rooms("なし", ["301"]) == []
    assert sheet_import.split_list("Fusion,3Dプリンター") == ["Fusion", "3Dプリンター"]
    assert sheet_import.soft_key("AfterEffects") == sheet_import.soft_key("aftereffect")


def test_parse_shared_sheet_builds_masters():
    res = sheet_import.parse_shared_sheet(COURSE_ROWS, ROOM_ROWS)
    t = res.tables
    names = {c["name"] for c in t["course"]}
    assert names == {"ゲーム制作体験", "手描きイラスト体験", "ロボット体験"}  # 同じ名前の行は1つの授業にまとまる
    game = next(c["course_id"] for c in t["course"] if c["name"] == "ゲーム制作体験")
    teachers = {r["teacher_id"] for r in t["teacher_course"] if r["course_id"] == game}
    assert len(teachers) == 3  # 1つの授業を複数の講師が担当する
    pri = {(r["room_id"], r["priority"]) for r in t["course_room"] if r["course_id"] == game}
    r101 = sheet_import.stable_id("r", "101")
    r102 = sheet_import.stable_id("r", "102")
    assert (r101, MAIN) in pri and (r102, MAIN) in pri  # 先に書かれた「優先」が残る
    rooms = {r["name"]: r["usable"] for r in t["room"]}
    assert rooms["202"] is False and rooms["lab1"] is True
    assert next(c for c in t["course"] if c["name"] == "手描きイラスト体験")["capacity"] == 8
    assert any("ロボットキット" in n for n in res.notes)  # 教室シートにない機材は判定できないと知らせる


def test_reimport_keeps_genre_and_deactivates_missing_teacher(repo):
    manager.import_masters(repo, COURSE_ROWS, ROOM_ROWS)
    rows = repo.read("course")
    for r in rows:
        r["genre_id"] = "g_game"
    repo.replace("course", rows)
    manager.import_masters(repo, COURSE_ROWS[:4], ROOM_ROWS)
    assert all(r["genre_id"] == "g_game" for r in repo.read("course"))
    inactive = {r["name"] for r in repo.read("teacher") if r["active"] is False}
    assert inactive == {"上田", "遠藤", "小川"}


def test_parse_form_rows_reads_half_day_answers():
    rows = [
        ["メールアドレス", "講師名をフルネームで記載ください",
         "12/6(日)\n【午前・午後】\n講師朝礼:10:10/13:10", "12/13(日)\n【午前・午後】", "備考"],
        ["a@example.com", "青木 太郎", "午前午後 対応可", "午後のみ 対応可", ""],
        ["b@example.com", "井上花子", "対応不可", "午前のみ 対応可", "よろしくお願いします"],
    ]
    recs, notes = sheet_import.parse_form_rows(rows, MONTH)
    got = {(r["name"], r["day"].day, r["period"]) for r in recs}
    assert got == {("青木太郎", 6, "AM"), ("青木太郎", 6, "PM"), ("青木太郎", 13, "PM"), ("井上花子", 13, "AM")}
    assert notes == []


def test_form_year_rolls_over_for_january():
    rows = [["講師名", "12/27(日)", "1/10(日)"], ["青木", "午前午後 対応可", "対応不可"]]
    recs, _ = sheet_import.parse_form_rows(rows, "2027-01")
    assert recs[0]["day"] == date(2026, 12, 27)


def test_form_names_match_surname_and_learn_email(repo):
    manager.import_masters(repo, COURSE_ROWS, ROOM_ROWS)
    repo.save_slots(MONTH, [(D1, "AM"), (D1, "PM")])
    recs = [{"name": "青木太郎", "email": "a@example.com", "day": D1, "period": "AM"}]
    n, notes = manager.import_availability(repo, MONTH, recs)
    assert n == 1 and notes == []
    assert next(t for t in repo.read("teacher") if t["name"] == "青木")["email"] == "a@example.com"
    # 2回目はメールアドレスで照合する(名前の書き方が変わっても入る)
    n, _ = manager.import_availability(repo, MONTH, [{"name": "あおき", "email": "A@example.com", "day": D1,
                                                      "period": "PM"}])
    assert n == 1


def test_solver_prefers_main_room_and_warns_outside_list(repo):
    sample_data.seed_masters(repo, confirmed_on=date(2026, 11, 1))
    sample_data.seed_month(repo, MONTH)
    ctx = repo.load(MONTH).ctx
    plan = solver.generate(ctx)
    for a in plan:
        assert ctx.room_priority(a.course_id, a.room_id, ctx.slots[a.slot_id].day) is not None
    # 優先・予備以外の教室に入れたら、講師への動作確認を促す
    slot = slot_id_of(D1, "AM")
    a = Assignment("x", slot, "c_python", "t_03", "r_403")
    ctx.course_rooms["c_python"] = {"r_401": MAIN}
    assert any(f.rule == "教室" for f in rules.check_warnings([a], ctx))


def test_r11_alternative_room_counts_as_main_until_deadline(repo):
    sample_data.seed_masters(repo, confirmed_on=date(2026, 11, 1))
    ctx = repo.load(MONTH).ctx
    ctx.course_rooms["c_python"] = {"r_401": MAIN}
    ctx.room_alternatives = [RoomAlternative("r_401", "r_403", date(2026, 12, 31))]
    assert ctx.room_priority("c_python", "r_403", D1) == MAIN
    assert ctx.room_priority("c_python", "r_403", date(2027, 1, 10)) is None


def test_unusable_room_is_not_used(repo):
    sample_data.seed_masters(repo, confirmed_on=date(2026, 11, 1))
    rows = repo.read("room")
    for r in rows:
        r["usable"] = r["room_id"] != "r_404"
    repo.replace("room", rows)
    assert "r_404" not in repo.load(MONTH).ctx.rooms


def test_events_roundtrip(repo):
    repo.save_slots(MONTH, [(D1, "AM")])
    repo.save_events(MONTH, {D1: "ゲームお仕事体験", date(2027, 1, 3): "対象外"})
    assert repo.load(MONTH).ctx.events == {D1: "ゲームお仕事体験"}


def test_old_tables_get_new_columns(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE teacher (teacher_id TEXT, name TEXT, active INTEGER)")
    con.execute("INSERT INTO teacher VALUES ('t1', '青木', 1)")
    con.commit()
    con.close()
    r = Repo(SqliteBackend(str(path)))
    r.init_schema()
    assert r.read("teacher") == [{"teacher_id": "t1", "name": "青木", "active": True, "email": None}]


def test_import_masters_from_sheet_raw(repo):
    import json

    rows = []
    for name, data in (("体験授業一覧", COURSE_ROWS), ("教室", ROOM_ROWS)):
        rows += [{"sheet_name": name, "row_no": i, "cells_json": json.dumps([str(c) for c in r], ensure_ascii=False),
                  "received_at": "2026-11-24T00:00:00"} for i, r in enumerate(data, start=1)]
    repo.insert("sheet_raw", rows)
    counts, _ = manager.import_masters_from_sheet_raw(repo)
    assert counts["course"] == 3 and counts["room"] == 6


def test_form_names_with_company_and_kanji_variants(repo):
    by_name = {manager._norm_name(k): v for k, v in {"青木": "t1", "齋藤": "t2", "サンプル工房": "t3", "上田": "t4"}.items()}
    amb: set[str] = set()
    assert manager._match_name(manager._norm_name("有限会社サンプル工房"), by_name, amb) == "t3"
    assert manager._match_name(manager._norm_name("斉藤 花子"), by_name, amb) == "t2"  # 斉・齋は同じ字として扱う
    # 会社名のあとに担当者の名前があるときは、担当者を選ぶ
    assert manager._match_name(manager._norm_name("株式会社サンプル工房上田_アニメ担当可能日"), by_name, amb) == "t4"
    assert manager._match_name(manager._norm_name("有限会社見知らぬ社青木"), by_name, amb) == "t1"
    assert amb == set()
