"""テーブル定義(要件定義書 7章の ER 図に対応)。

Databricks(Delta Lake / Unity Catalog)と、ローカル確認用の SQLite の両方で
同じ定義からテーブルを作ります。

調整共有シート(体験授業一覧・教室・ルール一覧)と早見表から分かったことで足したもの:
- teacher.email          : フォーム回答(メールアドレス)と講師を照合するため
- course.capacity, note  : 体験授業一覧の「定員目安」「備考・準備事項」
- room.usable            : 教室シートで「使用しない」と書かれた教室を外すため
- course_room            : 体験授業一覧の「優先して入れる教室」「予備教室」(ルール一覧 No.1)
- room_alternative       : 工事中の代わりの教室(ルール一覧 No.4、R11)
- event                  : 開催日ごとのイベント名(早見表の「イベント名」)
- sheet_raw              : Apps Script が調整共有シートの行をそのまま書き込む場所

ER 図からの追加:
- week_target      : R6「週ごとの系統の目安」を入力するため
- app_setting      : Should の重みなどの設定を保存するため
- availability_raw : Google フォームの回答を Apps Script がそのまま書き込む場所
- assignment.manual / message.subject, body / assignment_change.* : 画面で使う項目
"""
from __future__ import annotations

TABLES: dict[str, list[tuple[str, str]]] = {
    # ---- マスター(あまり変わらないデータ)
    "teacher": [("teacher_id", "STRING"), ("name", "STRING"), ("active", "BOOLEAN"), ("email", "STRING")],
    "genre": [("genre_id", "STRING"), ("name", "STRING"), ("r5_target", "BOOLEAN")],
    "genre_substitute": [("genre_id", "STRING"), ("substitute_genre_id", "STRING")],
    "course": [
        ("course_id", "STRING"), ("name", "STRING"), ("genre_id", "STRING"),
        ("capacity", "INT"), ("note", "STRING"),
    ],
    "teacher_course": [("teacher_id", "STRING"), ("course_id", "STRING")],
    "software": [("software_id", "STRING"), ("name", "STRING")],
    "course_software": [("course_id", "STRING"), ("software_id", "STRING")],
    "room": [("room_id", "STRING"), ("name", "STRING"), ("usable", "BOOLEAN")],
    "course_room": [("course_id", "STRING"), ("room_id", "STRING"), ("priority", "STRING")],  # main / backup
    "room_alternative": [
        ("room_id", "STRING"), ("alt_room_id", "STRING"), ("valid_until", "DATE"), ("note", "STRING"),
    ],
    "room_software": [
        ("room_id", "STRING"), ("software_id", "STRING"),
        ("valid_from", "DATE"), ("valid_to", "DATE"), ("confirmed_on", "DATE"),
    ],
    "slot": [("slot_id", "STRING"), ("day", "DATE"), ("period", "STRING"), ("week_key", "STRING")],
    "event": [("day", "DATE"), ("event_name", "STRING")],
    # ---- 毎月発生するデータ
    "adjustment": [("adjustment_id", "STRING"), ("target_month", "STRING"), ("status", "STRING")],
    "availability": [("adjustment_id", "STRING"), ("teacher_id", "STRING"), ("slot_id", "STRING")],
    "room_block": [("room_id", "STRING"), ("slot_id", "STRING"), ("reason", "STRING")],
    "assignment": [
        ("assignment_id", "STRING"), ("adjustment_id", "STRING"), ("slot_id", "STRING"),
        ("course_id", "STRING"), ("teacher_id", "STRING"), ("room_id", "STRING"),
        ("status", "STRING"), ("reason", "STRING"), ("manual", "BOOLEAN"),
    ],
    "message": [
        ("message_id", "STRING"), ("adjustment_id", "STRING"), ("teacher_id", "STRING"),
        ("kind", "STRING"), ("subject", "STRING"), ("body", "STRING"), ("status", "STRING"),
    ],
    "assignment_change": [
        ("change_id", "STRING"), ("adjustment_id", "STRING"), ("assignment_id", "STRING"),
        ("changed_at", "STRING"), ("summary", "STRING"), ("reason", "STRING"),
    ],
    # ---- ER 図からの追加
    "week_target": [
        ("adjustment_id", "STRING"), ("week_key", "STRING"), ("genre_id", "STRING"), ("target_count", "INT"),
    ],
    "app_setting": [("setting_key", "STRING"), ("setting_value", "STRING")],
    "availability_raw": [
        ("received_at", "STRING"), ("teacher_name", "STRING"), ("day", "DATE"), ("period", "STRING"),
        ("teacher_email", "STRING"),
    ],
    # 過去の実際の割り当て(体験授業早見表から取り込む)。案を作るとき「いつもの組み合わせ」を優先するのに使う
    "history": [("day", "DATE"), ("period", "STRING"), ("course_id", "STRING"), ("teacher_id", "STRING"),
                ("room_id", "STRING")],
    "sheet_raw": [("sheet_name", "STRING"), ("row_no", "INT"), ("cells_json", "STRING"), ("received_at", "STRING")],
}

SQLITE_TYPES = {"STRING": "TEXT", "DATE": "TEXT", "BOOLEAN": "INTEGER", "INT": "INTEGER"}


def ddl(table: str, qualified_name: str, dialect: str) -> str:
    cols = ", ".join(
        f"{name} {SQLITE_TYPES[typ] if dialect == 'sqlite' else typ}" for name, typ in TABLES[table]
    )
    return f"CREATE TABLE IF NOT EXISTS {qualified_name} ({cols})"
