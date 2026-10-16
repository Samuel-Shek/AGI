function inspectForms() {
  const inputs = Array.from(document.querySelectorAll("input, textarea, select"));
  const text = [
    document.title || "",
    ...Array.from(document.querySelectorAll("form, label, button")).slice(0, 80).map((node) => node.innerText || node.getAttribute("aria-label") || "")
  ].join(" ").toLowerCase();
  const hasPassword = inputs.some((input) => String(input.type || "").toLowerCase() === "password");
  const hasPayment = PAM_POLICY.hasPaymentText(text) || inputs.some((input) => /cc-|card|cvv|cvc|expiry/i.test(`${input.name || ""} ${input.id || ""} ${input.autocomplete || ""}`));
  const hasLogin = hasPassword || PAM_POLICY.hasLoginText(text);
  const riskTags = [];
  if (hasPassword) riskTags.push("password_field");
  if (hasPayment) riskTags.push("payment_form");
  if (hasLogin) riskTags.push("login_form");
  riskTags.push(...PAM_POLICY.riskTagsFromPageText(text));
  chrome.runtime.sendMessage({
    type: "pam-form-signal",
    has_password_field: hasPassword,
    has_payment_form: hasPayment,
    has_login_form: hasLogin,
    risk_tags: riskTags
  });
}

inspectForms();
setTimeout(inspectForms, 1500);
