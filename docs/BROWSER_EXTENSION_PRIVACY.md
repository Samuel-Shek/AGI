# Browser Extension Privacy

The browser extension is optional. It exists to provide low-sensitivity browser
context to the local core so the Privacy Firewall can make better decisions than
OCR alone.

The extension does not make Personal AI Monitor a browser history collector. It
sends short-lived signals to a local native host, and the core stores only
sanitized fields.

## Permissions

| Permission | Why it is requested | What is stored |
| --- | --- | --- |
| `nativeMessaging` | Send the sanitized signal to the local Personal AI Monitor native host. | The native host writes a local JSON signal under the runtime data directory. |
| `tabs` | Read the active tab URL, tab id, window id, and incognito state. | Redacted URL, domain, scheme, tab/window ids, and private-window flag. |
| `webNavigation` | Notice page transitions and refresh browser context promptly. | No navigation history table is stored by the extension. |
| `<all_urls>` host permission | Allow the content script to detect login/payment/private-risk cues on pages the user visits. | Boolean risk flags and a short allow-listed set of risk tags. |
| Content script on `<all_urls>` | Inspect page title and visible form/label/button text for risk cues. | The text itself is not stored or sent as a durable activity field. |

## What The Extension Sends

The browser-side payload can include:

- observed timestamp
- browser name
- tab id and window id
- current URL
- private/incognito flag
- booleans for password, payment, and login form detection
- allow-listed risk tags such as `private_window`, `password_field`,
  `payment_form`, `login_form`, `financial_domain`, `secret_like`,
  `local_file`, and adult-content risk tags

The native host sanitizes that payload before storage. It keeps only a redacted
URL, safe domain, scheme, private/form flags, tab/window ids, risk tags, source,
confidence, and an expiry timestamp.

## What It Does Not Read Or Store

The extension does not read, store, or send:

- input values typed into forms
- passwords
- cookies
- bookmarks
- downloads
- clipboard contents
- full browser history
- full page body text
- screenshots
- raw OCR text
- chat transcripts

The content script checks form structure and visible labels/buttons for local
risk classification. It does not read `input.value`.

## Local-Only Native Host

Browser messages go to the local native host named
`app.personalai.monitor.browser_signal`. The host writes signals into the local
runtime data directory with user-only filesystem permissions. Signals expire
after 45 seconds and are pruned as new signals arrive.

The native host manifest allows only the fixed extension ID derived from the
public key in `core/browser_extension/manifest.json`, unless a developer
explicitly overrides it during local testing.

## Privacy Firewall Use

The core uses browser signals as context for the same local Privacy Firewall
that handles OCR and Accessibility text. Risk flags can cause a capture to be
blocked, partially redacted, or converted into a content-free gap event. They do
not bypass the firewall or create a raw browser log.

If the extension is not installed, the core falls back to lower-confidence local
browser attribution such as AppleScript where available.
