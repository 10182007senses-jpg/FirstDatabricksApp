"""体験授業調整AI — Databricks Apps(Dash)の画面。

画面は表示と操作だけを担当し、計算は scheduler/manager.py の関数を呼びます。
"""
from __future__ import annotations

import base64
import calendar
import functools
import logging
import os
import time
from datetime import date

import dash
import dash_bootstrap_components as dbc
from dash import ALL, MATCH, Input, Output, State, ctx, dash_table, dcc, html
from dash.exceptions import PreventUpdate

from scheduler import llm, manager, messages, sample_data
from scheduler.data_access import MASTER_TABLES, STATUS_LABEL, Repo, month_range
from scheduler.models import PERIOD_LABEL, WEEKDAYS, Settings, slot_id_of
from scheduler.rules import week_label
from scheduler.solver import new_id

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

repo = Repo()
STARTUP_ERROR = "まだ準備していません"


def ensure_schema() -> None:
    """テーブルを用意する。失敗したら原因を覚えておき、次に画面を開いたときにもう一度試す。"""
    global STARTUP_ERROR
    if not STARTUP_ERROR:
        return
    try:
        repo.init_schema()
        STARTUP_ERROR = ""
    except Exception as exc:  # 画面に原因を出せるよう、起動は続ける
        log.exception("テーブルの準備に失敗しました")
        STARTUP_ERROR = str(exc)


ensure_schema()
llm_client = llm.LLMClient()

# Bootstrap の CSS は assets/ に同梱(学内ネットワークから外部 CDN に届かなくても表示が崩れないように)
app = dash.Dash(
    __name__,
    suppress_callback_exceptions=True,
    title="体験授業調整AI",
    update_title=None,
)
server = app.server

YES_NO = [{"label": "はい", "value": "はい"}, {"label": "いいえ", "value": "いいえ"}]
TABLE_STYLE = dict(
    style_cell={"fontFamily": "inherit", "fontSize": "14px", "padding": "6px 10px", "textAlign": "left",
                "whiteSpace": "normal", "height": "auto", "minWidth": "140px"},
    style_header={"fontWeight": "600", "backgroundColor": "#f3f4f6"},
    style_table={"overflowX": "auto"},
    css=[{"selector": ".Select-menu-outer", "rule": "display: block !important"}],
)


# ================================================================ 共通の部品

def default_month() -> tuple[int, int]:
    """調整は対象月の約2か月前に行うので、初期表示は2か月先。"""
    today = date.today()
    m = today.month + 2
    return today.year + (m - 1) // 12, (m - 1) % 12 + 1


def month_key(year, month) -> str:
    return f"{int(year)}-{int(month):02d}"


def short_slot(slot) -> str:
    return f"{slot.day.month}/{slot.day.day}({WEEKDAYS[slot.day.weekday()]}) {PERIOD_LABEL[slot.period]}"


def alert(text, color="success", notes=None):
    body = [html.Div(text)]
    if notes:
        body.append(html.Ul([html.Li(n) for n in notes], className="mb-0 mt-1"))
    return dbc.Alert(body, color=color, dismissable=True, className="mb-3")


def done(text, color="success", notes=None):
    """操作の結果を知らせて、画面を読み直す。"""
    return alert(text, color, notes), time.time()


def action(func):
    """操作のコールバック共通処理:押されていなければ何もしない。失敗は画面に出す。"""
    @functools.wraps(func)
    def wrapper(*args):
        if not ctx.triggered or not any(t["value"] for t in ctx.triggered):
            raise PreventUpdate
        try:
            return func(*args)
        except PreventUpdate:
            raise
        except Exception as exc:
            log.exception("操作に失敗しました")
            return alert(f"うまくいきませんでした:{exc}", "danger"), dash.no_update
    return wrapper


NOTICE = [Output("notice", "children", allow_duplicate=True), Output("refresh", "data", allow_duplicate=True)]
MONTH_STATE = [State("year", "value"), State("month", "value")]


def card(title, *children, hint=None):
    head = [html.H5(title, className="mb-1")]
    if hint:
        head.append(html.Div(hint, className="text-muted small mb-2"))
    return dbc.Card(dbc.CardBody(head + list(children)), className="mb-3 shadow-sm")


def decode_upload(contents: str) -> str:
    raw = base64.b64decode(contents.split(",", 1)[1])
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise ValueError("CSV の文字コードを読み取れません(UTF-8 か Shift_JIS で保存してください)")


def upload_button(component_id, label):
    return dcc.Upload(dbc.Button(label, color="secondary", outline=True, size="sm"),
                      id=component_id, accept=".csv", className="d-inline-block")


# ================================================================ 全体のレイアウト

