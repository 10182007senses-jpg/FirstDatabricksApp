"""データアクセス。テーブルの読み書きをここ一か所に集めます。

- Databricks 上: SQL ウェアハウス経由で Delta テーブル(Unity Catalog)を読み書き
- ローカル確認・テスト: 同じ SQL を SQLite に対して実行
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import schema
from .messages import Message
from .models import (
    Assignment, Context, Course, Genre, PrevAssignment, RoomSoftware, Settings, Slot, Teacher,
    slot_id_of, week_key_of,
)
from .solver import new_id

MASTER_TABLES = (
    "teacher", "genre", "genre_substitute", "course", "teacher_course",
    "software", "course_software", "room", "room_software",
)
STATUS_LABEL = {"preparing": "準備中", "draft": "案", "confirmed": "確定", "contacted": "連絡済"}


# ---------------------------------------------------------------- 接続

class SqliteBackend:
    dialect = "sqlite"
    label = "ローカル(SQLite)"

    def __init__(self, path: str = "local.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)

    def table(self, name: str) -> str:
        return name

    def setup(self) -> None:
        pass

    def run(self, sql: str, params: list) -> list[tuple]:
        params = [p.isoformat() if isinstance(p, date) else p for p in params]
        return self._conn.execute(sql, params).fetchall()


class DatabricksBackend:
    dialect = "databricks"

    def __init__(self, warehouse_id: str, catalog: str, schema_name: str):
        self.warehouse_id, self.catalog, self.schema_name = warehouse_id, catalog, schema_name
        self.label = f"Databricks({catalog}.{schema_name})"
        self._conn = None

    def table(self, name: str) -> str:
        return f"`{self.catalog}`.`{self.schema_name}`.`{name}`"

    def setup(self) -> None:
        self.run(f"CREATE SCHEMA IF NOT EXISTS `{self.catalog}`.`{self.schema_name}`", [])

    def _connect(self):
        from databricks import sql
        from databricks.sdk.core import Config

        cfg = Config()  # アプリのサービスプリンシパルで認証(トークンは書かない)
        return sql.connect(
            server_hostname=cfg.host.replace("https://", "").rstrip("/"),
            http_path=f"/sql/1.0/warehouses/{self.warehouse_id}",
            credentials_provider=lambda: cfg.authenticate,
        )

    def run(self, sql: str, params: list) -> list[tuple]:
        for attempt in (1, 2):
            try:
                if self._conn is None:
                    self._conn = self._connect()
                with self._conn.cursor() as cur:
                    cur.execute(sql, params or None)
                    return [tuple(r) for r in cur.fetchall()] if cur.description else []
            except Exception:
                # 長時間開いたままの接続は切れることがあるので、1回だけつなぎ直す
                self._conn = None
                if attempt == 2:
                    raise
        return []


def backend_from_env():
    warehouse = os.getenv("DATABRICKS_WAREHOUSE_ID")
    if warehouse:
        return DatabricksBackend(
            warehouse, os.getenv("APP_CATALOG", "workspace"), os.getenv("APP_SCHEMA", "taiken_chosei"))
    return SqliteBackend(os.getenv("LOCAL_DB_PATH", "local.db"))


# ---------------------------------------------------------------- 1か月分のまとまり

@dataclass
class Bundle:
    month: str
    adjustment: dict | None
    ctx: Context
    assignments: list[Assignment] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.adjustment["status"] if self.adjustment else "preparing"


def month_range(month: str) -> tuple[date, date]:
    first = date(int(month[:4]), int(month[5:7]), 1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def previous_month(month: str) -> str:
    last = month_range(month)[0] - timedelta(days=1)
    return f"{last.year}-{last.month:02d}"


class Repo:
    def __init__(self, backend=None):
        self.db = backend or backend_from_env()
        self._lock = threading.RLock()
        self._cache: dict = {}

    # ------------------------------------------------------------ 基本操作

    def init_schema(self) -> None:
        with self._lock:
            self.db.setup()
            for name in schema.TABLES:
                self.db.run(schema.ddl(name, self.db.table(name), self.db.dialect), [])

    def read(self, table: str, where: str = "", params: list | None = None) -> list[dict]:
        cols = schema.TABLES[table]
        sql = f"SELECT {', '.join(c for c, _ in cols)} FROM {self.db.table(table)}"
        if where:
            sql += f" WHERE {where}"
        with self._lock:
            rows = self.db.run(sql, params or [])
        out = []
        for row in rows:
            rec = {}
            for (col, typ), value in zip(cols, row):
                if value is not None and typ == "DATE" and not isinstance(value, date):
                    value = date.fromisoformat(str(value)[:10])
                elif value is not None and typ == "DATE" and isinstance(value, datetime):
                    value = value.date()
                elif value is not None and typ == "BOOLEAN":
                    value = bool(value)
                rec[col] = value
            out.append(rec)
        return out

    def insert(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        cols = [c for c, _ in schema.TABLES[table]]
        one = "(" + ", ".join("?" for _ in cols) + ")"
        chunk = max(1, 200 // len(cols))  # 1文あたりのパラメータ数を抑える
        with self._lock:
            for i in range(0, len(rows), chunk):
                part = rows[i:i + chunk]
                sql = f"INSERT INTO {self.db.table(table)} ({', '.join(cols)}) VALUES " + ", ".join([one] * len(part))
                self.db.run(sql, [r.get(c) for r in part for c in cols])
            self._cache.clear()

    def delete(self, table: str, where: str = "", params: list | None = None) -> None:
        sql = f"DELETE FROM {self.db.table(table)}" + (f" WHERE {where}" if where else "")
        with self._lock:
            self.db.run(sql, params or [])
            self._cache.clear()

    def replace(self, table: str, rows: list[dict], where: str = "", params: list | None = None) -> None:
        with self._lock:
            self.delete(table, where, params)
            self.insert(table, rows)

    # ------------------------------------------------------------ 設定

    def load_settings(self) -> Settings:
        s = Settings()
        for row in self.read("app_setting"):
            if row["setting_key"] in Settings.INT_FIELDS:
                try:
                    setattr(s, row["setting_key"], int(row["setting_value"]))
                except (TypeError, ValueError):
                    pass
        return s

    def save_settings(self, s: Settings) -> None:
        self.replace("app_setting", [
            {"setting_key": k, "setting_value": str(getattr(s, k))} for k in Settings.INT_FIELDS])

    # ------------------------------------------------------------ 月次データ

    def get_adjustment(self, month: str) -> dict | None:
        rows = self.read("adjustment", "target_month = ?", [month])
        return rows[0] if rows else None

    def ensure_adjustment(self, month: str) -> dict:
        adj = self.get_adjustment(month)
        if adj is None:
            adj = {"adjustment_id": new_id(), "target_month": month, "status": "preparing"}
            self.insert("adjustment", [adj])
        return adj

    def set_status(self, month: str, status: str) -> None:
        adj = self.ensure_adjustment(month)
        with self._lock:
            self.db.run(
                f"UPDATE {self.db.table('adjustment')} SET status = ? WHERE adjustment_id = ?",
                [status, adj["adjustment_id"]])
            self._cache.clear()

    def load(self, month: str) -> Bundle:
        """1か月分のデータをまとめて読む。書き込みがあるまで結果を覚えておく。"""
        with self._lock:
            if month not in self._cache:
                self._cache[month] = self._load(month)
            return self._cache[month]

    def _load(self, month: str) -> Bundle:
        ctx = Context(target_month=month, settings=self.load_settings())
        for r in self.read("teacher"):
            ctx.teachers[r["teacher_id"]] = Teacher(r["teacher_id"], r["name"], r["active"] is not False)
        for r in self.read("genre"):
            ctx.genres[r["genre_id"]] = Genre(r["genre_id"], r["name"], r["r5_target"] is not False)
        for r in self.read("genre_substitute"):
            ctx.substitutes.setdefault(r["genre_id"], set()).add(r["substitute_genre_id"])
        for r in self.read("course"):
            ctx.courses[r["course_id"]] = Course(r["course_id"], r["name"], r["genre_id"])
        for r in self.read("teacher_course"):
            ctx.teacher_courses.setdefault(r["teacher_id"], set()).add(r["course_id"])
        for r in self.read("software"):
            ctx.software[r["software_id"]] = r["name"]
        for r in self.read("course_software"):
            ctx.course_software.setdefault(r["course_id"], set()).add(r["software_id"])
        for r in self.read("room"):
            ctx.rooms[r["room_id"]] = r["name"]
        ctx.room_software = [
            RoomSoftware(r["room_id"], r["software_id"], r["valid_from"], r["valid_to"], r["confirmed_on"])
            for r in self.read("room_software")
        ]

        first, last = month_range(month)
        for r in self.read("slot", "day >= ? AND day <= ?", [first, last]):
            ctx.slots[r["slot_id"]] = Slot(r["slot_id"], r["day"], r["period"], r["week_key"])
        for r in self.read("room_block", "slot_id LIKE ?", [f"{month}-%"]):
            if r["slot_id"] in ctx.slots:
                ctx.room_blocks[(r["room_id"], r["slot_id"])] = r["reason"] or ""

        adj = self.get_adjustment(month)
        bundle = Bundle(month, adj, ctx)
        if adj:
            aid = [adj["adjustment_id"]]
            for r in self.read("availability", "adjustment_id = ?", aid):
                if r["slot_id"] in ctx.slots:
                    ctx.availability.add((r["teacher_id"], r["slot_id"]))
            for r in self.read("week_target", "adjustment_id = ?", aid):
                ctx.week_targets[(r["week_key"], r["genre_id"])] = int(r["target_count"] or 0)
            bundle.assignments = [
                Assignment(r["assignment_id"], r["slot_id"], r["course_id"], r["teacher_id"], r["room_id"],
                           r["status"] or "draft", r["reason"] or "", bool(r["manual"]))
                for r in self.read("assignment", "adjustment_id = ?", aid)
            ]
            bundle.assignments.sort(key=lambda a: (a.slot_id, ctx.room_name(a.room_id)))
            bundle.messages = [
                Message(r["message_id"], r["teacher_id"], r["kind"], r["subject"] or "", r["body"] or "",
                        r["status"] or "draft")
                for r in self.read("message", "adjustment_id = ?", aid)
            ]
        ctx.previous = self._previous_assignments(month, ctx)
        return bundle

    def _previous_assignments(self, month: str, ctx: Context) -> list[PrevAssignment]:
        """前月の確定した割り当て(F14)。月をまたぐ R5 の判定に使う。"""
        prev = self.get_adjustment(previous_month(month))
        if not prev or prev["status"] not in ("confirmed", "contacted"):
            return []
        first, last = month_range(previous_month(month))
        days = {r["slot_id"]: r["day"] for r in self.read("slot", "day >= ? AND day <= ?", [first, last])}
        out = []
        for r in self.read("assignment", "adjustment_id = ?", [prev["adjustment_id"]]):
            day, genre = days.get(r["slot_id"]), ctx.genre_of(r["course_id"])
            if day and genre:
                out.append(PrevAssignment(day, week_key_of(day), genre))
        return out

    def save_slots(self, month: str, picks: list[tuple[date, str]]) -> None:
        first, last = month_range(month)
        rows = [
            {"slot_id": slot_id_of(d, p), "day": d, "period": p, "week_key": week_key_of(d)}
            for d, p in sorted(set(picks)) if first <= d <= last
        ]
        self.ensure_adjustment(month)
        self.replace("slot", rows, "day >= ? AND day <= ?", [first, last])

    def save_availability(self, month: str, pairs: set[tuple[str, str]]) -> None:
        adj = self.ensure_adjustment(month)
        rows = [{"adjustment_id": adj["adjustment_id"], "teacher_id": t, "slot_id": s} for t, s in sorted(pairs)]
        self.replace("availability", rows, "adjustment_id = ?", [adj["adjustment_id"]])

    def save_room_blocks(self, month: str, blocks: dict[tuple[str, str], str]) -> None:
        rows = [{"room_id": r, "slot_id": s, "reason": why} for (r, s), why in sorted(blocks.items())]
        self.replace("room_block", rows, "slot_id LIKE ?", [f"{month}-%"])

    def save_week_targets(self, month: str, targets: dict[tuple[str, str], int]) -> None:
        adj = self.ensure_adjustment(month)
        rows = [
            {"adjustment_id": adj["adjustment_id"], "week_key": w, "genre_id": g, "target_count": n}
            for (w, g), n in sorted(targets.items()) if n
        ]
        self.replace("week_target", rows, "adjustment_id = ?", [adj["adjustment_id"]])

    def save_assignments(self, month: str, assignments: list[Assignment]) -> None:
        adj = self.ensure_adjustment(month)
        rows = [
            {"assignment_id": a.assignment_id, "adjustment_id": adj["adjustment_id"], "slot_id": a.slot_id,
             "course_id": a.course_id, "teacher_id": a.teacher_id, "room_id": a.room_id,
             "status": a.status, "reason": a.reason, "manual": a.manual}
            for a in assignments
        ]
        self.replace("assignment", rows, "adjustment_id = ?", [adj["adjustment_id"]])

    def save_messages(self, month: str, messages: list[Message]) -> None:
        adj = self.ensure_adjustment(month)
        rows = [
            {"message_id": m.message_id, "adjustment_id": adj["adjustment_id"], "teacher_id": m.teacher_id,
             "kind": m.kind, "subject": m.subject, "body": m.body, "status": m.status}
            for m in messages
        ]
        self.replace("message", rows, "adjustment_id = ?", [adj["adjustment_id"]])

    def add_changes(self, month: str, changes: list[tuple[str, str]], reason: str) -> None:
        """先生が案を直した記録(後から加えるエージェント F15 でも使う)。"""
        adj = self.ensure_adjustment(month)
        now = datetime.now().isoformat(timespec="seconds")
        self.insert("assignment_change", [
            {"change_id": new_id(), "adjustment_id": adj["adjustment_id"], "assignment_id": aid,
             "changed_at": now, "summary": summary, "reason": reason}
            for aid, summary in changes
        ])

    def count_changes(self, month: str) -> int:
        adj = self.get_adjustment(month)
        return len(self.read("assignment_change", "adjustment_id = ?", [adj["adjustment_id"]])) if adj else 0
