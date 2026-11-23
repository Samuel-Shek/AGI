import AppKit
import SwiftUI
import WebKit

/// 事项工作台：在 PSI 主窗口内承载原工作台页面（127.0.0.1:8767）。
/// 服务只在用户进入此页时启动；PSI 主窗口关闭或 PSI 退出时停止。
@MainActor
final class IssueWorkbenchService: NSObject, WKNavigationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    static let shared = IssueWorkbenchService()
    static let localURL = URL(string: "http://127.0.0.1:8767/")!
    private static let python = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"

    private(set) lazy var webView: WKWebView = {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.userContentController.add(self, name: "psiDraftPending")
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = self
        view.allowsMagnification = true
        return view
    }()
    private var server: Process?
    private var stoppingServer: Process?
    private var reopenAfterStop = false
    private var instance = UUID().uuidString
    private var probeDeadline = 0.0
    private var probeInterval = 0.05
    private var probeExhausted = false
    private(set) var hasWorkbenchPage = false
    private(set) var hasPendingDraftOrHandoff = false
    private var closeTicket: UUID?
    private var closeCallbacks: [(Bool) -> Void] = []
    private var closingWindow = false
    var showBlockedWorkbench: (() -> Void)?

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        let origin = message.frameInfo.securityOrigin
        guard hasWorkbenchPage, message.webView === webView, message.frameInfo.isMainFrame,
              origin.protocol == "http", origin.host == "127.0.0.1", origin.port == 8767,
              let pending = message.body as? Bool else { return }
        hasPendingDraftOrHandoff = pending
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard hasWorkbenchPage else { return true }
        guard !closingWindow else { return false }
        closingWindow = true
        prepareForClose { [weak self, weak sender] safe in
            guard let self else { return }
            self.closingWindow = false
            if safe { sender?.close() } else { self.showBlockedWorkbench?() }
        }
        return false
    }

    func prepareForClose(_ completion: @escaping (Bool) -> Void) {
        guard hasWorkbenchPage else { completion(true); return }
        closeCallbacks.append(completion)
        guard closeTicket == nil else { return }
        let ticket = UUID()
        closeTicket = ticket
        // A stalled web process cannot keep an application-quit request pending indefinitely.
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            self?.finishClose(ticket, safe: false)
        }
        let script = "if (typeof workbench === 'undefined') return !hadPending && !document.querySelector('#case-input'); return await workbench.prepareToClose();"
        webView.callAsyncJavaScript(script, arguments: ["hadPending": hasPendingDraftOrHandoff], in: nil, in: .page) { [weak self] result in
            self?.finishClose(ticket, safe: (try? result.get() as? Bool) == true)
        }
    }

    private func finishClose(_ ticket: UUID, safe: Bool) {
        guard ticket == closeTicket else { return }
        closeTicket = nil
        let callbacks = closeCallbacks
        closeCallbacks.removeAll()
        if !safe {
            resumeEditing()
            if server?.isRunning != true { activate() }
        }
        for callback in callbacks { callback(safe) }
    }

    func terminationReply(automatic: Bool, nativeDecision: @escaping () -> NSApplication.TerminateReply,
                          reply: @escaping (Bool) -> Void) -> NSApplication.TerminateReply {
        guard !automatic && hasWorkbenchPage else { return nativeDecision() }
        prepareForClose { safe in
            let mayQuit = safe && nativeDecision() == .terminateNow
            if !mayQuit { self.resumeEditing() }
            reply(mayQuit)
        }
        return .terminateLater
    }

    func resumeEditing() {
        guard hasWorkbenchPage else { return }
        webView.evaluateJavaScript("if (typeof workbench !== 'undefined') workbench.resumeEditing()")
    }

    private static func argument(_ key: String) -> String? {
        guard let i = CommandLine.arguments.firstIndex(of: key), i + 1 < CommandLine.arguments.count else { return nil }
        return CommandLine.arguments[i + 1]
    }

    func activate() {
        guard !CommandLine.arguments.contains("--background-maintenance") else { return }
        if stoppingServer != nil { reopenAfterStop = true; return }
        if server?.isRunning == true {
            if probeExhausted { resetProbe(); probe() }
            return
        }
        if !hasWorkbenchPage { showMessage("正在打开事项工作台…", "首次读取事项可能需要一分钟。") }
        start()
    }

    func stop() {
        reopenAfterStop = false
        hasWorkbenchPage = false
        hasPendingDraftOrHandoff = false
        guard let process = server else { return }
        server = nil
        if process.isRunning {
            stoppingServer = process
            process.terminate()
        }
    }

    private func serviceFailed(_ title: String, _ message: String) {
        if hasWorkbenchPage {
            webView.callAsyncJavaScript("if (typeof announce === 'function') announce(message)",
                arguments: ["message": message], in: nil, in: .page, completionHandler: nil)
        } else {
            showMessage(title, message)
        }
    }

    private func start() {
        guard let script = Bundle.main.resourceURL?.appendingPathComponent("workbench/psi_sync_center.py"),
              FileManager.default.fileExists(atPath: script.path) else {
            serviceFailed("暂时无法启动", "PSI 安装包缺少事项工作台组件，请重新安装 PSI。"); return
        }
        instance = UUID().uuidString
        resetProbe()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: Self.python)
        process.arguments = [script.path]
        // 仅供合成数据验收（沿用原 --fixture/--data-dir 能力），日常启动不带参数。
        for (own, forwarded) in [("--workbench-fixture", "--fixture"), ("--workbench-data-dir", "--data-dir")] {
            if let value = Self.argument(own) { process.arguments! += [forwarded, value] }
        }
        var env = ProcessInfo.processInfo.environment
        env["PSI_APP_INSTANCE"] = instance
        env["PSI_APP_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"   // 不往已签名包里写 .pyc
        env.removeValue(forKey: "PYTHONPATH")
        process.environment = env
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] ended in
            Task { @MainActor in
                guard let self else { return }
                if self.stoppingServer === ended {
                    self.stoppingServer = nil
                    if self.reopenAfterStop { self.reopenAfterStop = false; self.activate() }
                    return
                }
                guard self.server === ended else { return }
                self.server = nil
                self.serviceFailed("事项工作台服务已停止",
                    "请切换到其他页面后再回到「事项工作台」。若本机 8767 端口被其他程序占用，请先退出该程序；PSI 不会停止其他程序。")
            }
        }
        do { try process.run(); server = process; probe() }
        catch { serviceFailed("暂时无法启动", "本机 Python 3.12 不可用，请检查运行环境。") }
    }

    private func probe() {
        guard server?.isRunning == true else { return }
        let expectedInstance = instance
        var request = URLRequest(url: Self.localURL.appendingPathComponent("health"))
        request.timeoutInterval = 2; request.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            DispatchQueue.main.async {
                guard let self, self.server?.isRunning == true, self.instance == expectedInstance else { return }
                let value = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                if (response as? HTTPURLResponse)?.statusCode == 200, value?["instance"] as? String == self.instance {
                    if self.hasWorkbenchPage {
                        self.webView.callAsyncJavaScript("return await reconnectWorkbench(expectedInstance)",
                            arguments: ["expectedInstance": expectedInstance], in: nil, in: .page) { [weak self] result in
                            guard let self, self.instance == expectedInstance, self.server?.isRunning == true else { return }
                            if (try? result.get() as? Bool) != true {
                                self.retryProbe(expectedInstance)
                            }
                        }
                        return
                    }
                    var components = URLComponents(url: Self.localURL, resolvingAgainstBaseURL: false)!
                    components.fragment = "embedded"
                    self.hasWorkbenchPage = true
                    self.webView.load(URLRequest(url: components.url!)); return
                }
                self.retryProbe(expectedInstance)
            }
        }.resume()
    }

    private func resetProbe() {
        probeExhausted = false
        probeInterval = 0.05
        probeDeadline = ProcessInfo.processInfo.systemUptime + 50
    }

    private func retryProbe(_ expectedInstance: String) {
        guard ProcessInfo.processInfo.systemUptime < probeDeadline else {
            probeExhausted = true
            serviceFailed("工作台服务尚未就绪", hasWorkbenchPage ? "请保留输入，切换到其他页面后再回到事项工作台重试。" : "请关闭主窗口后重新打开事项工作台。")
            return
        }
        let interval = probeInterval
        probeInterval = min(interval * 2, 0.5)
        DispatchQueue.main.asyncAfter(deadline: .now() + interval) { [weak self] in
            guard let self, self.instance == expectedInstance else { return }
            self.probe()
        }
    }

    func showMessage(_ title: String, _ message: String) {
        webView.loadHTMLString("<meta charset='utf-8'><body style='font:17px -apple-system;padding:60px'><h2>\(title)</h2><p>\(message)</p></body>", baseURL: nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        // 与原实现相同：只放行 about: 与 http://127.0.0.1:8767（无 user/password）
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        let local = url.scheme == "http" && url.host == "127.0.0.1" && url.port == 8767 && url.user == nil && url.password == nil
        decisionHandler(url.scheme == "about" || local ? .allow : .cancel)
    }
}

struct IssueWorkbenchView: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let container = NSView()
        let web = IssueWorkbenchService.shared.webView
        web.removeFromSuperview()
        web.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(web)
        NSLayoutConstraint.activate([
            web.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            web.topAnchor.constraint(equalTo: container.topAnchor),
            web.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        return container
    }
    @Environment(\.colorScheme) private var colorScheme
    func updateNSView(_ nsView: NSView, context: Context) {
        let web = IssueWorkbenchService.shared.webView
        web.appearance = NSAppearance(named: colorScheme == .dark ? .darkAqua : .aqua)
    }
}