def layout():
    ensure_schema()
    y, m = default_month()
    years = [{"label": f"{v}年", "value": v} for v in range(y - 2, y + 2)]
    months = [{"label": f"{v}月", "value": v} for v in range(1, 13)]
    return dbc.Container([
        dcc.Store(id="refresh", data=0),
        dcc.Download(id="download"),
        dbc.Row([
            dbc.Col(html.H3("体験授業調整AI", className="mb-0"), width="auto"),
            dbc.Col(html.Div("対象月", className="text-muted small"), width="auto", className="ms-4"),
            dbc.Col(dbc.Select(id="year", options=years, value=y, size="sm"), width="auto"),
            dbc.Col(dbc.Select(id="month", options=months, value=m, size="sm"), width="auto"),
            dbc.Col(html.Div(id="status"), width="auto"),
            dbc.Col(html.Div(
                f"保存先:{repo.db.label} / 文章生成AI:{'利用可' if llm_client.available else '未設定(定型文で動作)'}",
                className="text-muted small text-end")),
        ], align="center", className="py-3 border-bottom mb-3 g-2"),
        dbc.Alert(f"テーブルの準備に失敗しました:{STARTUP_ERROR}", color="danger") if STARTUP_ERROR else None,
        dbc.Tabs(id="tabs", active_tab="prep", className="mb-3", children=[
            dbc.Tab(label="① 準備", tab_id="prep"),
            dbc.Tab(label="② 割り当て案", tab_id="plan"),
            dbc.Tab(label="③ 連絡文", tab_id="msg"),
            dbc.Tab(label="マスター登録", tab_id="master"),
            dbc.Tab(label="設定", tab_id="settings"),
        ]),
        html.Div(id="notice"),
        dcc.Loading(html.Div(id="content"), type="dot", delay_show=400),
    ], fluid=True, className="px-4 pb-5")


app.layout = layout


@app.callback(
    Output("content", "children"), Output("status", "children"),
    Input("tabs", "active_tab"), Input("year", "value"), Input("month", "value"), Input("refresh", "data"),
)
def render(tab, year, month, _):
    try:
        bundle = repo.load(month_key(year, month))
        body = {"prep": prep_tab, "plan": plan_tab, "msg": msg_tab,
                "master": master_tab, "settings": settings_tab}[tab](bundle)
    except Exception as exc:
        log.exception("画面の表示に失敗しました")
        return alert(f"データを読み込めませんでした:{exc}", "danger"), ""
    color = {"preparing": "secondary", "draft": "warning", "confirmed": "success", "contacted": "primary"}
    badge = dbc.Badge(STATUS_LABEL[bundle.status], color=color[bundle.status], className="fs-6")
    return body, badge


@app.callback(Output("notice", "children"), Input("tabs", "active_tab"), Input("year", "value"),
              Input("month", "value"), prevent_initial_call=True)
def clear_notice(*_):
    return None


# ================================================================ ① 準備

def prep_tab(bundle):
    c = bundle.ctx
    if not c.teachers or not c.rooms or not c.courses:
        return alert("先に「マスター登録」で講師・体験授業・教室・ソフトを登録してください。"
                     "動きを試すだけなら、マスター登録の「デモデータを入れる」が使えます。", "info")
    return html.Div([slot_card(bundle), availability_card(bundle), block_card(bundle), target_card(bundle)])


def slot_card(bundle):
    year, month = int(bundle.month[:4]), int(bundle.month[5:7])
    picked = set(bundle.ctx.slots)
    rows = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        cells = []
        for d in week:
            if d.month != month:
                cells.append(html.Td(className="bg-light"))
                continue
            boxes = [
                dbc.Checkbox(id={"type": "slot", "key": slot_id_of(d, p)}, label=PERIOD_LABEL[p],
                             value=slot_id_of(d, p) in picked, className="small mb-0")
                for p in ("AM", "PM")
            ]
            cells.append(html.Td([html.Div(str(d.day), className="fw-bold")] + boxes,
                                 className="table-warning" if d.weekday() >= 5 else ""))
        rows.append(html.Tr(cells))
    table = dbc.Table([html.Thead(html.Tr([html.Th(w) for w in WEEKDAYS])), html.Tbody(rows)],
                      bordered=True, size="sm", className="mb-2", style={"tableLayout": "fixed"})
    return card(
        "1. 開催日と時間枠",
        table,
        dbc.Button("土日をまとめて選ぶ", id="slots-weekend", color="secondary", outline=True, size="sm",
                   className="me-2"),
        dbc.Button("すべて外す", id="slots-clear", color="secondary", outline=True, size="sm", className="me-2"),
        dbc.Button("開催枠を保存", id="slots-save", color="primary", size="sm"),
        hint="体験授業を開催する日の午前・午後にチェックを入れます。3・7・8月は平日も選べます。",
    )


@app.callback(Output({"type": "slot", "key": ALL}, "value"),
              Input("slots-weekend", "n_clicks"), Input("slots-clear", "n_clicks"),
              State({"type": "slot", "key": ALL}, "id"), prevent_initial_call=True)
def pick_slots(_w, _c, ids):
    if not ctx.triggered or not ctx.triggered[0]["value"]:
        raise PreventUpdate
    if ctx.triggered_id == "slots-clear":
        return [False] * len(ids)
    return [date.fromisoformat(i["key"][:10]).weekday() >= 5 for i in ids]


@app.callback(*NOTICE, Input("slots-save", "n_clicks"),
              State({"type": "slot", "key": ALL}, "value"), State({"type": "slot", "key": ALL}, "id"),
              *MONTH_STATE, prevent_initial_call=True)
