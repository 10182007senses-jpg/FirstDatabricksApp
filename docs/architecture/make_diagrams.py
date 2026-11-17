"""アーキテクチャ図(C4 のコンテナ図・コンポーネント図)を SVG で書き出す。

図の中身は、コードの実際の構成(import と呼び出し関係)に合わせてある。
コードの構成を変えたら、ここも直して作り直すこと。

    python docs/architecture/make_diagrams.py
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).parent
FONT = '"Hiragino Sans","Yu Gothic UI","Meiryo","Noto Sans CJK JP","Noto Sans JP",sans-serif'
PERSON, CONTAINER, COMPONENT, EXTERNAL, LINE = "#08427B", "#438DD5", "#85BBF0", "#999999", "#707070"


class Svg:
    def __init__(self, width: int, height: int):
        self.w, self.h = width, height
        self.items: list[str] = []
        self.labels: list[str] = []  # 線より手前に描く

    def text(self, x, y, s, size=13, color="#000", weight="normal", anchor="middle", into=None):
        (self.items if into is None else into).append(
            f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" '
            f'text-anchor="{anchor}">{escape(s)}</text>')

    def _block(self, x, y, w, title, kind, lines, color):
        cx = x + w / 2
        self.text(cx, y, title, 17, color, "bold")
        self.text(cx, y + 17, f"[{kind}]", 11, color)
        for i, line in enumerate(lines):
            self.text(cx, y + 44 + i * 17, line, 12, color)

    def box(self, x, y, w, h, title, kind, lines, fill, dashed=False):
        fg = "#fff" if fill in (PERSON, CONTAINER, EXTERNAL) else "#0b2a4a"
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        self.items.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="#2b6cb0" '
            f'stroke-opacity=".5"{dash}/>')
        self._block(x, y + h / 2 - (len(lines) * 17 + 34) / 2 + 14, w, title, kind, lines, fg)

    def person(self, x, y, w, h, title, lines):
        cx = x + w / 2
        self.items.append(f'<circle cx="{cx}" cy="{y + 34}" r="34" fill="{PERSON}"/>')
        self.items.append(f'<rect x="{x}" y="{y + 56}" width="{w}" height="{h - 56}" rx="34" fill="{PERSON}"/>')
        self._block(x, y + 84, w, title, "Person", lines, "#fff")

    def cylinder(self, x, y, w, h, title, kind, lines):
        ry = 14
        self.items.append(
            f'<path d="M{x},{y + ry} a{w / 2},{ry} 0 0 1 {w},0 v{h - 2 * ry} a{w / 2},{ry} 0 0 1 -{w},0 z" '
            f'fill="{CONTAINER}"/>'
            f'<path d="M{x},{y + ry} a{w / 2},{ry} 0 0 0 {w},0" fill="none" stroke="#fff" stroke-opacity=".5"/>')
        self._block(x, y + h / 2 - (len(lines) * 17 + 34) / 2 + 24, w, title, kind, lines, "#fff")

    def boundary(self, x, y, w, h, title, kind):
        self.items.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="none" stroke="#666" stroke-dasharray="8 6"/>')
        self.text(x + 10, y + h - 26, title, 14, "#222", "bold", "start")
        self.text(x + 10, y + h - 10, f"[{kind}]", 11, "#444", anchor="start")

    def arrow(self, points, label=None, tech=None, at=None, both=False, anchor="middle"):
        d = "M" + " L".join(f"{px},{py}" for px, py in points)
        start = ' marker-start="url(#tail)"' if both else ""
        self.items.append(
            f'<path d="{d}" fill="none" stroke="{LINE}" stroke-width="1.4" stroke-dasharray="5 4" '
            f'marker-end="url(#head)"{start}/>')
        if not label:
            return
        lines = label if isinstance(label, list) else [label]
        if at is None:
            (x1, y1), (x2, y2) = points[len(points) // 2 - 1], points[len(points) // 2]
            at = ((x1 + x2) / 2, (y1 + y2) / 2)
        x, y = at
        rows = len(lines) + (1 if tech else 0)
        width = max(len(s) for s in lines + ([tech] if tech else [])) * 12.5 + 8
        left = x - width / 2 if anchor == "middle" else x - 4 if anchor == "start" else x - width + 4
        top = y - rows * 8
        self.labels.append(
            f'<rect x="{left}" y="{top - 4}" width="{width}" height="{rows * 16 + 6}" fill="#fff" fill-opacity=".9"/>')
        for i, s in enumerate(lines):
            self.text(x, top + 10 + i * 16, s, 12, "#222", "bold", anchor, self.labels)
        if tech:
            self.text(x, top + 10 + len(lines) * 16, f"[{tech}]", 10.5, "#444", anchor=anchor, into=self.labels)

    def title(self, s, note=None):
        self.text(4, self.h - (30 if note else 12), s, 16, "#000", "bold", "start")
        if note:
            self.text(4, self.h - 10, note, 11.5, "#444", anchor="start")

    def save(self, name: str) -> None:
        marker = ('<marker id="{0}" viewBox="0 0 10 10" refX="{1}" refY="5" markerWidth="8" markerHeight="8" '
                  'orient="{2}"><path d="M0,0 L10,5 L0,10 z" fill="' + LINE + '"/></marker>')
        body = "\n".join(self.items + self.labels)
        (OUT / name).write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
            f'height="{self.h}" font-family=\'{FONT}\'>\n'
            f'<defs>{marker.format("head", 9, "auto")}{marker.format("tail", 9, "auto-start-reverse")}</defs>\n'
            f'<rect width="{self.w}" height="{self.h}" fill="#fff"/>\n{body}\n</svg>\n', encoding="utf-8")


# ================================================================ コンテナ図

def container_diagram() -> None:
    s = Svg(1100, 1200)
    s.person(40, 10, 240, 170, "広報の先生", ["割り当て案を確認・確定し、", "連絡文を送る(4名)"])
    s.box(410, 40, 250, 120, "先生のメール", "外部システム", ["連絡文を送る"], EXTERNAL)
    s.person(810, 10, 240, 170, "講師", ["対応可能日をフォームで回答する"])
    s.box(410, 260, 250, 110, "HP業者", "外部の会社", ["体験授業をHPに公開する"], EXTERNAL)
    s.box(805, 260, 250, 110, "Googleフォーム", "外部システム", ["講師の対応可能日を集める"], EXTERNAL)

    s.boundary(20, 440, 1060, 540, "体験授業調整AI", "Software System")
    s.box(40, 500, 340, 170, "Webアプリ", "Container: Databricks Apps / Dash(Python)",
          ["開催枠・対応可能日・使えない教室の登録、", "割り当て案の作成とルール判定、",
           "理由・連絡文の下書き、マスター登録"], CONTAINER)
    s.box(740, 500, 310, 150, "回答の取り込み", "Container: Google Apps Script",
          ["フォーム送信時に、回答を", "availability_raw テーブルへ書き込む"], CONTAINER)
    s.cylinder(740, 760, 310, 170, "テーブル群", "Container: Delta Lake / Unity Catalog",
               ["マスター9種・開催枠・月次データ6種、", "フォーム回答・週の目安・設定",
                "(スキーマ workspace.taiken_chosei)"])
    s.box(40, 1040, 340, 110, "LLM", "外部システム: Foundation Model API",
          ["理由文とあいさつ文を整える(なくても動く)"], EXTERNAL)

    s.arrow([(110, 180), (110, 500)], "操作する", "HTTPS", at=(110, 300))
    s.arrow([(230, 500), (230, 180)], ["割り当て案・理由・", "連絡文の下書きを表示"], "画面・コピーボタン", at=(262, 380))
    s.arrow([(280, 100), (410, 100)], "コピペして送信", "手動", at=(345, 78))
    s.arrow([(660, 100), (810, 100)], "送る", "メール", at=(735, 78))
    s.arrow([(535, 160), (535, 260)], "送る", "メール")
    s.arrow([(930, 180), (930, 260)], "回答する", "Webフォーム")
    s.arrow([(915, 370), (895, 500)], "送信時に起動", "トリガー")
    s.arrow([(895, 650), (895, 760)], "回答を書き込む", "HTTPS: SQL Statement Execution API")
    s.arrow([(380, 585), (560, 585), (560, 850), (740, 850)], "読み書き(起動時にテーブルを作成)",
            "SQLウェアハウス: databricks-sql-connector", at=(560, 720))
    s.arrow([(210, 670), (210, 1040)], ["文章の生成を依頼・返却", "(講師名は記号に置き換える)"],
            "HTTPS: serving-endpoints/…/invocations", at=(210, 800), both=True)
    s.title("コンテナ図:体験授業調整システム(コードから作成・エージェントなし)",
            "補足:対応可能日はフォーム経由のほか、Webアプリの CSV 取り込み・画面入力でも登録できる。"
            "ローカル確認時はテーブル群の代わりに SQLite を使う。")
    s.save("container.svg")


# ================================================================ コンポーネント図

def component_diagram() -> None:
    s = Svg(1100, 1420)
    s.person(430, 10, 250, 170, "広報の先生", ["割り当て案を確認・確定する(4名)"])
    s.boundary(20, 230, 1060, 920, "Webアプリ", "Container: Databricks Apps / Dash")

    s.box(40, 270, 440, 120, "月次調整の画面", "Component: Dash — app.py",
          ["① 準備・② 割り当て案・③ 連絡文", "表示と操作だけ(計算はしない)"], COMPONENT)
    s.box(640, 270, 380, 120, "登録の画面", "Component: Dash — app.py",
          ["マスター登録(F2)・設定(Should の重み)"], COMPONENT)
    s.box(40, 470, 440, 120, "割り当て管理", "Component: Python — scheduler/manager.py",
          ["手順をまとめる:取り込む・案を作る・修正する・", "確定する・連絡文を作る(F1/F4/F7/F8/F14)"], COMPONENT)
    s.box(640, 470, 260, 120, "データアクセス", "Component: data_access.py + schema.py",
          ["テーブルの読み書きを一か所に集める", "(Databricks SQL / ローカルは SQLite)"], COMPONENT)

    s.box(40, 690, 190, 120, "割り当て作成", "Component: solver.py",
          ["案を作る(F4)", "代わりの系統(F9)", "組めない理由(F5)"], COMPONENT)
    s.box(250, 690, 190, 120, "理由説明", "Component: reasons.py",
          ["各行の理由を", "事実から組み立てる(F6)"], COMPONENT)
    s.box(460, 690, 190, 120, "連絡文作成", "Component: messages.py",
          ["下書きを作る(F8)", "割り当てとの照合"], COMPONENT)
    s.box(780, 690, 220, 120, "デモデータ", "Component: sample_data.py",
          ["架空のマスターと", "月次データを入れる"], COMPONENT, dashed=True)

    s.box(40, 900, 330, 120, "ルール判定", "Component: Python — scheduler/rules.py",
          ["R1〜R10 を判定する(Must / Should / 警告)", "DB にも画面にも触らない"], COMPONENT)
    s.box(400, 900, 290, 120, "LLM呼び出し", "Component: Python — scheduler/llm.py",
          ["講師名を伏せて依頼し、", "返ってきた文の名前を戻す"], COMPONENT)

    s.box(350, 1200, 300, 110, "LLM", "外部システム: Foundation Model API",
          ["理由文・あいさつ文を整える"], EXTERNAL)
    s.cylinder(700, 1195, 320, 140, "テーブル群", "Container: Delta Lake / Unity Catalog",
               ["マスター・対応可能日・使えない教室・", "割り当て・連絡文・修正の記録"])

    s.arrow([(470, 180), (300, 270)], "確認・修正・確定・コピー", "HTTPS", at=(300, 205))
    s.arrow([(640, 180), (800, 270)], "登録する", "HTTPS", at=(790, 205))
    s.arrow([(200, 390), (200, 470)], "manager の関数を呼ぶ", at=(200, 432))
    s.arrow([(480, 340), (585, 340), (585, 500), (640, 500)], ["開催枠・対応可能日・", "使えない教室を保存"],
            at=(585, 420))
    s.arrow([(780, 390), (780, 470)], "保存", at=(780, 432))
    s.arrow([(960, 390), (960, 690)], "デモデータを入れる", at=(952, 615), anchor="end")
    s.arrow([(810, 690), (810, 590)], "投入", at=(810, 660))
    s.arrow([(480, 550), (640, 550)], "読み書き", at=(560, 536))

    s.arrow([(135, 590), (135, 690)], "呼び出す", at=(135, 652))
    s.arrow([(345, 590), (345, 690)])
    s.arrow([(440, 590), (440, 640), (555, 640), (555, 690)])
    s.arrow([(40, 530), (28, 530), (28, 960), (40, 960)], "再チェック", at=(34, 850), anchor="start")

    s.arrow([(135, 810), (135, 900)], "判定を使う", at=(135, 858))
    s.arrow([(310, 810), (310, 900)])
    s.arrow([(420, 810), (420, 900)], "文章を依頼", at=(488, 858))
    s.arrow([(555, 810), (555, 900)])

    s.arrow([(500, 1020), (500, 1200)], "伏せた文で生成を依頼・返却", "HTTPS", at=(500, 1100), both=True)
    s.arrow([(720, 590), (720, 1205)], "読み書き", "SQL", at=(720, 1100), both=True)

    s.title("コンポーネント図:Webアプリ(コードから作成・v1・エージェントなし)",
            "補足:線は実際の import と呼び出しの向き。月次調整の画面は、連絡文の照合結果の表示のために"
            "連絡文作成も直接呼ぶ(図では省略)。")
    s.save("component.svg")


if __name__ == "__main__":
    container_diagram()
    component_diagram()
    print("書き出しました:", ", ".join(p.name for p in sorted(OUT.glob("*.svg"))))
