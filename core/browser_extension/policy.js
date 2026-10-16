const PAM_POLICY = (() => {
  const adultDomainTerms = [
    "pornhub", "xvideos", "xhamster", "xnxx", "redtube", "youporn", "spankbang",
    "tube8", "brazzers", "onlyfans", "fansly", "chaturbate", "stripchat",
    "bongacams", "nhentai", "hanime", "rule34", "f95zone", "dlsite", "missav",
    "javbus", "javdb", "porn", "porno", "xxx", "nsfw", "hentai", "eroge", "r18"
  ];
  const adultTextTerms = [
    ...adultDomainTerms,
    "pornography", "adult content", "adult video", "adult site", "adult game",
    "adult entertainment", "explicit sexual content", "sexual content",
    "adult-only sexual content", "adult only sexual content", "成人内容", "色情",
    "成人视频", "成人影片", "成人电影", "成人图片", "黄色游戏", "成人游戏", "黄油",
    "裸露", "性内容", "18禁", "アダルト", "エロゲ"
  ];
  const financialUrlPattern = /(checkout|payment|billing|paypal|stripe|bank|wallet|account|login|signin|verify)/;
  const paymentTextPattern = /(card number|cvv|cvc|expiry|payment|checkout|billing|pay now|银行卡|支付|付款|收银台)/;
  const loginTextPattern = /(login|sign in|password|verify|verification|otp|2fa|验证码|登录|登入|密码)/;

  function escapeRegExp(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  const adultDomainPattern = new RegExp(`(^|[./-])(${adultDomainTerms.map(escapeRegExp).join("|")})([./-]|$)`);
  const adultTextPattern = new RegExp(adultTextTerms.map(escapeRegExp).join("|"));

  function riskTagsFromUrl(url) {
    const tags = [];
    const lower = String(url || "").toLowerCase();
    if (lower.startsWith("file://")) tags.push("local_file");
    if (financialUrlPattern.test(lower)) tags.push("financial_domain");
    if (adultDomainPattern.test(lower)) tags.push("adult_content");
    return tags;
  }

  function riskTagsFromPageText(text) {
    const lower = String(text || "").toLowerCase();
    return adultTextPattern.test(lower) ? ["adult_content"] : [];
  }

  function hasPaymentText(text) {
    return paymentTextPattern.test(String(text || "").toLowerCase());
  }

  function hasLoginText(text) {
    return loginTextPattern.test(String(text || "").toLowerCase());
  }

  return {
    riskTagsFromUrl,
    riskTagsFromPageText,
    hasPaymentText,
    hasLoginText
  };
})();

globalThis.PAM_POLICY = PAM_POLICY;