@action
def save_slots(_, values, ids, year, month):
    picks = [(date.fromisoformat(i["key"][:10]), i["key"][11:]) for v, i in zip(values, ids) if v]
    repo.save_slots(month_key(year, month), picks)
    return done(f"開催枠を{len(picks)}件保存しました。")


def availability_card(bundle):
    c = bundle.ctx
    slots = c.sorted_slots()
    if not slots:
        body = [html.Div("開催枠を保存すると、ここに講師×枠の表が出ます。", className="text-muted")]
    else:
        head = html.Tr([html.Th("講師")] + [html.Th(short_slot(s), className="small text-center") for s in slots])
        rows = []
        for t in sorted(c.teachers.values(), key=lambda t: t.name):
            if not t.active:
                continue
            rows.append(html.Tr([html.Td(t.name, className="text-nowrap")] + [
                html.Td(dbc.Checkbox(id={"type": "av", "t": t.teacher_id, "s": s.slot_id},
                                     value=(t.teacher_id, s.slot_id) in c.availability),
                        className="text-center")
                for s in slots
            ]))
        body = [html.Div(dbc.Table([html.Thead(head), html.Tbody(rows)], bordered=True, size="sm", hover=True),
                         style={"overflowX": "auto"})]
    return card(
        "2. 講師の対応可能日(F1)",
        html.Div([
            dbc.Button("フォームの回答を取り込む", id="av-form", color="secondary", outline=True, size="sm",
                       className="me-2"),
            upload_button("av-upload", "CSVから取り込む(講師名, 日付, 時間帯)"),
        ], className="mb-2 d-flex align-items-center"),
        *body,
        dbc.Button("対応可能日を保存", id="av-save", color="primary", size="sm") if slots else None,
        hint=f"Googleフォームの回答を取り込むか、表に直接チェックを入れます。現在 {len(c.availability)} 件。",
    )


@app.callback(*NOTICE, Input("av-form", "n_clicks"), *MONTH_STATE, prevent_initial_call=True)
@action
def import_form(_, year, month):
    n, notes = manager.import_availability_from_form(repo, month_key(year, month))
    return done(f"フォームの回答から{n}件を取り込みました。", "success" if n else "warning", notes)


@app.callback(*NOTICE, Input("av-upload", "contents"), *MONTH_STATE, prevent_initial_call=True)
@action
def import_csv(contents, year, month):
    records = manager.parse_availability_csv(decode_upload(contents))
    n, notes = manager.import_availability(repo, month_key(year, month), records)
    return done(f"CSVから{n}件を取り込みました。", "success" if n else "warning", notes)


@app.callback(*NOTICE, Input("av-save", "n_clicks"),
              State({"type": "av", "t": ALL, "s": ALL}, "value"), State({"type": "av", "t": ALL, "s": ALL}, "id"),
              *MONTH_STATE, prevent_initial_call=True)
@action
def save_availability(_, values, ids, year, month):
    pairs = {(i["t"], i["s"]) for v, i in zip(values, ids) if v}
    repo.save_availability(month_key(year, month), pairs)
    return done(f"対応可能日を{len(pairs)}件保存しました。")


def block_card(bundle):
    c = bundle.ctx
    days = sorted({s.day for s in c.slots.values()})
    grouped: dict = {}
    for (room_id, slot_id), why in c.room_blocks.items():
        s = c.slots[slot_id]
        grouped.setdefault((s.day, room_id, why), set()).add(s.period)
    data = [
        {"day": d.isoformat(), "period": "終日" if len(ps) == 2 else PERIOD_LABEL[next(iter(ps))],
         "room_id": r, "reason": why}
        for (d, r, why), ps in sorted(grouped.items())
    ]
    table = dash_table.DataTable(
        id="blocks-table", data=data, editable=True, row_deletable=True,
        columns=[
            {"name": "日付", "id": "day", "presentation": "dropdown"},
            {"name": "時間帯", "id": "period", "presentation": "dropdown"},
            {"name": "教室", "id": "room_id", "presentation": "dropdown"},
            {"name": "理由(行事名など)", "id": "reason"},
        ],
        dropdown={
            "day": {"options": [{"label": f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})", "value": d.isoformat()}
                                for d in days]},
            "period": {"options": [{"label": v, "value": v} for v in ("午前", "午後", "終日")]},
            "room_id": {"options": [{"label": n, "value": r} for r, n in sorted(c.rooms.items(), key=lambda x: x[1])]},
        },
        **TABLE_STYLE,
    )
    return card(
        "3. 使えない教室・日時(F3)",
        table,
        html.Div([
            dbc.Button("行を追加", id="blocks-add", color="secondary", outline=True, size="sm", className="me-2"),
            dbc.Button("使えない教室を保存", id="blocks-save", color="primary", size="sm"),
        ], className="mt-2"),
        hint="教務側の行事(オリエンテーション、保護者会、esports大会など)で使えない教室を登録します。"
             "ここに登録漏れがあると、割り当て案が行事と重なります。",
    )


@app.callback(Output("blocks-table", "data"), Input("blocks-add", "n_clicks"), State("blocks-table", "data"),
              prevent_initial_call=True)
def add_block_row(n, data):
    if not n:
        raise PreventUpdate
    return (data or []) + [{"day": None, "period": "終日", "room_id": None, "reason": ""}]


