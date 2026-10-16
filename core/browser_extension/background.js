importScripts("policy.js");

const HOST = "app.personalai.monitor.browser_signal";
const pendingSignals = new Map();

function browserName() {
  const agent = navigator.userAgent || "";
  if (agent.includes("Dia/") || agent.includes("Dia ")) return "Dia";
  if (agent.includes("Edg/")) return "Chrome";
  return "Chrome";
}

async function sendSignal(tabId, tab, formSignal = {}) {
  if (!tab || !tab.url) return;
  const payload = {
    observed_at: new Date().toISOString(),
    browser: browserName(),
    tab_id: String(tabId),
    window_id: String(tab.windowId || ""),
    url: tab.url,
    private_window: Boolean(chrome.extension.inIncognitoContext || tab.incognito),
    has_password_field: Boolean(formSignal.has_password_field),
    has_payment_form: Boolean(formSignal.has_payment_form),
    has_login_form: Boolean(formSignal.has_login_form),
    risk_tags: [...new Set([...(PAM_POLICY.riskTagsFromUrl(tab.url)), ...((formSignal.risk_tags) || [])])],
    source: "chromium_extension_native_host",
    confidence: 0.96
  };
  try {
    chrome.runtime.sendNativeMessage(HOST, payload, () => {
      void chrome.runtime.lastError;
    });
  } catch (_error) {
    // Native host may not be installed yet. The core keeps AppleScript/AX fallback.
  }
}

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await chrome.tabs.get(tabId);
  sendSignal(tabId, tab, pendingSignals.get(tabId) || {});
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url || changeInfo.status === "complete") {
    sendSignal(tabId, tab, pendingSignals.get(tabId) || {});
  }
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (!sender.tab || message.type !== "pam-form-signal") return;
  pendingSignals.set(sender.tab.id, message);
  sendSignal(sender.tab.id, sender.tab, message);
});
