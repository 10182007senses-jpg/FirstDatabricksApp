/**
 * 調整共有シートの取り込み(コンテナ図の「データの取り込み」)。
 * 「体験授業一覧」「教室」シートが変わったら、その行を Databricks の sheet_raw テーブルへ書き込みます。
 * アプリの「マスター」→「シートの内容を取り込む」で、マスターに反映されます。
 *
 * 個人情報を減らすため、送るのは2つのシートの値だけです(打ち合わせ調整などのシートは送りません)。
 *
 * 設定(スクリプト プロパティ。コードには書かない):
 *   DATABRICKS_HOST   例: https://dbc-xxxxxxxx-xxxx.cloud.databricks.com
 *   DATABRICKS_TOKEN  書き込み専用に発行したトークン
 *   WAREHOUSE_ID      SQL ウェアハウスの ID
 *   SHEET_TABLE       例: workspace.trial_class_scheduler.sheet_raw
 *
 * トリガー: エディタの「トリガー」から onSheetChange を「スプレッドシートから・変更時」に設定します。
 * 手で送るときは pushSheets を実行します。
 */
const SHEETS = ['体験授業一覧', '教室'];

function onSheetChange() {
  pushSheets();
}

function pushSheets() {
  const props = PropertiesService.getScriptProperties();
  const table = props.getProperty('SHEET_TABLE');
  const book = SpreadsheetApp.getActiveSpreadsheet();
  const now = new Date().toISOString();

  SHEETS.forEach(function (name) {
    const sheet = book.getSheetByName(name);
    if (!sheet) return;
    const values = sheet.getDataRange().getDisplayValues();
    // そのシートの古い行を消してから、今の行を入れる
    run(props, 'DELETE FROM ' + table + ' WHERE sheet_name = :name', [{ name: 'name', value: name }]);
    for (let start = 0; start < values.length; start += 50) {
      const part = values.slice(start, start + 50);
      const params = [{ name: 'name', value: name }, { name: 'at', value: now }];
      const rows = part.map(function (cells, i) {
        params.push({ name: 'no' + i, value: String(start + i + 1), type: 'INT' });
        params.push({ name: 'cells' + i, value: JSON.stringify(cells) });
        return '(:name, :no' + i + ', :cells' + i + ', :at)';
      });
      run(props, 'INSERT INTO ' + table + ' (sheet_name, row_no, cells_json, received_at) VALUES ' +
        rows.join(', '), params);
    }
  });
}

function run(props, statement, parameters) {
  const res = UrlFetchApp.fetch(props.getProperty('DATABRICKS_HOST') + '/api/2.0/sql/statements', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + props.getProperty('DATABRICKS_TOKEN') },
    payload: JSON.stringify({
      warehouse_id: props.getProperty('WAREHOUSE_ID'),
      statement: statement,
      parameters: parameters,
      wait_timeout: '30s',
    }),
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() >= 300) {
    // 失敗してもシートはそのまま。アプリで Excel を選んで取り込める
    throw new Error('Databricks への書き込みに失敗: ' + res.getContentText());
  }
}