@app.callback(*NOTICE, Input("blocks-save", "n_clicks"), State("blocks-table", "data"), *MONTH_STATE,
              prevent_initial_call=True)
@action
def save_blocks(_, data, year, month):
    mk = month_key(year, month)
    slots = repo.load(mk).ctx.slots
    blocks, skipped = {}, 0
    for row in data or []:
        if not row.get("day") or not row.get("room_id"):
            skipped += 1
            continue
        periods = ("AM", "PM") if row.get("period") == "終日" else \
            ("AM",) if row.get("period") == "午前" else ("PM",)
        for p in periods:
            sid = slot_id_of(date.fromisoformat(row["day"]), p)
            if sid in slots:
                blocks[(row["room_id"], sid)] = (row.get("reason") or "").strip()
    repo.save_room_blocks(mk, blocks)
    notes = [f"日付か教室が空の行を{skipped}件読み飛ばしました"] if skipped else None
    return done(f"使えない教室を{len(blocks)}枠分保存しました。", notes=notes)


def target_card(bundle):
    c = bundle.ctx
    weeks = sorted({s.week_key for s in c.slots.values()})
    genres = sorted(c.genres.values(), key=lambda g: g.name)
    data = [
        {"week_key": w, "week": week_label(c, w),
         **{g.genre_id: c.week_targets.get((w, g.genre_id)) for g in genres}}
        for w in weeks
    ]
    table = dash_table.DataTable(
        id="targets-table", data=data, editable=True,
        columns=[{"name": "週", "id": "week", "editable": False}] + [
            {"name": f"{g.name}系", "id": g.genre_id, "type": "numeric"} for g in genres],
        **TABLE_STYLE,
    )
    return card(
        "4. 週ごとの打ち出し方の目安(R6・任意)",
        table,
        dbc.Button("目安を保存", id="targets-save", color="primary", size="sm", className="mt-2"),
        hint="「この週はIT系を多めに」のような方針を、系統ごとのコマ数の目安として入れます。空欄なら考慮しません。",
    )


@app.callback(*NOTICE, Input("targets-save", "n_clicks"), State("targets-table", "data"), *MONTH_STATE,
              prevent_initial_call=True)
@action
def save_targets(_, data, year, month):
    targets = {}
    for row in data or []:
        for key, value in row.items():
            if key in ("week", "week_key") or value in (None, ""):
                continue
            targets[(row["week_key"], key)] = int(value)
    repo.save_week_targets(month_key(year, month), targets)
    return done("週ごとの目安を保存しました。")


# ================================================================ ② 割り当て案

def findings_block(title, findings, color):
    if not findings:
        return None
    return dbc.Alert([html.Strong(title), html.Ul(
        [html.Li([dbc.Badge(f.rule, color=color, className="me-2"), f.message]) for f in findings],
        className="mb-0 mt-1")], color=color, className="py-2")


