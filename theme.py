"""画面の見た目(デザイン案A:shadcn/ui 風の静かな業務ツール)。

- 白地に濃いグレーの文字。色を使うのは落ち着いた青(ACCENT)1色だけ
- 枠は細い罫線、角は少し丸く、影は付けない
- 違反は赤ではなくオレンジで知らせる(先生を責める色にしない)
"""
ACCENT = "#2f5fd0"
TEXT = "#18181b"
MUTED = "#71717a"
BORDER = "#e4e4e7"
FONT = '"Hiragino Sans", "Hiragino Kaku Gothic ProN", "Noto Sans JP", "Yu Gothic UI", "Meiryo", system-ui, sans-serif'

THEME = {
    "primaryColor": "accent",
    "primaryShade": 6,
    "colors": {
        # Mantine は1色につき10段階の色を使う。6番目が基本の色
        "accent": ["#eef3fc", "#dce5f8", "#b7caf0", "#8fade8", "#6c93e0", "#5682db", "#2f5fd0",
                   "#2650b4", "#1f4293", "#173372"],
    },
    "fontFamily": FONT,
    "headings": {"fontFamily": FONT, "fontWeight": "600"},
    "defaultRadius": "md",
    "black": TEXT,
    "components": {
        "Button": {"defaultProps": {"fw": 500}},
        "Paper": {"defaultProps": {"shadow": None}},
        "Badge": {"defaultProps": {"fw": 500}},
        "Tabs": {"styles": {"tab": {"fontWeight": 500}}},
    },
}

# dash_table.DataTable を Mantine の表に見た目をそろえる
TABLE_STYLE = dict(
    style_cell={"fontFamily": "inherit", "fontSize": "14px", "padding": "10px 12px", "textAlign": "left",
                "whiteSpace": "normal", "height": "auto", "minWidth": "120px", "color": TEXT,
                "border": "none", "borderBottom": f"1px solid {BORDER}"},
    style_header={"fontWeight": "500", "backgroundColor": "#fafafa", "color": MUTED, "fontSize": "13px",
                  "borderBottom": f"1px solid {BORDER}"},
    style_table={"overflowX": "auto", "border": f"1px solid {BORDER}", "borderRadius": "8px"},
    css=[{"selector": ".Select-menu-outer", "rule": "display: block !important"}],
)
