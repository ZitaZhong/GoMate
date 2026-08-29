// 周末去哪儿 回填助手 · service worker（DD-14）
// 极简：仅监听安装事件。所有回填逻辑在 popup（用户主动触发），后台不做任何自动抓取/购票。
chrome.runtime.onInstalled.addListener(() => {
  console.log("周末去哪儿 回填助手已安装（仅本地解析 + 人工确认回填）");
});
