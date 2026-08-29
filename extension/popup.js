// 周末去哪儿 回填助手 · popup（DD-14）
// 读取当前页选区 → 本地解析草稿 → 用户逐字段确认 → POST /plans/{id}/bookings/import。
// 绝不自动登录/购票；只在用户点击"确认并提交"时提交一次。

async function config() {
  const { bff = "http://127.0.0.1:8000", token = "" } = await chrome.storage.local.get(["bff", "token"]);
  return { bff, token };
}

async function loadDraftFromActiveTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) return null;
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, func: () => window.getSelectionDraft && window.getSelectionDraft(),
    });
    return result;
  } catch (e) {
    return null; // activeTab 未授权或非匹配页 → 静默，用户手填
  }
}

function fillForm(draft) {
  if (!draft) return;
  if (draft.kind && draft.kind !== "manual") document.getElementById("kind").value = draft.kind;
  const ex = draft.extracted || {};
  const f1 = document.getElementById("f1");
  f1.value = ex.train_no || ex.flight_no || ex.name || "";
  if (ex.date) document.getElementById("f2").value = ex.date;
}

document.addEventListener("DOMContentLoaded", async () => {
  const draft = await loadDraftFromActiveTab();
  fillForm(draft);
});

document.getElementById("submit").onclick = async () => {
  const status = document.getElementById("status");
  const planId = document.getElementById("planId").value.trim();
  const kind = document.getElementById("kind").value;
  const { bff, token } = await config();
  if (!planId) { status.textContent = "请填写 Plan ID"; return; }
  const body = {
    kind,
    input_kind: "manual",
    extracted: {
      [kind === "hotel" ? "name" : kind === "flight" ? "flight_no" : "train_no"]: document.getElementById("f1").value,
      date: document.getElementById("f2").value || undefined,
    },
    token,
  };
  status.textContent = "提交中…";
  try {
    const r = await fetch(`${bff}/plans/${planId}/bookings/import`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const d = await r.json();
    status.textContent = d.ready_for_resume ? "✅ 已确认，可继续规划" : "已记录草稿，请补全关键字段";
  } catch (e) {
    status.textContent = "提交失败：" + e.message;
  }
};
