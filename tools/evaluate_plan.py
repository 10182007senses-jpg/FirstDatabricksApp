"""実データで割り当て案を作り、体験授業早見表(実際の割り当て)とどれだけ一致するかを測る。

使い方(リポジトリの直下で):
    python tools/evaluate_plan.py 早見表.xlsx 調整共有シート.xlsx フォーム回答.xlsx 2026-11
    python tools/evaluate_plan.py ... 2026-10 --given-teachers   # 講師×日は実際と同じにして、授業・教室の選び方だけを見る

- データベースは使わず、メモリの中だけで動きます(アプリのデータは変わりません)
- 過去の実績には、対象月より前の早見表のシートだけを使います(答えを先に見ないため)
- 個人名は表示しません。件数と一致率だけを出します
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scheduler import manager, sheet_import, solver  # noqa: E402
from scheduler.data_access import Repo, SqliteBackend  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("quickref", help="体験授業早見表の Excel")
    ap.add_argument("shared", help="調整共有シートの Excel")
    ap.add_argument("form", help="フォーム回答の Excel")
    ap.add_argument("month", help="対象月(例 2026-11)")
    ap.add_argument("--given-teachers", action="store_true",
                    help="対応可能日を、実際に入った講師のその日の午前午後だけにする")
    ap.add_argument("--no-history", action="store_true", help="過去の早見表を使わない")
    ap.add_argument("--guess-genres", action="store_true", help="系統の決まっていない授業に、授業名から推定した系統を入れる")
    args = ap.parse_args()

    month = args.month
    quick = sheet_import.read_workbook(Path(args.quickref).read_bytes())
    title = next((t for t in quick if t.strip() == month.replace("-", "")), None)
    if title is None:
        sys.exit(f"早見表に {month.replace('-', '')} のシートがありません")

    repo = Repo(SqliteBackend(":memory:"))
    repo.init_schema()
    manager.import_masters_from_book(repo, sheet_import.read_workbook(Path(args.shared).read_bytes()))
    if args.guess_genres:
        manager.apply_genre_guesses(repo)
    if not args.no_history:
        manager.import_history_from_book(repo, quick)  # 対象月以降の分は、案を作るときに使われない

    truth = sheet_import.parse_quickref(quick[title], title)
    days = sorted({t["day"] for t in truth})
    form = sheet_import.read_workbook(Path(args.form).read_bytes())
    if not args.given_teachers:
        rows = sheet_import.pick_sheet(form, sheet_import.FORM_SHEET) or next(iter(form.values()))
        asked = {r["day"] for r in sheet_import.parse_form_rows(rows, month)[0]}
        days = [d for d in days if d in asked]  # フォームで聞いていない日(説明会など)は比べない
        truth = [t for t in truth if t["day"] in set(days)]
    repo.save_slots(month, [(d, p) for d in days for p in ("AM", "PM")])
    if args.given_teachers:
        manager.import_availability(repo, month, [{"name": t["teacher"], "day": t["day"], "period": p}
                                                 for t in truth for p in ("AM", "PM")])
    else:
        manager.import_availability_from_book(repo, month, form)

    ctx = repo.load(month).ctx
    plan = solver.generate(ctx)

    by_name = {manager._norm_name(t.name): tid for tid, t in ctx.teachers.items()}
    courses = {cid: c.name for cid, c in ctx.courses.items()}
    rooms = {sheet_import.norm(n).lower(): rid for rid, n in ctx.rooms.items()}
    miss = Counter()
    actual = set()
    for t in truth:
        tid = manager._match_name(manager._norm_name(t["teacher"]), by_name, set())
        cid = sheet_import.match_course(t["course"], courses)
        rid = rooms.get(sheet_import.norm(t["room"]).lower())
        miss["講師"] += tid is None
        miss["授業"] += cid is None
        miss["教室"] += rid is None
        actual.add((t["day"], t["period"], tid, cid, rid))
    pred = {(ctx.slots[a.slot_id].day, ctx.slots[a.slot_id].period, a.teacher_id, a.course_id, a.room_id)
            for a in plan}

    def score(n):
        a, p = {x[:n] for x in actual}, {x[:n] for x in pred}
        hit = len(a & p)
        return f"{hit:3d}/{len(a):3d}  再現 {hit / max(len(a), 1):4.0%}  適合 {hit / max(len(p), 1):4.0%}"

    print(f"対象月 {month}  比べた日 {len(days)}日  実際 {len(actual)}件  案 {len(pred)}件")
    print("早見表の名前をマスターと照合できなかった行:", {k: v for k, v in miss.items() if v} or "なし")
    print("講師×日×時間帯            ", score(3))
    print("講師×日×時間帯×授業       ", score(4))
    print("講師×日×時間帯×授業×教室  ", score(5))


if __name__ == "__main__":
    main()