def plan_tab(bundle):
    c = bundle.ctx
    if not c.slots:
        return alert("先に「① 準備」で開催枠と対応可能日を登録してください。", "info")
    locked = bundle.status in ("confirmed", "contacted")
    rv = manager.review(bundle.assignments, c)
    flagged = {aid for f in rv.must for aid in f.assignment_ids}

    def stat(label, n, color):
        return dbc.Col(dbc.Card(dbc.CardBody([
            html.Div(label, className="text-muted small"),
            html.Div(str(n), className=f"fs-3 fw-bold text-{color if n else 'success'}"),
        ], className="py-2"), className="shadow-sm"), width="auto")

    buttons = [
        dbc.Button("割り当て案を作る", id="plan-create", color="primary", className="me-2", disabled=locked),
        dbc.Button("AIで理由を読みやすくする", id="plan-polish", color="secondary", outline=True, className="me-2",
                   disabled=locked or not bundle.assignments or not llm_client.available),
        dbc.Button("確定を解除", id="plan-unconfirm", color="warning", outline=True, className="me-2") if locked
        else dbc.Button("この内容で確定する", id="plan-confirm", color="success", className="me-2",
                        disabled=bool(rv.must) or not bundle.assignments),
        dbc.Button("CSVで保存", id="plan-export", color="secondary", outline=True, className="me-2",
                   disabled=not bundle.assignments),
    ]
    if not locked:
        buttons.append(upload_button("plan-upload", "過去の割り当てCSVを読み込む"))

    data = [
        {"assignment_id": a.assignment_id, "slot_id": a.slot_id, "course_id": a.course_id,
         "teacher_id": a.teacher_id, "room_id": a.room_id, "reason": a.reason}
        for a in bundle.assignments
    ]
    table = dash_table.DataTable(
        id="plan-table", data=data, editable=not locked, row_deletable=not locked,
        columns=[
            {"name": "日付・時間帯", "id": "slot_id", "presentation": "dropdown"},
            {"name": "体験授業", "id": "course_id", "presentation": "dropdown"},
            {"name": "講師", "id": "teacher_id", "presentation": "dropdown"},
            {"name": "教室", "id": "room_id", "presentation": "dropdown"},
            {"name": "理由", "id": "reason", "editable": False},
        ],
        dropdown={
            "slot_id": {"options": [{"label": short_slot(s), "value": s.slot_id} for s in c.sorted_slots()]},
            "course_id": {"options": [{"label": x.name, "value": x.course_id}
                                      for x in sorted(c.courses.values(), key=lambda x: x.name)]},
            "teacher_id": {"options": [{"label": t.name, "value": t.teacher_id}
                                       for t in sorted(c.teachers.values(), key=lambda t: t.name)]},
            "room_id": {"options": [{"label": n, "value": r} for r, n in sorted(c.rooms.items(), key=lambda x: x[1])]},
        },
        style_data_conditional=[
            {"if": {"row_index": i}, "backgroundColor": "#fdecea"}
            for i, row in enumerate(data) if row["assignment_id"] in flagged
        ] + [{"if": {"column_id": "reason"}, "color": "#374151"}],
        style_cell_conditional=[
            {"if": {"column_id": "slot_id"}, "minWidth": "150px"},
            {"if": {"column_id": "course_id"}, "minWidth": "250px"},
            {"if": {"column_id": "teacher_id"}, "minWidth": "130px"},
            {"if": {"column_id": "room_id"}, "minWidth": "100px"},
            {"if": {"column_id": "reason"}, "minWidth": "360px"},
        ],
        **TABLE_STYLE,
    )
    edit_row = None if locked else dbc.Row([
        dbc.Col(dbc.Button("行を追加", id="plan-add", color="secondary", outline=True, size="sm"), width="auto"),
        dbc.Col(dbc.Input(id="plan-change-reason", placeholder="直した理由(任意。次回以降のルール見直しに使います)",
                          size="sm")),
        dbc.Col(dbc.Button("修正を保存して再チェック", id="plan-save", color="primary", size="sm"), width="auto"),
    ], className="mt-2 g-2", align="center")

    return html.Div([
        html.Div(buttons, className="mb-3 d-flex flex-wrap align-items-center"),
        dbc.Row([
            stat("割り当て", len(bundle.assignments), "dark"),
            stat("必ず守るルールの違反", len(rv.must), "danger"),
            stat("なるべく守るルールの未充足", len(rv.should), "warning"),
            stat("警告", len(rv.warnings), "warning"),
            stat("先生の手修正(累計)", repo.count_changes(bundle.month), "dark"),
        ], className="g-2 mb-3"),
        alert("確定済みです。直す場合は「確定を解除」を押してください。", "success") if locked else None,
        findings_block("必ず守るルール(Must)の違反 — 解消するまで確定できません", rv.must, "danger"),
        findings_block("なるべく守るルール(Should)で満たせていないもの", rv.should, "warning"),
        findings_block("確認してほしいこと", rv.warnings, "info"),
        dbc.Alert([html.Strong("組めなかった理由(F5)"),
                   html.Ul([html.Li(n) for n in rv.unplaced], className="mb-0 mt-1")],
                  color="secondary", className="py-2") if rv.unplaced and bundle.assignments else None,
        card("割り当て表", table, edit_row,
             hint="セルを選ぶと直せます。赤い行は必ず守るルールに違反しています。") if bundle.assignments
        else alert("まだ案がありません。「割り当て案を作る」を押してください。", "info"),
    ])


@app.callback(*NOTICE, Input("plan-create", "n_clicks"), *MONTH_STATE, prevent_initial_call=True)
@action
def create_plan(_, year, month):
    n = manager.create_plan(repo, month_key(year, month))
    if n == 0:
        return done("割り当てられる組み合わせがありませんでした。対応可能日・教室のソフト・使えない教室を"
                    "確認してください。", "warning")
    return done(f"{n}件の割り当て案を作りました。内容を確認し、必要なら直してから確定してください。")


@app.callback(*NOTICE, Input("plan-polish", "n_clicks"), *MONTH_STATE, prevent_initial_call=True)
@action
def polish_plan(_, year, month):
    if manager.polish_reasons(repo, month_key(year, month), llm_client):
        return done("理由の文をAIで整えました(講師名はAIに渡していません)。")
    return done("AIを呼び出せなかったため、理由は元の文のままです。", "warning")


@app.callback(Output("plan-table", "data"), Input("plan-add", "n_clicks"), State("plan-table", "data"),
              prevent_initial_call=True)
def add_plan_row(n, data):
    if not n:
        raise PreventUpdate
    return (data or []) + [{"assignment_id": "", "slot_id": None, "course_id": None, "teacher_id": None,
                            "room_id": None, "reason": "(新しい行。保存すると理由が付きます)"}]


@app.callback(*NOTICE, Input("plan-save", "n_clicks"), State("plan-table", "data"),
              State("plan-change-reason", "value"), *MONTH_STATE, prevent_initial_call=True)
@action
def save_plan(_, data, reason, year, month):
    rows = data or []
    if any(not row.get(k) for row in rows for k in ("slot_id", "course_id", "teacher_id", "room_id")):
        return alert("日付・授業・講師・教室が空の行があります。埋めるか、行を削除してから保存してください。",
                     "danger"), dash.no_update
    n = manager.save_edits(repo, month_key(year, month), rows, (reason or "").strip())
    return done(f"{n}件の修正を保存し、ルールを再チェックしました。" if n else "変更はありませんでした。")


