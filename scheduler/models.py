"""データの型定義(要件定義書 7章のデータモデルに対応)。

ここにある型は DB にも画面にも依存しません。
ルール判定・割り当て作成を他の環境へ移せるようにするためです。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

PERIODS = ("AM", "PM")
PERIOD_LABEL = {"AM": "午前", "PM": "午後"}
LABEL_PERIOD = {v: k for k, v in PERIOD_LABEL.items()}
WEEKDAYS = "月火水木金土日"


def week_key_of(day: date) -> str:
    """週の区切り。月曜始まりの ISO 週で数える【要確認: R5 の「週」の定義】。"""
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def slot_id_of(day: date, period: str) -> str:
    return f"{day.isoformat()}-{period}"


def format_day(day: date) -> str:
    return f"{day.month}月{day.day}日({WEEKDAYS[day.weekday()]})"


@dataclass(frozen=True)
class Teacher:
    teacher_id: str
    name: str
    active: bool = True


@dataclass(frozen=True)
class Genre:
    genre_id: str
    name: str
    r5_target: bool = True


@dataclass(frozen=True)
class Course:
    course_id: str
    name: str
    genre_id: str


@dataclass(frozen=True)
class RoomSoftware:
    room_id: str
    software_id: str
    valid_from: date | None = None
    valid_to: date | None = None
    confirmed_on: date | None = None


@dataclass(frozen=True)
class Slot:
    slot_id: str
    day: date
    period: str  # "AM" / "PM"
    week_key: str

    @property
    def label(self) -> str:
        return f"{format_day(self.day)} {PERIOD_LABEL[self.period]}"


@dataclass
class Assignment:
    assignment_id: str
    slot_id: str
    course_id: str
    teacher_id: str
    room_id: str
    status: str = "draft"  # draft(案) / confirmed(確定)
    reason: str = ""
    manual: bool = False  # 先生が手で直した行


@dataclass(frozen=True)
class PrevAssignment:
    """前月の確定した割り当て(R5 の月またぎ判定に使う)。"""

    day: date
    week_key: str
    genre_id: str


@dataclass
class Settings:
    """Should ルールの重みと運用パラメータ。値はすべて【要確認】の初期値。"""

    per_slot_target: int = 2  # 1枠あたりの授業数の目安
    per_slot_max: int = 3  # 1枠あたりの授業数の上限
    w_r5: int = 50  # 同じ系統が2週続けて空かない
    w_r6: int = 20  # 週ごとの打ち出し方の目安
    w_r8: int = 40  # どの講師も月に1回
    w_r10: int = 15  # 3月は空き教室を残す
    w_fill: int = 10  # 枠を目安まで埋める
    w_balance: int = 3  # 特定の講師に偏らせない
    stale_days: int = 180  # ソフト情報の確認日がこれより古ければ警告
    reserve_months: tuple[int, ...] = (3,)  # 空き教室を残す月
    reserve_rooms: int = 1  # 残す教室数

    INT_FIELDS = (
        "per_slot_target", "per_slot_max", "w_r5", "w_r6", "w_r8",
        "w_r10", "w_fill", "w_balance", "stale_days", "reserve_rooms",
    )


@dataclass
class Context:
    """1か月分の調整に必要なデータをまとめたもの。"""

    target_month: str  # "YYYY-MM"
    teachers: dict[str, Teacher] = field(default_factory=dict)
    genres: dict[str, Genre] = field(default_factory=dict)
    substitutes: dict[str, set[str]] = field(default_factory=dict)  # 系統 -> 代わりにできる系統
    courses: dict[str, Course] = field(default_factory=dict)
    teacher_courses: dict[str, set[str]] = field(default_factory=dict)
    software: dict[str, str] = field(default_factory=dict)  # id -> 名前
    course_software: dict[str, set[str]] = field(default_factory=dict)
    rooms: dict[str, str] = field(default_factory=dict)  # id -> 名前
    room_software: list[RoomSoftware] = field(default_factory=list)
    slots: dict[str, Slot] = field(default_factory=dict)
    availability: set[tuple[str, str]] = field(default_factory=set)  # (teacher_id, slot_id)
    room_blocks: dict[tuple[str, str], str] = field(default_factory=dict)  # (room_id, slot_id) -> 理由
    week_targets: dict[tuple[str, str], int] = field(default_factory=dict)  # (week_key, genre_id) -> コマ数
    previous: list[PrevAssignment] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)

    def sorted_slots(self) -> list[Slot]:
        return sorted(self.slots.values(), key=lambda s: (s.day, s.period))

    def teacher_name(self, teacher_id: str) -> str:
        t = self.teachers.get(teacher_id)
        return t.name if t else f"(不明:{teacher_id})"

    def course_name(self, course_id: str) -> str:
        c = self.courses.get(course_id)
        return c.name if c else f"(不明:{course_id})"

    def room_name(self, room_id: str) -> str:
        return self.rooms.get(room_id, f"(不明:{room_id})")

    def genre_name(self, genre_id: str) -> str:
        g = self.genres.get(genre_id)
        return g.name if g else f"(不明:{genre_id})"

    def genre_of(self, course_id: str) -> str | None:
        c = self.courses.get(course_id)
        return c.genre_id if c else None
