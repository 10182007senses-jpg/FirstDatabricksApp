/**
 * 回答の取り込み(コンテナ図の「Google Apps Script」)。
 * Google フォームの送信時に、講師の対応可能日を Databricks のテーブル availability_raw へ書き込みます。
 *
 * 【要確認】フォームの作りは次を前提にしています。違う場合は NAME_TITLE / parseChoice を直してください。
 *   - 名前を聞く質問のタイトルに「名前」が入っている
 *   - 対応可能日はチェックボックスで、選択肢が「12/6(日) 午前」「2026/12/06 午後」のような形
 *
 * 設定(スクリプト プロパティ。コードには書かない):
 *   DATABRICKS_HOST   例: https://dbc-xxxxxxxx-xxxx.cloud.databricks.com
 *   DATABRICKS_TOKEN  書き込み専用に発行したトークン
 *   WAREHOUSE_ID      SQL ウェアハウスの ID
 *   TABLE             例: workspace.taiken_chosei.availability_raw
 *   TARGET_YEAR       年の書かれていない選択肢(12/6 など)に補う年。例: 2026
 *
 * トリガー: エディタの「トリガー」から onFormSubmit を「フォーム送信時」に設定します。
 */
const NAME_TITLE = '名前';

function onFormSubmit(e) {
  const props = PropertiesService.getScriptProperties();
  let name = '';
  const rows = [];

  e.response.getItemResponses().forEach(function (item) {
    const title = item.getItem().getTitle();
    const answer = item.getResponse();
    if (title.indexOf(NAME_TITLE) !== -1) {
      name = String(answer).trim();
      return;
    }
    (Array.isArray(answer) ? answer : [answer]).forEach(function (choice) {
      const parsed = parseChoice(String(choice), props.getProperty('TARGET_YEAR'));
      if (parsed) rows.push(parsed);
    });
  });
  if (!name || rows.length === 0) return;

  const receivedAt = e.response.getTimestamp().toISOString();
  const values = rows.map(function (_, i) {
    return '(:received_at, :teacher_name, CAST(:day' + i + ' AS DATE), :period' + i + ')';
  });
  const parameters = [
    { name: 'received_at', value: receivedAt },
    { name: 'teacher_name', value: name },
  ];
  rows.forEach(function (r, i) {
    parameters.push({ name: 'day' + i, value: r.day });
    parameters.push({ name: 'period' + i, value: r.period });
  });

  const table = props.getProperty('TABLE');
  const res = UrlFetchApp.fetch(props.getProperty('DATABRICKS_HOST') + '/api/2.0/sql/statements', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + props.getProperty('DATABRICKS_TOKEN') },
    payload: JSON.stringify({
      warehouse_id: props.getProperty('WAREHOUSE_ID'),
      statement:
        'INSERT INTO ' + table + ' (received_at, teacher_name, day, period) VALUES ' + values.join(', '),
      parameters: parameters,
      wait_timeout: '30s',
    }),
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() >= 300) {
    // 失敗しても回答はフォーム(スプレッドシート)に残る。アプリの CSV 取り込みで代替できる
    console.error('Databricks への書き込みに失敗: ' + res.getContentText());
  }
}

/** 「12/6(日) 午前」「2026/12/06 午後」→ { day: '2026-12-06', period: '午前' } */
function parseChoice(text, defaultYear) {
  const period = text.indexOf('午前') !== -1 ? '午前' : text.indexOf('午後') !== -1 ? '午後' : '終日';
  const m = text.match(/(?:(\d{4})[\/\-年])?(\d{1,2})[\/\-月](\d{1,2})/);
  if (!m) return null;
  const year = m[1] || defaultYear;
  if (!year) return null;
  const pad = function (n) { return ('0' + n).slice(-2); };
  return { day: year + '-' + pad(m[2]) + '-' + pad(m[3]), period: period };
}
