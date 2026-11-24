"""画面の組み立てテスト。すべてのタブを、空の状態・デモデータ・確定後で作ってみて、例外が出ないことを確かめる。"""
from datetime import date

import pytest


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID", raising=False)
    import importlib
    import sys

    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    return mod


def render_all(mod, month):
    for tab in ("prep", "plan", "msg", "master", "settings"):
        body = mod.render(tab, month[:4], str(int(month[5:])), 0)[0]
        assert body is not None
        # 失敗したときは「データを読み込めませんでした」の Alert が返るので、それではないことを見る
        assert "読み込めませんでした" not in str(body), tab


def test_layout_and_tabs_render(app_module):
    mod = app_module
    mod.layout()
    month = "2026-12"
    render_all(mod, month)  # 空の状態
    from scheduler import manager, sample_data

    assert mod.next_action(mod.repo.load(month))[0] == 0

    sample_data.seed_masters(mod.repo, confirmed_on=date(2026, 11, 1))
    sample_data.seed_month(mod.repo, month)
    mod.repo.save_events(month, {date(2026, 12, 6): "ゲームお仕事体験"})
    render_all(mod, month)
    assert mod.next_action(mod.repo.load(month))[0] == 1
    manager.create_plan(mod.repo, month)
    render_all(mod, month)
    assert mod.next_action(mod.repo.load(month))[0] == 2
    assert not manager.confirm(mod.repo, month)
    assert mod.next_action(mod.repo.load(month))[0] == 3
    manager.create_messages(mod.repo, month, "鈴木")
    render_all(mod, month)
    manager.mark_contacted(mod.repo, month)
    assert mod.next_action(mod.repo.load(month))[0] == 4


def test_master_course_genre_save_keeps_other_columns(app_module):
    mod = app_module
    from scheduler import sample_data

    sample_data.seed_masters(mod.repo)
    rows = mod.parse_master_rows("course", [{"course_id": "c_vfx", "genre_id": "g_cg"}])
    vfx = next(r for r in rows if r["course_id"] == "c_vfx")
    assert vfx["genre_id"] == "g_cg" and vfx["name"] == "VFX体験"
    assert len(rows) == len(mod.repo.read("course"))


def test_quickref_export_matches_sheet_layout(app_module):
    from io import BytesIO

    from openpyxl import load_workbook

    from scheduler import manager, quickref, sample_data

    mod = app_module
    month = "2026-12"
    sample_data.seed_masters(mod.repo, confirmed_on=date(2026, 11, 1))
    sample_data.seed_month(mod.repo, month)
    mod.repo.save_events(month, {date(2026, 12, 6): "ゲームお仕事体験"})
    manager.create_plan(mod.repo, month)
    b = mod.repo.load(month)
    ws = load_workbook(BytesIO(quickref.build_quickref(b.assignments, b.ctx)))["202612"]
    assert ws["B3"].value == "イベント名" and "選べる体験授業" in ws["E3"].value
    days = [ws.cell(row=r, column=2).value for r in range(4, ws.max_row + 1, 4)]
    assert [d.date() for d in days] == sorted({s.day for s in b.ctx.slots.values()})
    assert ws["D4"].value == "ゲームお仕事体験"
    assert "先生" in ws["E5"].value  # 授業名の下の行は「講師 教室」
