// 周末去哪儿 回填助手 · content script（DD-14）
// 只读 window.getSelection()，本地正则解析草稿；不做任何登录/购票/后台抓取。
// 活动事件：popup 通过 chrome.scripting.executeScript 调用 getSelectionDraft()。

function localParseDraft(text) {
  const t = (text || "").trim();
  if (!t) return null;
  const draft = { raw: t, kind: null, extracted: {} };
  // 车次 G/D/C/Z/T/K + 数字
  const train = t.match(/([GDCZTK]\d{1,4})/);
  // 航班号 两字母+3~4位数字
  const flight = t.match(/\b([A-Z0-9]{2})\s?(\d{3,4})\b/);
  // 日期 2026-08-09 / 8月9日
  const date = t.match(/(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日)/);
  // 时间 08:00
  const times = t.match(/\d{1,2}:\d{2}/g) || [];
  if (train) {
    draft.kind = "train";
    draft.extracted.train_no = train[1];
    if (date) draft.extracted.date = date[1];
    if (times[0]) draft.extracted.dep_time = times[0];
    if (times[1]) draft.extracted.arr_time = times[1];
  } else if (flight) {
    draft.kind = "flight";
    draft.extracted.flight_no = flight[1] + flight[2];
    if (date) draft.extracted.date = date[1];
    if (times[0]) draft.extracted.dep_time = times[0];
    if (times[1]) draft.extracted.arr_time = times[1];
  } else {
    draft.kind = "manual"; // 本地不足 → 后端小模型补抽（text 模式）或人工手填
  }
  return draft;
}

window.getSelectionDraft = function () {
  const sel = window.getSelection ? window.getSelection().toString() : "";
  return localParseDraft(sel);
};