@app.callback(*NOTICE, Input("plan-confirm", "n_clicks"), *MONTH_STATE, prevent_initial_call=True)
@action
def confirm_plan(_, year, month):
    violations = manager.confirm(repo, month_key(year, month))
    if violations:
        return done("必ず守るルールの違反があるため、確定できません。", "danger")
    return done("割り当てを確定しました。「③ 連絡文」で下書きを作れます。")


@app.callback(*NOTICE, Input("plan-unconfirm", "n_clicks"), *MONTH_STATE, prevent_initial_call=True)
@action
def unconfirm_plan(_, year, month):
    manager.unconfirm(repo, month_key(year, month))
    return done("確定を解除しました。連絡文の下書きは作り直しになります。", "warning")


@app.callback(*NOTICE, Input("plan-upload", "contents"), *MONTH_STATE, prevent_initial_call=True)
@action
def upload_plan(contents, year, month):
    n, notes = manager.import_assignments_csv(repo, month_key(year, month), decode_upload(contents))
    return done(f"{n}件の割り当てを読み込み、ルールをかけました。", "success" if n else "warning", notes)


@app.callback(Output("download", "data"), Input("plan-export", "n_clicks"), *MONTH_STATE,
              prevent_initial_call=True)
def export_plan(n, year, month):
    if not n:
        raise PreventUpdate
    bundle = repo.load(month_key(year, month))
    return dict(content=manager.export_csv(bundle.assignments, bundle.ctx),
                filename=f"taiken_wariate_{bundle.month}.csv", type="text/csv")


# ================================================================ ③ 連絡文

def msg_tab(bundle):
    c = bundle.ctx
    if bundle.status not in ("confirmed", "contacted"):
        return alert("連絡文は、「② 割り当て案」で割り当てを確定してから作ります。", "info")
    controls = dbc.Row([
        dbc.Col(dbc.Input(id="msg-sender", placeholder="差出人の名前(例:鈴木)", size="sm"), width=3),
        dbc.Col(dbc.Checkbox(id="msg-ai", label="あいさつ文をAIで整える", value=False,
                             disabled=not llm_client.available), width="auto"),
        dbc.Col(dbc.Button("連絡文の下書きを作る", id="msg-create", color="primary", size="sm"), width="auto"),
        dbc.Col(dbc.Button("送信が済んだら「連絡済」にする", id="msg-sent", color="success", outline=True, size="sm",
                           disabled=not bundle.messages or bundle.status == "contacted"), width="auto"),
    ], className="g-2 mb-3", align="center")
    if not bundle.messages:
        return html.Div([controls, alert("まだ下書きがありません。", "info")])

    problems = messages.verify_messages(bundle.messages, bundle.assignments, c)
    check = alert("確定した割り当てと連絡文の照合:食い違い 0件", "success") if not problems else \
        alert("確定した割り当てと連絡文に食い違いがあります。下書きを作り直してください。", "danger", problems)
    order = {"notice": 0, "thanks": 1, "hp": 2}
    items = []
    for i, m in enumerate(sorted(bundle.messages, key=lambda m: (order[m.kind], c.teacher_name(m.teacher_id or "")))):
        to = "HP業者" if m.kind == "hp" else c.teacher_name(m.teacher_id)
        items.append(dbc.AccordionItem([
            html.Div([html.Span("件名:", className="text-muted"), html.Span(m.subject, id=f"msg-subject-{i}"),
                      dcc.Clipboard(target_id=f"msg-subject-{i}", title="件名をコピー",
                                    className="d-inline-block ms-2 text-primary")], className="mb-2"),
            html.Div([
                dcc.Clipboard(target_id=f"msg-body-{i}", title="本文をコピー",
                              className="float-end text-primary fs-5"),
                html.Pre(m.body, id=f"msg-body-{i}", className="mb-0",
                         style={"whiteSpace": "pre-wrap", "fontFamily": "inherit", "fontSize": "14px"}),
            ], className="border rounded p-3 bg-light"),
        ], title=f"{to} 宛て — {messages.KIND_LABEL[m.kind]}"))
    return html.Div([
        controls, check,
        html.Div("右上のアイコンで本文をコピーし、ふだんのメールに貼り付けて送ってください。送信は先生が行います。",
                 className="text-muted small mb-2"),
        dbc.Accordion(items, start_collapsed=True, always_open=True),
    ])


@app.callback(*NOTICE, Input("msg-create", "n_clicks"), State("msg-sender", "value"), State("msg-ai", "value"),
              *MONTH_STATE, prevent_initial_call=True)
@action
def create_messages(_, sender, use_ai, year, month):
    problems = manager.create_messages(repo, month_key(year, month), (sender or "").strip(),
                                       llm_client if use_ai else None)
    if problems:
        return done("下書きを作りましたが、割り当てとの食い違いがあります。", "danger", problems)
    return done("連絡文の下書きを作りました。日付・授業・教室は確定した割り当てから差し込んでいます。")


@app.callback(*NOTICE, Input("msg-sent", "n_clicks"), *MONTH_STATE, prevent_initial_call=True)
@action
def mark_sent(_, year, month):
    manager.mark_contacted(repo, month_key(year, month))
    return done("この月を「連絡済」にしました。")


# ================================================================ マスター登録

