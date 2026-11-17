"""テーブル定義(要件定義書 7章の ER 図に対応)。

Databricks(Delta Lake / Unity Catalog)と、ローカル確認用の SQLite の両方で
同じ定義からテーブルを作ります。

ER 図からの追加:
- week_target      : R6「週ごとの系統の目安」を入力するため
- app_setting      : Should の重みなどの設定を保存するため
- availability_raw : Google フォームの回答を Apps Script がそのまま書き込む場所
- assignment.manual / message.subject, body / assignment_change.* : 画面で使う項目
"""
from __future__ import annotations

TABLES: dict[str, list[tuple[str, str]]] = {
    # ---- マスター(あまり変わらないデータ)
    "teacher": [("teacher_id", "STRING"), ("name", "STRING"), ("active", "BOOLEAN")],
    "genre": [("genre_id", "STRING"), ("name", "STRING"), ("r5_target", "BOOLEAN")],
    "genre_substitute": [("genre_id", "STRING"), ("substitute_genre_id", "STRING")],
    "course": [("course_id", "STRING"), ("name", "STRING"), ("genre_id", "STRING")],
    "teacher_course": [("teacher_id", "STRING"), ("course_id", "STRING")],
    "software": [("software_id", "STRING"), ("name", "STRING")],
    "course_software": [("course_id", "STRING"), ("software_id", "STRING")],
    "room": [("room_id", "STRING"), ("name", "STRING")],
    "room_software": [
        ("room_id", "STRING"), ("software_id", "STRING"),
        ("valid_from", "DATE"), ("valid_to", "DATE"), ("confirmed_on", "DATE"),
    ],
    "slot": [("slot_id", "STRING"), ("day", "DATE"), ("period", "STRING"), ("week_key", "STRING")],
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
    ],
}

SQLITE_TYPES = {"STRING": "TEXT", "DATE": "TEXT", "BOOLEAN": "INTEGER", "INT": "INTEGER"}


def ddl(table: str, qualified_name: str, dialect: str) -> str:
    cols = ", ".join(
        f"{name} {SQLITE_TYPES[typ] if dialect == 'sqlite' else typ}" for name, typ in TABLES[table]
    )
    return f"CREATE TABLE IF NOT EXISTS {qualified_name} ({cols})"
