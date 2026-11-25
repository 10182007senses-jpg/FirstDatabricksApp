# TODO: ブラッシュアップ

確認コマンド: `.venv/bin/python -m pytest -q`
精度確認: `.venv/bin/python tools/evaluate_plan.py 早見表.xlsx 調整共有.xlsx 回答.xlsx 2026-11` (+ `2026-10 --given-teachers`)

---

## Phase 1: データ壊れにくさ

### T1 開催枠変更で古い割り当て整理 [S]
開催枠保存時、外れた枠の割り当て・対応可能日・教室ブロックを削除。案ありなら状態 draft へ。消えた件数を通知。
- [x] 枠から外した日の割り当てが残らない
- [x] 手動で直した行(manual)を消す時は件数を通知文に含める
- [x] 案の表で枠のない行は「(開催枠なし)」表示、元ID生表示なし
- 確認: pytest に「枠を外す→割り当て消える」テスト追加
- 依存: なし
- ファイル: scheduler/data_access.py, app.py, tests/test_sheet_import.py

### T2 設定「初期値に戻す」 [S]
設定画面にボタン。保存値と初期値が違う項目に印。
- [x] ボタンで app_setting 削除→初期値表示
- [x] 初期値と違う項目が見分けられる
- 確認: pytest(設定保存→戻す→初期値)、ローカル画面で操作
- 依存: なし
- ファイル: app.py, scheduler/data_access.py, tests/test_app_smoke.py

### T3 データリセット [M]
設定画面に「この月のデータを消す」「全部消す」。確認モーダル(消える内容・取消不可を明記)。
- [x] 月: 開催枠・対応可能日・割り当て・連絡文・イベントのみ消える。マスター残る
- [x] 全部: 全テーブル空。デモデータ投入可能
- [x] モーダルで「消す」押すまで何も消えない
- 確認: pytest(月リセット後マスター残存)、ローカル画面
- 依存: T1(削除処理共有)
- ファイル: scheduler/data_access.py, scheduler/manager.py, app.py, tests

### Checkpoint 1
- [ ] pytest 全通過
- [ ] 11/29 事例(案作成→枠外す)で違反出ない
- [ ] ユーザー確認

## Phase 2: 案の精度

### T4 系統の自動推定と一括設定 [M]
授業名・必要ソフトから系統候補(ゲーム/IT/CG/イラスト/esports/ロボット…)を推定。マスター画面で候補確認→一括反映。R5 が効くようにする。
- [ ] 未設定の授業に候補表示、ボタンで一括反映(上書きなし)
- [ ] 実データで系統未設定 0 件にできる
- [ ] evaluate_plan 3指標、10月・11月とも下がらない
- 依存: なし
- ファイル: scheduler/sheet_import.py(推定), app.py, tests

### T5 講師・授業の別名 [M]
teacher.aliases / course.aliases 列。会社名・旧授業名で照合可能に。早見表取り込み・フォーム取り込み・評価で使用。
- [ ] 「デジタルノイズ」表記で担当者に照合できる(別名登録時)
- [ ] 早見表照合不可の行数が減る(評価スクリプト表示)
- [ ] マスター画面で別名編集可
- 依存: 未決事項の回答
- ファイル: scheduler/schema.py, scheduler/manager.py, scheduler/sheet_import.py, app.py, tests

### T6 講師×日の選び方 [M]
先生回答の決め手を Should ルール化(例: 前月入った講師後回し、イベント分野一致優先)。1ルールずつ評価。
- [ ] 講師×日×時間帯 再現率 37% 超(11月)
- [ ] 10月(--given-teachers なし版が作れれば)でも悪化なし
- [ ] 理由文に新ルール表示
- 依存: T4、未決事項の回答
- ファイル: scheduler/solver.py, scheduler/reasons.py, scheduler/models.py, tests

### Checkpoint 2
- [ ] 3指標 現状値以上、README 表更新
- [ ] ユーザー確認

## Phase 3: 使いやすさ

### T7 マスター行追加も未保存扱い [XS]
- [ ] 行追加・削除で「未保存」バッジ点灯
- 確認: ローカル画面
- ファイル: app.py

### T8 取り込み結果の詳細 [S]
対応可能日・マスター・早見表取り込み後、読み飛ばした名前・日時を折りたたみ一覧で。
- [ ] 何件入って何件・誰/どの日が飛んだか画面で分かる
- [ ] 「18件取り込み、変化なし」状況で原因が画面から読める
- ファイル: app.py, scheduler/manager.py

### T9 案を早見表と同じ並びで確認 [M]
案タブに「早見表で見る」切替。日ごと・授業名/講師 教室・※午前のみ表示(quickref と同ロジック)。
- [ ] 表と早見表ビュー切替
- [ ] 違反のある授業は色付き
- 依存: なし
- ファイル: app.py, scheduler/quickref.py, assets/style.css, tests

## Phase 4: 見た目

### T10 早見表出力の仕上げ [S]
- [ ] 会社講師に「先生」付けない(T5 の属性で判定)
- [ ] 枠のない日は出さない、空欄列も罫線そろう
- [ ] 共有用早見表と並べて見た目差なし(ユーザー確認)
- 依存: T5
- ファイル: scheduler/quickref.py, tests

### T11 アクセシビリティ・見やすさ点検 [S]
accessibility-engineering スキルで WCAG 2.2 AA 点検。コントラスト・ラベル・キーボード操作。
- [ ] 指摘の重大項目 0
- 確認: ローカル画面 + axe 相当チェック
- ファイル: assets/style.css, theme.py, app.py

### Checkpoint 完了
- [ ] 全受入条件達成、pytest 全通過
- [ ] push → Databricks 再デプロイ → ユーザー画面確認