# (テーブル, タブ名, 説明, [(列, 見出し, 種類)])  種類: text / bool / date / ref:参照先テーブル
MASTERS = [
    ("teacher", "講師", "体験授業を担当する講師。", [("name", "名前", "text"), ("active", "在籍中", "bool")]),
    ("teacher_course", "講師×授業", "どの講師がどの体験授業を担当できるか。",
     [("teacher_id", "講師", "ref:teacher"), ("course_id", "体験授業", "ref:course")]),
    ("course", "体験授業", "体験授業と、その系統。", [("name", "体験授業名", "text"), ("genre_id", "系統", "ref:genre")]),
    ("course_software", "授業×ソフト", "体験授業で使うソフト。ここにあるソフトが入った教室にだけ割り当てます(R2)。",
     [("course_id", "体験授業", "ref:course"), ("software_id", "ソフト", "ref:software")]),
    ("genre", "系統", "「2週続けて空けない」(R5)の対象にする系統は「はい」にします。",
     [("name", "系統名", "text"), ("r5_target", "R5の対象", "bool")]),
    ("genre_substitute", "代わりの系統", "左の系統を入れられない週に、代わりにできる近い系統(例:CG → VFX)。",
     [("genre_id", "系統", "ref:genre"), ("substitute_genre_id", "代わりにできる系統", "ref:genre")]),
    ("room", "教室", "体験授業で使う教室。", [("name", "教室名", "text")]),
    ("room_software", "教室×ソフト", "教室に入っているソフト。春や長期休みの入れ替え時に、機材担当に確認して更新します。"
     "日付は 2026-04-01 の形で入れます。確認日が古いと警告が出ます(R10)。",
     [("room_id", "教室", "ref:room"), ("software_id", "ソフト", "ref:software"),
      ("valid_from", "導入日", "date"), ("valid_to", "外した日", "date"), ("confirmed_on", "確認日", "date")]),
    ("software", "ソフト", "体験授業で使うソフト。", [("name", "ソフト名", "text")]),
]
MASTER_SPEC = {name: cols for name, _, _, cols in MASTERS}
ID_COLUMN = {"teacher": "teacher_id", "course": "course_id", "genre": "genre_id",
             "room": "room_id", "software": "software_id"}


def master_tab(bundle):
    names = {t: {r[ID_COLUMN[t]]: r["name"] for r in repo.read(t)} for t in ID_COLUMN}
    tabs = []
    for table, label, hint, cols in MASTERS:
        rows = repo.read(table)
        data, columns, dropdown = [], [], {}
        for field, title, kind in cols:
            col = {"name": title, "id": field}
            if kind == "bool":
                col["presentation"] = "dropdown"
                dropdown[field] = {"options": YES_NO}
            elif kind.startswith("ref:"):
                col["presentation"] = "dropdown"
                dropdown[field] = {"options": [{"label": n, "value": i} for i, n in
                                               sorted(names[kind[4:]].items(), key=lambda x: x[1])]}
            columns.append(col)
        for r in rows:
            row = dict(r)
            for field, _, kind in cols:
                if kind == "bool":
                    row[field] = "いいえ" if r[field] is False else "はい"
                elif kind == "date":
                    row[field] = r[field].isoformat() if r[field] else ""
            data.append(row)
        sort_key = cols[0][0]
        data.sort(key=lambda r: str(names.get(MASTER_SPEC[table][0][2][4:], {}).get(r[sort_key], r[sort_key])))
        tabs.append(dbc.Tab(label=f"{label}({len(data)})", tab_id=table, children=html.Div([
            html.Div(hint, className="text-muted small my-2"),
            dash_table.DataTable(id={"type": "master", "name": table}, data=data, columns=columns,
                                 dropdown=dropdown, editable=True, row_deletable=True, **TABLE_STYLE),
            html.Div([
                dbc.Button("行を追加", id={"type": "master-add", "name": table}, color="secondary", outline=True,
                           size="sm", className="me-2"),
                dbc.Button(f"「{label}」を保存", id={"type": "master-save", "name": table}, color="primary",
                           size="sm"),
            ], className="mt-2"),
        ])))
    demo = dcc.ConfirmDialogProvider(
        dbc.Button("デモデータを入れる", color="secondary", outline=True, size="sm"), id="demo-seed",
        message="マスターをすべて架空のデモデータに置き換え、表示中の月に開催枠と対応可能日を入れます。よろしいですか?")
    return html.Div([
        html.Div([html.Span("講師・授業・教室・ソフトの登録(F2)。", className="me-3"), demo],
                 className="mb-2 d-flex align-items-center"),
        # 保存して画面を読み直しても、開いていたタブに戻る
        dbc.Tabs(tabs, id="master-tabs", active_tab="teacher", persistence=True, persistence_type="session"),
    ])


@app.callback(Output({"type": "master", "name": MATCH}, "data"),
              Input({"type": "master-add", "name": MATCH}, "n_clicks"),
              State({"type": "master", "name": MATCH}, "data"), State({"type": "master", "name": MATCH}, "id"),
              prevent_initial_call=True)
def add_master_row(n, data, table_id):
    if not n:
        raise PreventUpdate
    blank = {field: ("はい" if kind == "bool" else None if kind.startswith("ref:") else "")
             for field, _, kind in MASTER_SPEC[table_id["name"]]}
    return (data or []) + [blank]


