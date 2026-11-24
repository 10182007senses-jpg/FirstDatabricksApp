"""体験授業調整AI — Databricks Apps(Dash)の画面。

画面は表示と操作だけを担当し、計算は scheduler/manager.py の関数を呼びます。

見た目は Dash Mantine Components(assets/theme.py の THEME)でそろえています。
トーン&マナー:静かで整った業務ツール。白地に濃いグレーの文字、色は落ち着いた青1色だけ。
表は細い罫線で余白を広めに。違反は赤ではなく、注意を示すオレンジのバッジで出します。
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
import dash_mantine_components as dmc
from dash import ALL, MATCH, Input, Output, State, ctx, dash_table, dcc, html
from dash.exceptions import PreventUpdate

from scheduler import llm, manager, messages, sample_data, sheet_import
from scheduler.data_access import STATUS_LABEL, Repo
from scheduler.models import PERIOD_LABEL, PRIORITY_LABEL, WEEKDAYS, Settings, slot_id_of
from scheduler.rules import week_label
from scheduler.solver import new_id
from theme import ACCENT, BORDER, MUTED, TABLE_STYLE, THEME

# Dash 2 系で Mantine(React 18 が必要)を使うための設定
dash._dash_renderer._set_react_version("18.2.0")

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

app = dash.Dash(__name__, suppress_callback_exceptions=True, title="体験授業調整AI", update_title=None)
server = app.server


# ================================================================ 共通の部品

def default_month() -> tuple[int, int]:
    """調整は対象月の約2か月前に行うので(ルール一覧 No.2)、初期表示は2か月先。"""
    today = date.today()
    m = today.month + 2
    return today.year + (m - 1) // 12, (m - 1) % 12 + 1


def month_key(year, month) -> str:
    return f"{int(year)}-{int(month):02d}"


def short_slot(slot) -> str:
    return f"{slot.day.month}/{slot.day.day}({WEEKDAYS[slot.day.weekday()]}) {PERIOD_LABEL[slot.period]}"


ALERT_COLOR = {"success": "teal", "info": "blue", "warning": "orange", "danger": "orange", "secondary": "gray"}


def alert(text, color="success", notes=None, title=None):
    body = [dmc.Text(text, size="sm")]
    if notes:
        body.append(dmc.List([dmc.ListItem(dmc.Text(n, size="sm")) for n in notes], mt=4, size="sm"))
    return dmc.Alert(body, title=title, color=ALERT_COLOR.get(color, color), variant="light",
                     withCloseButton=True, radius="md", mb="md")


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


def section(title, *children, hint=None, step=None, right=None):
    """白いカード1枚=作業のひとまとまり。見出し・説明・中身の順に並べる。"""
    head = [dmc.Group([
        dmc.Group([dmc.Badge(step, variant="outline", color="gray", radius="sm") if step else None,
                   dmc.Title(title, order=4, fw=600)], gap="xs"),
        right,
    ], justify="space-between", align="center")]
    if hint:
        head.append(dmc.Text(hint, size="sm", c="dimmed", mt=4))
    return dmc.Paper(head + [html.Div(list(children), style={"marginTop": 16})],
                     withBorder=True, radius="md", p="lg", mb="md")


def button(label, id, primary=False, **kw):
    return dmc.Button(label, id=id, variant="filled" if primary else "default", size="sm", radius="md", **kw)


def decode_upload(contents: str) -> str:
    raw = base64.b64decode(contents.split(",", 1)[1])
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise ValueError("CSV の文字コードを読み取れません(UTF-8 か Shift_JIS で保存してください)")


def upload_bytes(contents: str) -> bytes:
    return base64.b64decode(contents.split(",", 1)[1])


def upload_button(component_id, label, accept=".csv"):
    return dcc.Upload(dmc.Button(label, variant="default", size="sm", radius="md"),
                      id=component_id, accept=accept, style={"display": "inline-block"})


def stat(label, n, warn=False):
    color = "orange" if warn and n else "dark"
    return dmc.Paper([
        dmc.Text(label, size="xs", c="dimmed"),
        dmc.Text(str(n), fw=600, size="xl", c=color),
    ], withBorder=True, radius="md", px="md", py="sm", style={"minWidth": 150})


# ================================================================ 全体のレイアウト

STEPS = [("prep", "① 準備"), ("plan", "② 割り当て案"), ("msg", "③ 連絡文"), ("master", "マスター"),
         ("settings", "設定")]


def layout():
    ensure_schema()
    y, m = default_month()
    years = [{"label": f"{v}年", "value": str(v)} for v in range(y - 2, y + 2)]
    months = [{"label": f"{v}月", "value": str(v)} for v in range(1, 13)]
    header = dmc.Group([
        dmc.Group([
            dmc.Title("体験授業調整AI", order=3, fw=700),
            dmc.Divider(orientation="vertical"),
            dmc.Text("対象月", size="sm", c="dimmed"),
            dmc.Select(id="year", data=years, value=str(y), w=100, size="sm", allowDeselect=False),
            dmc.Select(id="month", data=months, value=str(m), w=80, size="sm", allowDeselect=False),
            html.Div(id="status"),
            html.Div(id="dirty-badge"),
        ], gap="sm", wrap="nowrap"),
        dmc.Tooltip(dmc.Text(f"保存先:{repo.db.label}", size="xs", c="dimmed", truncate="end", maw=260),
                    label=f"保存先:{repo.db.label} / 文章生成AI:{'利用可' if llm_client.available else '未設定(定型文で動作)'}"),
    ], justify="space-between", h="100%", px="xl", wrap="nowrap")
    return dmc.MantineProvider(theme=THEME, children=dmc.AppShell([
        dmc.AppShellHeader(header, style={"borderBottom": f"1px solid {BORDER}"}),
        dmc.AppShellMain(dmc.Container([
            dcc.Store(id="refresh", data=0),
            dcc.Store(id="dirty", data=False),  # 画面で直して、まだ保存していない変更があるか
            dcc.Store(id="shown-tab", data="prep"),  # いま表示しているタブ
            dcc.Store(id="pending-tab"),  # 保存していない変更があるときに、移ろうとしたタブ
            html.Div(id="unload-guard", style={"display": "none"}),
            dcc.Download(id="download"),
            dmc.Modal(id="leave-modal", title="保存していない変更があります", centered=True, radius="md", children=[
                dmc.Text("このまま移ると、直した内容は消えます。先に保存ボタンを押してください。", size="sm"),
                dmc.Group([
                    dmc.Button("戻って保存する", id="leave-cancel", variant="filled", size="sm"),
                    dmc.Button("保存せずに移る", id="leave-ok", variant="default", color="gray", size="sm"),
                ], justify="flex-end", mt="lg", gap="xs"),
            ]),
            html.Div(id="progress"),
            alert(f"テーブルの準備に失敗しました:{STARTUP_ERROR}", "danger") if STARTUP_ERROR else None,
            dmc.Tabs(dmc.TabsList([dmc.TabsTab(label, value=key) for key, label in STEPS]),
                     id="tabs", value="prep", variant="default", mb="lg"),
            # 操作の処理中・画面の読み込み中は、画面を薄くして「処理中です」を出す(二度押しも防ぐ)
            dcc.Loading(
                html.Div([html.Div(id="notice"), html.Div(id="content")]),
                target_components={"notice": "children", "content": "children"},
                delay_show=300,
                overlay_style={"visibility": "visible", "opacity": 0.45, "filter": "blur(1px)",
                               "pointerEvents": "none"},
                custom_spinner=dmc.Paper(dmc.Group([dmc.Loader(size="sm", color="accent"),
                                                    dmc.Text("処理中です…", size="sm", fw=500)], gap="sm"),
                                         withBorder=True, radius="md", px="lg", py="sm", shadow="sm"),
                parent_style={"minHeight": 200},
            ),
        ], size="xl", py="lg")),
    ], header={"height": 60}, padding=0))


app.layout = layout


@app.callback(
    Output("content", "children"), Output("status", "children"), Output("progress", "children"),
    Output("dirty", "data"),
    Input("shown-tab", "data"), Input("year", "value"), Input("month", "value"), Input("refresh", "data"),
)
def render(tab, year, month, _):
    try:
        bundle = repo.load(month_key(year, month))
        body = {"prep": prep_tab, "plan": plan_tab, "msg": msg_tab,
                "master": master_tab, "settings": settings_tab}[tab](bundle)
    except Exception as exc:
        log.exception("画面の表示に失敗しました")
        return alert(f"データを読み込めませんでした:{exc}", "danger"), "", None, False
    color = {"preparing": "gray", "draft": "orange", "confirmed": "teal", "contacted": "blue"}
    badge = dmc.Badge(STATUS_LABEL[bundle.status], color=color[bundle.status], variant="light", radius="sm")
    return body, badge, progress_bar(bundle), False


# ================================================================ 進み具合と、案を作る前の確認

FLOW = [("準備", "開催枠・対応可能日・使えない教室"), ("割り当て案", "案を作って確認する"),
        ("確定", "違反がなければ確定する"), ("連絡", "連絡文を作って送る")]


def readiness(bundle) -> list[dict]:
    """割り当て案を作る前に、そろっているか確かめる項目。must=True がそろわないと案を作れない。"""
    c = bundle.ctx
    active = [t for t in c.teachers.values() if t.active]
    no_genre = [x for x in c.courses.values() if not x.genre_id]
    offered = {t for (t, s) in c.availability if s in c.slots}
    return [
        {"label": "講師・体験授業・教室のマスター", "ok": bool(active and c.courses and c.rooms), "must": True,
         "todo": "「マスター」で調整共有シートを取り込む", "tab": "master"},
        {"label": f"開催枠({len(c.slots)}枠)", "ok": bool(c.slots), "must": True,
         "todo": "「① 準備」で開催日の午前・午後を選んで保存する", "tab": "prep"},
        {"label": f"講師の対応可能日({len(offered)}人・{len(c.availability)}件)", "ok": bool(c.availability),
         "must": True, "todo": "「① 準備」でフォームの回答を取り込む", "tab": "prep"},
        {"label": "体験授業の系統" + (f"(未設定 {len(no_genre)}件)" if no_genre else ""), "ok": not no_genre,
         "must": False, "todo": "「マスター」の体験授業で系統を選ぶ(未設定の授業は R5 で見ません)", "tab": "master"},
        {"label": f"使えない教室・日時({len(c.room_blocks)}件)", "ok": bool(c.room_blocks), "must": False,
         "todo": "教務側の行事がないか確かめ、あれば「① 準備」で登録する", "tab": "prep"},
    ]


def next_action(bundle) -> tuple[int, str]:
    """(いまの段階, 次にやること)。段階は FLOW の番号。4 はすべて完了。"""
    missing = [r for r in readiness(bundle) if r["must"] and not r["ok"]]
    if missing:
        return 0, missing[0]["todo"]
    if bundle.status == "contacted":
        return 4, "この月の調整は完了しています"
    if bundle.status == "confirmed":
        return 3, "「③ 連絡文」で下書きを作り、講師とHP業者に送る。送ったら「連絡済」にする"
    if not bundle.assignments:
        return 1, "「② 割り当て案」で「割り当て案を作る」を押す"
    must = manager.review(bundle.assignments, bundle.ctx).must
    if must:
        return 2, f"必ず守るルールの違反が{len(must)}件あります。「② 割り当て案」で直してから確定する"
    return 2, "「② 割り当て案」で内容を確認し、「この内容で確定する」を押す"


def progress_bar(bundle):
    step, todo = next_action(bundle)
    stepper = dmc.Stepper(
        [dmc.StepperStep(label=name, description=desc) for name, desc in FLOW],
        active=step, size="sm", iconSize=28, color="accent", allowNextStepsSelect=False,
    )
    return dmc.Paper([
        stepper,
        dmc.Group([dmc.Badge("次にやること", variant="light", radius="sm"), dmc.Text(todo, size="sm")],
                  gap="sm", mt="md"),
    ], withBorder=True, radius="md", p="md", mb="md")


def readiness_card(bundle):
    rows = []
    for r in readiness(bundle):
        if r["ok"]:
            icon, color = "✓", "teal"
        else:
            icon, color = ("!" if r["must"] else "・"), ("orange" if r["must"] else "gray")
        rows.append(dmc.Group([
            dmc.ThemeIcon(icon, color=color, variant="light", radius="xl", size="sm"),
            dmc.Text(r["label"], size="sm", fw=500, w=260),
            dmc.Text("" if r["ok"] else r["todo"] + ("" if r["must"] else "(なくても案は作れます)"),
                     size="sm", c="dimmed", style={"flex": 1}),
        ], gap="sm", wrap="nowrap"))
    return section("案を作る前の確認", dmc.Stack(rows, gap=8),
                   hint="オレンジの項目がそろうと「割り当て案を作る」を押せます。")


# ================================================================ 保存していない変更

def _mark_dirty(*_):
    # 画面を作り直したときは、表示した部品がまとめて(または値なしで)届くので数えない。
    # 先生が1か所を直したときは、その1つだけが届く
    trig = ctx.triggered or []
    if len(trig) != 1 or trig[0]["prop_id"] == "." or trig[0]["value"] is None:
        raise PreventUpdate
    return True


# 先生が直せる入力が変わったら「保存していない変更あり」にする(画面を作り直したときは変わらない)
for _input in [
    Input({"type": "slot", "key": ALL}, "checked"), Input({"type": "event", "day": ALL}, "value"),
    Input({"type": "av", "t": ALL, "s": ALL}, "checked"),
    # 表は、先生が編集・行の削除をしたときだけ変わる data_timestamp を見る
    Input("blocks-table", "data_timestamp"), Input("targets-table", "data_timestamp"),
    Input("plan-table", "data_timestamp"), Input({"type": "master", "name": ALL}, "data_timestamp"),
    Input({"type": "setting", "key": ALL}, "value"),
]:
    app.callback(Output("dirty", "data", allow_duplicate=True), _input, prevent_initial_call=True)(_mark_dirty)


@app.callback(Output("dirty-badge", "children"), Output("year", "disabled"), Output("month", "disabled"),
              Input("dirty", "data"))
def show_dirty(dirty):
    badge = dmc.Badge("保存していない変更があります", color="orange", variant="light", radius="sm") if dirty else None
    # 対象月を変えると直した内容が消えるので、保存するまで変えられないようにする
    return badge, bool(dirty), bool(dirty)


# ブラウザを閉じる・再読み込みするときも確認を出す
app.clientside_callback(
    """function(dirty) {
        window.onbeforeunload = dirty ? function(e) { e.preventDefault(); e.returnValue = ''; return ''; } : null;
        return '';
    }""",
    Output("unload-guard", "children"), Input("dirty", "data"),
)


@app.callback(Output("shown-tab", "data"), Output("tabs", "value"), Output("leave-modal", "opened"),
              Output("pending-tab", "data"),
              Input("tabs", "value"), State("shown-tab", "data"), State("dirty", "data"),
              prevent_initial_call=True)
def switch_tab(tab, shown, dirty):
    """保存していない変更があるときは、タブを移る前に確かめる。"""
    if tab == shown:
        raise PreventUpdate
    if dirty:
        return dash.no_update, shown, True, tab
    return tab, dash.no_update, False, None


@app.callback(Output("shown-tab", "data", allow_duplicate=True), Output("tabs", "value", allow_duplicate=True),
              Output("leave-modal", "opened", allow_duplicate=True), Output("dirty", "data", allow_duplicate=True),
              Input("leave-ok", "n_clicks"), Input("leave-cancel", "n_clicks"), State("pending-tab", "data"),
              prevent_initial_call=True)
def answer_leave(_ok, _cancel, pending):
    if ctx.triggered_id == "leave-ok" and pending:
        return pending, pending, False, False
    return dash.no_update, dash.no_update, False, dash.no_update


@app.callback(Output("notice", "children"), Input("shown-tab", "data"), Input("year", "value"),
              Input("month", "value"), prevent_initial_call=True)
def clear_notice(*_):
    return None


# ================================================================ ① 準備

def prep_tab(bundle):
    c = bundle.ctx
    if not c.teachers or not c.rooms or not c.courses:
        return alert("先に「マスター」で調整共有シート(体験授業一覧・教室)を取り込んでください。"
                     "動きを試すだけなら、マスターの「デモデータを入れる」が使えます。", "info")
    return html.Div([slot_card(bundle), availability_card(bundle), block_card(bundle), target_card(bundle)])


def slot_card(bundle):
    c = bundle.ctx
    year, month = int(bundle.month[:4]), int(bundle.month[5:7])
    picked = set(c.slots)
    head = dmc.TableThead(dmc.TableTr([dmc.TableTh(w, style={"textAlign": "center"}) for w in WEEKDAYS]))
    rows = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        cells = []
        for d in week:
            if d.month != month:
                cells.append(dmc.TableTd(style={"background": "#fafafa"}))
                continue
            weekend = d.weekday() >= 5
            cells.append(dmc.TableTd(dmc.Stack([
                dmc.Text(str(d.day), fw=600, size="sm", c=ACCENT if weekend else "dark"),
                dmc.Checkbox(id={"type": "slot", "key": slot_id_of(d, "AM")}, label="午前", size="xs",
                             checked=slot_id_of(d, "AM") in picked),
                dmc.Checkbox(id={"type": "slot", "key": slot_id_of(d, "PM")}, label="午後", size="xs",
                             checked=slot_id_of(d, "PM") in picked),
                dmc.TextInput(id={"type": "event", "day": d.isoformat()}, value=c.event_name(d),
                              placeholder="イベント名", size="xs"),
            ], gap=4), style={"verticalAlign": "top"}))
        rows.append(dmc.TableTr(cells))
    table = dmc.Table([head, dmc.TableTbody(rows)], withTableBorder=True, withColumnBorders=True,
                      style={"tableLayout": "fixed"}, mb="sm")
    return section(
        "開催日と時間枠",
        table,
        dmc.Group([
            button("土日をまとめて選ぶ", "slots-weekend"),
            button("すべて外す", "slots-clear"),
            button("開催枠を保存", "slots-save", primary=True),
        ], gap="xs"),
        step="1",
        hint="開催する日の午前・午後にチェックを入れ、早見表のイベント名(例:ゲームお仕事体験W)を書きます。"
             "授業は 10:20〜11:50/13:30〜15:00。3・7・8月は平日も選べます。",
    )


@app.callback(Output({"type": "slot", "key": ALL}, "checked"), Output("dirty", "data", allow_duplicate=True),
              Input("slots-weekend", "n_clicks"), Input("slots-clear", "n_clicks"),
              State({"type": "slot", "key": ALL}, "id"), prevent_initial_call=True)
def pick_slots(_w, _c, ids):
    if not ctx.triggered or not ctx.triggered[0]["value"]:
        raise PreventUpdate
    if ctx.triggered_id == "slots-clear":
        return [False] * len(ids), True
    return [date.fromisoformat(i["key"][:10]).weekday() >= 5 for i in ids], True


@app.callback(*NOTICE, Input("slots-save", "n_clicks"),
              State({"type": "slot", "key": ALL}, "checked"), State({"type": "slot", "key": ALL}, "id"),
              State({"type": "event", "day": ALL}, "value"), State({"type": "event", "day": ALL}, "id"),
              *MONTH_STATE, prevent_initial_call=True)
@action
def save_slots(_, values, ids, names, name_ids, year, month):
    mk = month_key(year, month)
    picks = [(date.fromisoformat(i["key"][:10]), i["key"][11:]) for v, i in zip(values, ids) if v]
    days = {d for d, _ in picks}
    events = {date.fromisoformat(i["day"]): (n or "") for n, i in zip(names, name_ids)
              if date.fromisoformat(i["day"]) in days}
    repo.save_slots(mk, picks)
    repo.save_events(mk, events)
    return done(f"開催枠を{len(picks)}件保存しました。")


def availability_card(bundle):
    c = bundle.ctx
    slots = c.sorted_slots()
    if not slots:
        body = [dmc.Text("開催枠を保存すると、ここに講師×枠の表が出ます。", size="sm", c="dimmed")]
    else:
        head = dmc.TableThead(dmc.TableTr([dmc.TableTh("講師")] + [
            dmc.TableTh(short_slot(s), style={"textAlign": "center", "fontWeight": 500, "fontSize": 12})
            for s in slots]))
        rows = []
        for t in sorted(c.teachers.values(), key=lambda t: t.name):
            if not t.active:
                continue
            rows.append(dmc.TableTr([dmc.TableTd(t.name, style={"whiteSpace": "nowrap"})] + [
                dmc.TableTd(dmc.Center(dmc.Checkbox(id={"type": "av", "t": t.teacher_id, "s": s.slot_id},
                                                    checked=(t.teacher_id, s.slot_id) in c.availability,
                                                    size="xs")))
                for s in slots
            ]))
        body = [dmc.ScrollArea(dmc.Table([head, dmc.TableTbody(rows)], withTableBorder=True,
                                         highlightOnHover=True, striped=False), type="auto", mb="sm")]
    return section(
        "講師の対応可能日",
        dmc.Group([
            button("フォームの回答を取り込む", "av-form"),
            upload_button("av-upload", "回答の Excel / CSV を選ぶ", accept=".xlsx,.csv"),
        ], gap="xs", mb="sm"),
        *body,
        button("対応可能日を保存", "av-save", primary=True) if slots else None,
        step="2",
        hint=f"Googleフォームの回答(開催日ごとに午前午後/午前のみ/午後のみ/対応不可)を取り込みます。"
             f"講師はメールアドレス、なければ名前で照合します。現在 {len(c.availability)} 件。",
    )


@app.callback(*NOTICE, Input("av-form", "n_clicks"), *MONTH_STATE, prevent_initial_call=True)
@action
def import_form(_, year, month):
    n, notes = manager.import_availability_from_form(repo, month_key(year, month))
    return done(f"フォームの回答から{n}件を取り込みました。", "success" if n else "warning", notes)


@app.callback(*NOTICE, Input("av-upload", "contents"), State("av-upload", "filename"), *MONTH_STATE,
              prevent_initial_call=True)
@action
def import_upload(contents, filename, year, month):
    mk = month_key(year, month)
    if str(filename or "").lower().endswith(".xlsx"):
        n, notes = manager.import_availability_from_book(repo, mk, sheet_import.read_workbook(upload_bytes(contents)))
    else:
        records = manager.parse_availability_csv(decode_upload(contents))
        n, notes = manager.import_availability(repo, mk, records)
    return done(f"{n}件を取り込みました。", "success" if n else "warning", notes)


@app.callback(*NOTICE, Input("av-save", "n_clicks"),
              State({"type": "av", "t": ALL, "s": ALL}, "checked"), State({"type": "av", "t": ALL, "s": ALL}, "id"),
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
    return section(
        "使えない教室・日時",
        table,
        dmc.Group([button("行を追加", "blocks-add"), button("使えない教室を保存", "blocks-save", primary=True)],
                  gap="xs", mt="sm"),
        step="3",
        hint="教務側の行事(オリエンテーション、保護者会、esports大会など)で使えない教室を登録します。"
             "登録漏れがあると、割り当て案が行事と重なります。",
    )


@app.callback(Output("blocks-table", "data"), Output("dirty", "data", allow_duplicate=True),
              Input("blocks-add", "n_clicks"), State("blocks-table", "data"), prevent_initial_call=True)
def add_block_row(n, data):
    if not n:
        raise PreventUpdate
    return (data or []) + [{"day": None, "period": "終日", "room_id": None, "reason": ""}], True


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
    data = []
    for w in weeks:
        names = sorted({c.event_name(s.day) for s in c.slots.values() if s.week_key == w} - {""})
        data.append({"week_key": w, "week": week_label(c, w), "event": "・".join(names),
                     **{g.genre_id: c.week_targets.get((w, g.genre_id)) for g in genres}})
    table = dash_table.DataTable(
        id="targets-table", data=data, editable=True,
        columns=[{"name": "週", "id": "week", "editable": False}, {"name": "イベント", "id": "event", "editable": False}]
        + [{"name": f"{g.name}系", "id": g.genre_id, "type": "numeric"} for g in genres],
        **TABLE_STYLE,
    )
    return section(
        "週ごとの打ち出し方の目安(任意)",
        table,
        button("目安を保存", "targets-save", primary=True, mt="sm"),
        step="4",
        hint="イベント名を手がかりに、「この週はIT系を多めに」のような方針を系統ごとのコマ数で入れます(R6)。空欄なら考慮しません。",
    )


@app.callback(*NOTICE, Input("targets-save", "n_clicks"), State("targets-table", "data"), *MONTH_STATE,
              prevent_initial_call=True)
@action
def save_targets(_, data, year, month):
    targets = {}
    for row in data or []:
        for key, value in row.items():
            if key in ("week", "week_key", "event") or value in (None, ""):
                continue
            targets[(row["week_key"], key)] = int(value)
    repo.save_week_targets(month_key(year, month), targets)
    return done("週ごとの目安を保存しました。")


# ================================================================ ② 割り当て案

FINDING_STYLE = {"must": ("orange", "filled"), "should": ("yellow", "light"), "warning": ("gray", "light")}


def findings_block(title, findings, kind):
    if not findings:
        return None
    color, variant = FINDING_STYLE[kind]
    return dmc.Paper([
        dmc.Text(title, fw=600, size="sm", mb="xs"),
        dmc.Stack([dmc.Group([dmc.Badge(f.rule, color=color, variant=variant, radius="sm", w=56),
                              dmc.Text(f.message, size="sm", style={"flex": 1})], gap="sm", wrap="nowrap",
                             align="flex-start")
                   for f in findings], gap=6),
    ], withBorder=True, radius="md", p="md", mb="sm")


def plan_tab(bundle):
    c = bundle.ctx
    locked = bundle.status in ("confirmed", "contacted")
    not_ready = any(r["must"] and not r["ok"] for r in readiness(bundle))
    if not_ready or not bundle.assignments:
        check = readiness_card(bundle)
        if not_ready:
            return check
    else:
        check = None
    rv = manager.review(bundle.assignments, c)
    flagged = {aid for f in rv.must for aid in f.assignment_ids}

    buttons = dmc.Group([
        button("割り当て案を作り直す" if bundle.assignments else "割り当て案を作る", "plan-create", primary=True,
               disabled=locked),
        button("AIで理由を読みやすくする", "plan-polish",
               disabled=locked or not bundle.assignments or not llm_client.available),
        button("確定を解除", "plan-unconfirm") if locked
        else dmc.Button("この内容で確定する", id="plan-confirm", color="teal", size="sm", radius="md",
                        disabled=bool(rv.must) or not bundle.assignments),
        button("CSVで保存", "plan-export", disabled=not bundle.assignments),
        None if locked else upload_button("plan-upload", "過去の割り当てCSVを読み込む"),
    ], gap="xs", mb="md")

    data = [
        {"assignment_id": a.assignment_id, "slot_id": a.slot_id, "course_id": a.course_id,
         "teacher_id": a.teacher_id, "room_id": a.room_id, "reason": a.reason,
         "event": c.event_name(c.slots[a.slot_id].day) if a.slot_id in c.slots else ""}
        for a in bundle.assignments
    ]
    table = dash_table.DataTable(
        id="plan-table", data=data, editable=not locked, row_deletable=not locked,
        columns=[
            {"name": "日付・時間帯", "id": "slot_id", "presentation": "dropdown"},
            {"name": "イベント", "id": "event", "editable": False},
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
                                       for t in sorted(c.teachers.values(), key=lambda t: t.name) if t.active]},
            "room_id": {"options": [{"label": n, "value": r} for r, n in sorted(c.rooms.items(), key=lambda x: x[1])]},
        },
        style_data_conditional=[
            {"if": {"row_index": i}, "backgroundColor": "#fff4e6", "borderLeft": "3px solid #fd7e14"}
            for i, row in enumerate(data) if row["assignment_id"] in flagged
        ] + [{"if": {"column_id": ["reason", "event"]}, "color": MUTED}],
        style_cell_conditional=[
            {"if": {"column_id": "slot_id"}, "minWidth": "150px"},
            {"if": {"column_id": "event"}, "minWidth": "130px"},
            {"if": {"column_id": "course_id"}, "minWidth": "250px"},
            {"if": {"column_id": "teacher_id"}, "minWidth": "120px"},
            {"if": {"column_id": "room_id"}, "minWidth": "90px"},
            {"if": {"column_id": "reason"}, "minWidth": "340px"},
        ],
        **TABLE_STYLE,
    )
    edit_row = None if locked else dmc.Group([
        button("行を追加", "plan-add"),
        dmc.TextInput(id="plan-change-reason", placeholder="直した理由(任意。次回以降のルール見直しに使います)",
                      size="sm", style={"flex": 1}),
        button("修正を保存して再チェック", "plan-save", primary=True),
    ], gap="xs", mt="sm")

    if not bundle.assignments:  # 案がないうちは、件数や未充足の一覧は出さない
        return html.Div([check, buttons])
    return html.Div([
        check,
        buttons,
        dmc.Group([
            stat("割り当て", len(bundle.assignments)),
            stat("必ず守るルールの違反", len(rv.must), warn=True),
            stat("なるべく守るルールの未充足", len(rv.should), warn=True),
            stat("確認してほしいこと", len(rv.warnings), warn=True),
            stat("先生の手修正(累計)", repo.count_changes(bundle.month)),
        ], gap="sm", mb="md"),
        alert("確定済みです。直す場合は「確定を解除」を押してください。", "success") if locked else None,
        findings_block("必ず守るルール(Must)の違反 — 解消するまで確定できません", rv.must, "must"),
        findings_block("なるべく守るルール(Should)で満たせていないもの", rv.should, "should"),
        findings_block("確認してほしいこと", rv.warnings, "warning"),
        dmc.Paper([dmc.Text("組めなかった理由", fw=600, size="sm", mb="xs"),
                   dmc.List([dmc.ListItem(dmc.Text(n, size="sm")) for n in rv.unplaced], size="sm")],
                  withBorder=True, radius="md", p="md", mb="sm") if rv.unplaced and bundle.assignments else None,
        section("割り当て表", table, edit_row,
                hint="セルを選ぶと直せます。左にオレンジの線がある行は、必ず守るルールに違反しています。")
        if bundle.assignments else alert("まだ案がありません。「割り当て案を作る」を押してください。", "info"),
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


@app.callback(Output("plan-table", "data"), Output("dirty", "data", allow_duplicate=True),
              Input("plan-add", "n_clicks"), State("plan-table", "data"), prevent_initial_call=True)
def add_plan_row(n, data):
    if not n:
        raise PreventUpdate
    return (data or []) + [{"assignment_id": "", "slot_id": None, "course_id": None, "teacher_id": None,
                            "room_id": None, "event": "", "reason": "(新しい行。保存すると理由が付きます)"}], True


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
    controls = dmc.Group([
        dmc.TextInput(id="msg-sender", placeholder="差出人の名前(例:鈴木)", size="sm", w=220),
        dmc.Checkbox(id="msg-ai", label="あいさつ文をAIで整える", checked=False, disabled=not llm_client.available),
        button("連絡文の下書きを作る", "msg-create", primary=True),
        button("送信が済んだら「連絡済」にする", "msg-sent",
               disabled=not bundle.messages or bundle.status == "contacted"),
    ], gap="sm", mb="md")
    if not bundle.messages:
        return html.Div([controls, alert("まだ下書きがありません。", "info")])

    problems = messages.verify_messages(bundle.messages, bundle.assignments, c)
    check = alert("確定した割り当てと連絡文の照合:食い違い 0件", "success") if not problems else \
        alert("確定した割り当てと連絡文に食い違いがあります。下書きを作り直してください。", "danger", problems)
    order = {"notice": 0, "thanks": 1, "hp": 2}
    items = []
    for i, m in enumerate(sorted(bundle.messages, key=lambda m: (order[m.kind], c.teacher_name(m.teacher_id or "")))):
        to = "HP業者" if m.kind == "hp" else c.teacher_name(m.teacher_id)
        items.append(dmc.AccordionItem([
            dmc.AccordionControl(dmc.Group([
                dmc.Text(f"{to} 宛て", fw=500, size="sm"),
                dmc.Badge(messages.KIND_LABEL[m.kind], variant="light", color="gray", radius="sm"),
            ], gap="sm")),
            dmc.AccordionPanel([
                dmc.Group([dmc.Text("件名", size="xs", c="dimmed"), dmc.Text(m.subject, id=f"msg-subject-{i}", size="sm"),
                           dcc.Clipboard(target_id=f"msg-subject-{i}", title="件名をコピー",
                                         style={"color": ACCENT, "cursor": "pointer"})], gap="xs", mb="xs"),
                dmc.Paper([
                    dcc.Clipboard(target_id=f"msg-body-{i}", title="本文をコピー",
                                  style={"float": "right", "color": ACCENT, "fontSize": 18, "cursor": "pointer"}),
                    html.Pre(m.body, id=f"msg-body-{i}",
                             style={"whiteSpace": "pre-wrap", "fontFamily": "inherit", "fontSize": 14, "margin": 0}),
                ], withBorder=True, radius="md", p="md", bg="#fafafa"),
            ]),
        ], value=str(i)))
    return html.Div([
        controls, check,
        dmc.Text("右上のアイコンで本文をコピーし、ふだんのメールに貼り付けて送ってください。送信は先生が行います。",
                 size="sm", c="dimmed", mb="xs"),
        dmc.Accordion(items, multiple=True, variant="separated", radius="md"),
    ])


@app.callback(*NOTICE, Input("msg-create", "n_clicks"), State("msg-sender", "value"), State("msg-ai", "checked"),
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


# ================================================================ マスター

# マスターの多くは調整共有シートから取り込みます(先生はスプレッドシートを直す)。
# シートに列がない「系統」「代わりの系統」「代わりの教室(R11)」と、体験授業の系統だけをこの画面で直します。
# (テーブル, タブ名, 説明, [(列, 見出し, 種類, この画面で直せるか)])  種類: text / bool / date / int / ref:参照先 / priority
MASTERS = [
    ("course", "体験授業", "体験授業一覧から取り込んだ授業。系統だけここで選びます(R5 の判定に使います)。",
     [("name", "体験授業名", "text", False), ("genre_id", "系統", "ref:genre", True),
      ("capacity", "定員目安", "int", False), ("note", "備考・準備事項", "text", False)]),
    ("genre", "系統", "「2週続けて空けない」(R5)の対象にする系統は「はい」にします。",
     [("name", "系統名", "text", True), ("r5_target", "R5の対象", "bool", True)]),
    ("genre_substitute", "代わりの系統", "左の系統を入れられない週に、代わりにできる近い系統(例:CG → VFX)。",
     [("genre_id", "系統", "ref:genre", True), ("substitute_genre_id", "代わりにできる系統", "ref:genre", True)]),
    ("room_alternative", "代わりの教室", "工事中などに、元の教室の授業を入れてよい教室と期限(R11。ルール一覧 No.4)。"
     "日付は 2027-02-28 の形で入れます。",
     [("room_id", "元の教室", "ref:room", True), ("alt_room_id", "代わりの教室", "ref:room", True),
      ("valid_until", "期限", "date", True), ("note", "メモ", "text", True)]),
    ("teacher", "講師", "体験授業一覧の講師名から取り込みます。メールアドレスはフォーム回答の照合で覚えます。",
     [("name", "名前", "text", False), ("active", "在籍中", "bool", False), ("email", "メールアドレス", "text", False)]),
    ("teacher_course", "講師×授業", "どの講師がどの体験授業を担当できるか(1つの授業を複数の講師が担当します)。",
     [("teacher_id", "講師", "ref:teacher", False), ("course_id", "体験授業", "ref:course", False)]),
    ("course_room", "授業×教室", "体験授業一覧の「優先して入れる教室」「予備教室」。優先→予備の順に使います。",
     [("course_id", "体験授業", "ref:course", False), ("room_id", "教室", "ref:room", False),
      ("priority", "区分", "priority", False)]),
    ("course_software", "授業×ソフト", "授業に必要なソフト。ここにあるソフトが入った教室にだけ割り当てます(R2)。",
     [("course_id", "体験授業", "ref:course", False), ("software_id", "ソフト", "ref:software", False)]),
    ("room", "教室", "教室シートから取り込みます。「使用しない」の教室は割り当てに使いません。",
     [("name", "教室名", "text", False), ("usable", "使う", "bool", False)]),
    ("room_software", "教室×ソフト", "教室シートの「必要な教室環境/ソフト」。確認日が古いと警告が出ます(R10)。",
     [("room_id", "教室", "ref:room", False), ("software_id", "ソフト", "ref:software", False),
      ("confirmed_on", "確認日", "date", False)]),
    ("software", "ソフト", "教室シートに出てくるソフト。", [("name", "ソフト名", "text", False)]),
]
MASTER_SPEC = {name: cols for name, _, _, cols in MASTERS}
ID_COLUMN = {"teacher": "teacher_id", "course": "course_id", "genre": "genre_id",
             "room": "room_id", "software": "software_id"}
YES_NO = [{"label": "はい", "value": "はい"}, {"label": "いいえ", "value": "いいえ"}]


def master_tab(bundle):
    names = {t: {r[ID_COLUMN[t]]: r["name"] for r in repo.read(t)} for t in ID_COLUMN}
    tabs, panels = [], []
    for table, label, hint, cols in MASTERS:
        rows = repo.read(table)
        editable = any(e for *_, e in cols)
        data, columns, dropdown = [], [], {}
        for field, title, kind, can_edit in cols:
            col = {"name": title, "id": field, "editable": can_edit}
            if kind == "bool" and can_edit:
                col["presentation"] = "dropdown"
                dropdown[field] = {"options": YES_NO}
            elif kind.startswith("ref:") and can_edit:
                col["presentation"] = "dropdown"
                dropdown[field] = {"options": [{"label": n, "value": i} for i, n in
                                               sorted(names[kind[4:]].items(), key=lambda x: x[1])]}
            columns.append(col)
        for r in rows:
            row = dict(r)
            for field, _, kind, can_edit in cols:
                value = r.get(field)
                if kind == "bool":
                    row[field] = "いいえ" if value is False else "はい"
                elif kind == "date":
                    row[field] = value.isoformat() if value else ""
                elif kind == "priority":
                    row[field] = PRIORITY_LABEL.get(value, value)
                elif kind.startswith("ref:") and not can_edit:
                    row[field] = names[kind[4:]].get(value, value)
            data.append(row)
        first_field, _, first_kind, _ = cols[0]
        data.sort(key=lambda r: str(names.get(first_kind[4:], {}).get(r[first_field], r[first_field]) or ""))
        tabs.append(dmc.TabsTab(f"{label}({len(data)})", value=table,
                                rightSection=None if not editable else dmc.Badge("編集", size="xs", variant="light")))
        panels.append(dmc.TabsPanel(html.Div([
            dmc.Text(hint, size="sm", c="dimmed", my="sm"),
            dash_table.DataTable(id={"type": "master", "name": table}, data=data, columns=columns,
                                 dropdown=dropdown, editable=editable,
                                 row_deletable=editable and table != "course",
                                 page_size=50, **TABLE_STYLE),
            dmc.Group([
                None if table == "course" else button("行を追加", {"type": "master-add", "name": table}),
                button(f"「{label}」を保存", {"type": "master-save", "name": table}, primary=True),
            ], gap="xs", mt="sm") if editable else None,
        ]), value=table))
    demo = dcc.ConfirmDialogProvider(
        dmc.Button("デモデータを入れる", variant="subtle", color="gray", size="sm"), id="demo-seed",
        message="マスターをすべて架空のデモデータに置き換え、表示中の月に開催枠と対応可能日を入れます。よろしいですか?")
    return html.Div([
        section(
            "調整共有シートから取り込む",
            dmc.Group([
                button("シートの内容を取り込む", "master-sheet", primary=True),
                upload_button("master-upload", "調整共有シートの Excel を選ぶ", accept=".xlsx"),
                demo,
            ], gap="xs"),
            hint="「体験授業一覧」(授業・講師・必要なソフト・優先教室・予備教室・定員)と「教室」(教室ごとのソフト)を読み込みます。"
                 "講師・授業・教室を直すときは、スプレッドシートを直してから取り込み直してください。系統の設定は残ります。",
        ),
        # 保存して画面を読み直しても、開いていたタブに戻る
        dmc.Tabs([dmc.TabsList(tabs)] + panels, id="master-tabs", value="course", persistence=True,
                 persistence_type="session"),
    ])


@app.callback(Output({"type": "master", "name": MATCH}, "data"),
              Input({"type": "master-add", "name": MATCH}, "n_clicks"),
              State({"type": "master", "name": MATCH}, "data"), State({"type": "master", "name": MATCH}, "id"),
              prevent_initial_call=True)
def add_master_row(n, data, table_id):
    if not n:
        raise PreventUpdate
    blank = {field: ("はい" if kind == "bool" else None if kind.startswith("ref:") else "")
             for field, _, kind, _ in MASTER_SPEC[table_id["name"]]}
    return (data or []) + [blank]


def parse_master_rows(table: str, data: list[dict]) -> list[dict]:
    """画面で直せる表(系統・代わりの系統・代わりの教室・体験授業の系統)を、テーブルの行に戻す。"""
    spec = MASTER_SPEC[table]
    id_col = ID_COLUMN.get(table)
    if table == "course":  # 系統だけを差し替える。ほかの列は取り込んだ値のまま
        genre = {r["course_id"]: r.get("genre_id") or None for r in data or []}
        return [{**r, "genre_id": genre.get(r["course_id"], r["genre_id"])} for r in repo.read("course")]
    rows, seen = [], set()
    for n, raw in enumerate(data or [], start=1):
        row = {}
        for field, title, kind, _ in spec:
            value = raw.get(field)
            if kind == "bool":
                row[field] = value != "いいえ"
            elif kind == "date":
                text = str(value or "").strip().replace("/", "-")
                try:
                    row[field] = date.fromisoformat(text) if text else None
                except ValueError:
                    raise ValueError(f"{n}行目の「{title}」は 2027-02-28 の形で入れてください(入力:{value})")
            else:
                row[field] = str(value).strip() if value not in (None, "") else None
        required = [f for f, _, k, _ in spec if k.startswith("ref:") or (k == "text" and f == "name")]
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


def imported(counts, notes):
    text = "取り込みました:" + "、".join(
        f"{lbl}{counts[t]}件" for t, lbl in (("course", "体験授業"), ("teacher", "講師"), ("room", "教室"),
                                            ("course_room", "授業×教室")) if t in counts)
    return done(text, "warning" if notes else "success", notes)


@app.callback(*NOTICE, Input("master-sheet", "n_clicks"), prevent_initial_call=True)
@action
def import_sheet(_):
    return imported(*manager.import_masters_from_sheet_raw(repo))


@app.callback(*NOTICE, Input("master-upload", "contents"), prevent_initial_call=True)
@action
def import_sheet_upload(contents):
    return imported(*manager.import_masters_from_book(repo, sheet_import.read_workbook(upload_bytes(contents))))


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
        dmc.Group([
            dmc.Stack([dmc.Text(label, size="sm"), dmc.Text(hint, size="xs", c="dimmed")] if hint
                      else [dmc.Text(label, size="sm")], gap=0, style={"flex": 1}),
            dmc.NumberInput(id={"type": "setting", "key": key}, min=0, step=1, value=getattr(s, key),
                            size="sm", w=120, allowDecimal=False),
        ], justify="space-between", py=6, style={"borderBottom": f"1px solid {BORDER}"})
        for key, label, hint in SETTING_LABELS
    ]
    return section(
        "割り当ての作り方の設定",
        dmc.Stack(rows, gap=0, maw=640),
        button("設定を保存", "settings-save", primary=True, mt="md"),
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
        if value in (None, "") or int(value) < 0:
            return alert("空欄または負の値があります。0以上の整数を入れてください。", "danger"), dash.no_update
        setattr(s, i["key"], int(value))
    if s.per_slot_max < max(1, s.per_slot_target):
        return alert("「上限」は「目安」以上、かつ1以上にしてください。", "danger"), dash.no_update
    repo.save_settings(s)
    return done("設定を保存しました。次に案を作るときから反映されます。")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("DATABRICKS_APP_PORT", "8000")), debug=False)
