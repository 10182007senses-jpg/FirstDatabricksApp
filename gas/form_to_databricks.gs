/**
 * 回答の取り込み(コンテナ図の「Google Apps Script」)。
 * Google フォームの送信時に、講師の対応可能日を Databricks のテーブル availability_raw へ書き込みます。
 *
 * フォームの作り(実際の「体験授業のご相談」フォームに合わせています):
 *   - 講師名の質問:タイトルに「講師名」が入っている(フルネームの自由記述)
 *   - 開催日ごとに1問:タイトルが「11/8(日) 【午前・午後】 講師朝礼…」、
 *     回答が「午前午後 対応可」「午前/午後 対応可」「午前のみ 対応可」「午後のみ 対応可」「対応不可」
 *   - メールアドレスを収集する設定(講師の照合に使う。名前の表記揺れに強い)
 *
 * 設定(スクリプト プロパティ。コードには書かない):
 *   DATABRICKS_HOST   例: https://dbc-xxxxxxxx-xxxx.cloud.databricks.com
 *   DATABRICKS_TOKEN  書き込み専用に発行したトークン
 *   WAREHOUSE_ID      SQL ウェアハウスの ID
 *   TABLE             例: workspace.trial_class_scheduler.availability_raw
 *
 * トリガー: エディタの「トリガー」から onFormSubmit を「フォーム送信時」に設定します。
 */
const NAME_TITLE = '講師名';

function onFormSubmit(e) {
  const props = PropertiesService.getScriptProperties();
  let name = '';
  const rows = [];

  const email = (e.response.getRespondentEmail() || '').toLowerCase();
  e.response.getItemResponses().forEach(function (item) {
    const title = item.getItem().getTitle();
    const answer = String(item.getResponse() || '');
    if (title.indexOf(NAME_TITLE) !== -1 || title.indexOf('名前') !== -1) {
      name = answer.trim();
      return;
    }
    const day = parseDay(title, e.response.getTimestamp());
    if (!day) return;
    parseAnswer(answer).forEach(function (period) {
      rows.push({ day: day, period: period });
    });
  });
  if (!name || rows.length === 0) return;

  const receivedAt = e.response.getTimestamp().toISOString();
  const values = rows.map(function (_, i) {
    return '(:received_at, :teacher_name, CAST(:day' + i + ' AS DATE), :period' + i + ', :teacher_email)';
  });
  const parameters = [
    { name: 'received_at', value: receivedAt },
    { name: 'teacher_name', value: name },
    { name: 'teacher_email', value: email },
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
        'INSERT INTO ' + table + ' (received_at, teacher_name, day, period, teacher_email) VALUES ' + values.join(', '),
      parameters: parameters,
      wait_timeout: '30s',
    }),
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() >= 300) {
    // 失敗しても回答はフォーム(スプレッドシート)に残る。アプリで回答の Excel を選んで取り込める
    console.error('Databricks への書き込みに失敗: ' + res.getContentText());
  }
}

/**
 * 質問のタイトル「11/8(日) 【午前・午後】 …」→ '2026-11-08'。日付がなければ null。
 * 年は回答した日から決める(12月に1月分を聞くときは翌年にする)。
 */
function parseDay(title, answeredAt) {
  const m = title.match(/(\d{1,2})\s*\/\s*(\d{1,2})/);
  if (!m) return null;
  let year = answeredAt.getFullYear();
  if (Number(m[1]) < answeredAt.getMonth() + 1 - 6) year += 1;
  const pad = function (n) { return ('0' + n).slice(-2); };
  return year + '-' + pad(m[1]) + '-' + pad(m[2]);
}

/** 「午前午後 対応可」→ ['午前', '午後']、「午前のみ 対応可」→ ['午前']、「対応不可」→ [] */
function parseAnswer(text) {
  const t = text.replace(/\s/g, '');
  if (!t || t.indexOf('不可') !== -1) return [];
  if (t.indexOf('午前のみ') !== -1) return ['午前'];
  if (t.indexOf('午後のみ') !== -1) return ['午後'];
  return ['午前', '午後'];
}