def parse_master_rows(table: str, data: list[dict]) -> list[dict]:
    spec = MASTER_SPEC[table]
    id_col = ID_COLUMN.get(table)
    rows, seen = [], set()
    for n, raw in enumerate(data or [], start=1):
        row = {}
        for field, title, kind in spec:
            value = raw.get(field)
            if kind == "bool":
                row[field] = value != "いいえ"
            elif kind == "date":
                text = str(value or "").strip().replace("/", "-")
                try:
                    row[field] = date.fromisoformat(text) if text else None
                except ValueError:
                    raise ValueError(f"{n}行目の「{title}」は 2026-04-01 の形で入れてください(入力:{value})")
            else:
                row[field] = str(value).strip() if value not in (None, "") else None
        required = [f for f, _, k in spec if k == "text" or k.startswith("ref:")]
        if any(row[f] is None for f in required):
            if all(row[f] is None for f in required):
                continue  # 空行は捨てる
            raise ValueError(f"{n}行目に空欄があります。埋めるか、行を削除してください")
        key = row["name"] if id_col else tuple(row[f] for f in required)
        if key in seen:
            raise ValueError(f"{n}行目が他の行と重複しています")
        seen.add(key)
        if id_col:
            row[id_col] = raw.get(id_col) or f"{table[0]}_{new_id()}"
        rows.append(row)
    return rows


@app.callback(*NOTICE, Input({"type": "master-save", "name": ALL}, "n_clicks"),
              State({"type": "master", "name": ALL}, "data"), State({"type": "master", "name": ALL}, "id"),
              prevent_initial_call=True)
@action
def save_master(_, all_data, ids):
    table = ctx.triggered_id["name"]
    data = next(d for d, i in zip(all_data, ids) if i["name"] == table)
    rows = parse_master_rows(table, data)
    repo.replace(table, rows)
    label = next(lbl for name, lbl, _, _ in MASTERS if name == table)
    return done(f"「{label}」を{len(rows)}件保存しました。")


@app.callback(*NOTICE, Input("demo-seed", "submit_n_clicks"), *MONTH_STATE, prevent_initial_call=True)
@action
def seed_demo(_, year, month):
    mk = month_key(year, month)
    sample_data.seed_masters(repo)
    sample_data.seed_month(repo, mk)
    repo.save_assignments(mk, [])
    repo.save_messages(mk, [])
    repo.set_status(mk, "preparing")
    return done("デモデータを入れました。「① 準備」→「② 割り当て案」の順に試せます。")


# ================================================================ 設定

SETTING_LABELS = [
    ("per_slot_target", "1枠あたりの授業数の目安", "この数までは積極的に埋めます"),
    ("per_slot_max", "1枠あたりの授業数の上限", "これを超えては入れません"),
    ("w_r5", "重み:同じ系統が2週続けて空かない(R5)", ""),
    ("w_r8", "重み:どの講師も月に1回(R8)", ""),
    ("w_r6", "重み:週ごとの目安に近づける(R6)", ""),
    ("w_r10", "重み:3月は空き教室を残す(R10)", ""),
    ("w_fill", "重み:枠を目安の数まで埋める", ""),
    ("w_balance", "重み:特定の講師に偏らせない", ""),
    ("reserve_rooms", "3月に各枠で残す空き教室の数", ""),
    ("stale_days", "ソフト情報の確認日が古いとみなす日数", "これより古い情報を使うと警告を出します"),
]


def settings_tab(bundle):
    s = bundle.ctx.settings
    rows = [
        dbc.Row([
            dbc.Col(html.Div([html.Div(label), html.Div(hint, className="text-muted small")]), width=5),
            dbc.Col(dbc.Input(id={"type": "setting", "key": key}, type="number", min=0, step=1,
                              value=getattr(s, key), size="sm"), width=2),
        ], className="mb-2", align="center")
        for key, label, hint in SETTING_LABELS
    ]
    return card(
        "割り当ての作り方の設定",
        *rows,
        dbc.Button("設定を保存", id="settings-save", color="primary", size="sm", className="mt-2"),
        hint="なるべく守るルール(Should)どうしがぶつかったときは、重みが大きいほうを優先します。"
             "初期値は仮のものです。先生と相談して決めた値に直してください。",
    )


@app.callback(*NOTICE, Input("settings-save", "n_clicks"),
              State({"type": "setting", "key": ALL}, "value"), State({"type": "setting", "key": ALL}, "id"),
              prevent_initial_call=True)
@action
def save_settings(_, values, ids):
    s = Settings()
    for value, i in zip(values, ids):
        if value is None or int(value) < 0:
            return alert("空欄または負の値があります。0以上の整数を入れてください。", "danger"), dash.no_update
        setattr(s, i["key"], int(value))
    if s.per_slot_max < max(1, s.per_slot_target):
        return alert("「上限」は「目安」以上、かつ1以上にしてください。", "danger"), dash.no_update
    repo.save_settings(s)
    return done("設定を保存しました。次に案を作るときから反映されます。")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("DATABRICKS_APP_PORT", "8000")), debug=False)
