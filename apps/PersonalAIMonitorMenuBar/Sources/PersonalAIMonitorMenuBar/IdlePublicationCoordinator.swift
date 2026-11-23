import AppKit
import Foundation
import PAMInteractionModel
import Darwin

/// Cooperative update exit uses current draft receipts and current UI activity.
@MainActor
final class IdlePublicationCoordinator {
    private let workbenchHasPendingInput: @MainActor () -> Bool
    private var exitRequest: [String: Any]?
    var hasPublicationRequest: Bool { exitRequest != nil }
    var menuOpen = false
    private weak var statusItem: NSStatusItem?
    private var timer: Timer?
    private let instance = UUID().uuidString.lowercased()
    private let requestURL: URL
    private let revision: String
    private let terminate: @MainActor () -> Void
    init(requestURL: URL? = nil, revision: String? = nil,
         workbenchHasPendingInput: @escaping @MainActor () -> Bool = { false },
         terminate: @escaping @MainActor () -> Void = { NSApp.terminate(nil) }) {
        self.requestURL = requestURL ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/PSI/core/data/feedback/ui-exit-request.json")
        self.revision = revision ?? (Bundle.main.object(forInfoDictionaryKey: "PAMGitRevision") as? String ?? "")
        self.terminate = terminate
        self.workbenchHasPendingInput = workbenchHasPendingInput
    }
    func start() {
        timer?.invalidate()
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            // Swift 5.10 rejects Task capturing the weak optional var. Copy it first.
            let coordinator = self
            Task { @MainActor in coordinator?.poll() }
        }
        // Menu tracking and modal panels must still return their current blocker.
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }
    func registerStatusItem(_ item: NSStatusItem) {
        statusItem = item
    }
    private func isStatusItemWindow(_ window: NSWindow) -> Bool {
        // AppKit creates replicas of the status item on other displays. There
        // is no public list of those windows. Anchor their exact runtime type
        // to our live status item, and fail closed for generic/custom windows
        // or any editable/custom content. Do not name private AppKit classes.
        guard let button = statusItem?.button, let actual = button.window,
              type(of: actual) != NSWindow.self, type(of: actual) != NSPanel.self,
              type(of: window) == type(of: actual),
              Bundle(for: type(of: actual)) == Bundle(for: NSWindow.self),
              window.level == .statusBar, !window.canBecomeKey, !window.canBecomeMain,
              window.windowController == nil, window.delegate == nil,
              let content = window.contentView else { return false }
        func iconContent(_ view: NSView) -> Bool {
            guard Bundle(for: type(of: view)) == Bundle(for: NSView.self),
                  !(view is NSTextView), !(view is NSControl) || view === button,
                  !view.acceptsFirstResponder, view.gestureRecognizers.isEmpty else { return false }
            return view.subviews.allSatisfy(iconContent)
        }
        return iconContent(content)
    }
    private struct ExitObservation {
        let checkedAt: Double
        let reasons: [String]
        let interfaceSources: [String]?
    }
    private func observe(created: Double) -> ExitObservation {
        // One synchronous main-thread snapshot explains the same decision.
        // Only fixed codes leave the app: no titles, identifiers or draft text.
        var drafts = PublicationDrafts.shared.statuses
        if workbenchHasPendingInput() { drafts.append(.unsaved) }
        let active = NSApp.isActive
        let menu = menuOpen
        let windows = NSApp.windows
        let window = windows.contains { ($0.isVisible && !isStatusItemWindow($0)) || $0.isMiniaturized }
        let modal = NSApp.modalWindow != nil || windows.contains { $0.attachedSheet != nil }
        let edited = windows.contains { $0.isDocumentEdited }
        let checkedAt = Date().timeIntervalSince1970
        let reasons = IdlePublicationPolicy.reasons(drafts: drafts,
            active: active, menuOpen: menu, modal: modal,
            windowAtRisk: window || edited,
            age: checkedAt - created)
        let sources = [("menu_open", menu), ("application_active", active),
                       ("window", window), ("modal", modal), ("edited", edited)]
            .filter { $0.1 }.map { $0.0 }
        return ExitObservation(checkedAt: checkedAt, reasons: reasons,
            interfaceSources: reasons.contains("request_expired") ? nil : sources)
    }
    func shouldTerminate() -> NSApplication.TerminateReply {
        guard let request = exitRequest, let created = request["created"] as? Double else {
            let blocked = PublicationDrafts.shared.statuses.contains { $0 != .clean }
                || NSApp.windows.contains { $0.isDocumentEdited || $0.attachedSheet != nil }
                || NSApp.modalWindow != nil
            return blocked ? .terminateCancel : .terminateNow
        }
        // Recheck after AppKit's own document checks, on the same main thread.
        let url = requestURL
        guard let current = readRequest(url), NSDictionary(dictionary: current).isEqual(to: request) else {
            _ = writeReceipt(request, observation: ExitObservation(checkedAt: Date().timeIntervalSince1970,
                reasons: ["request_expired"], interfaceSources: nil), requestURL: url)
            exitRequest = nil; return .terminateCancel
        }
        let observation = observe(created: created)
        if !observation.reasons.isEmpty {
            _ = writeReceipt(request, observation: observation, requestURL: url)
            exitRequest = nil
            return .terminateCancel
        }
        return .terminateNow
    }
    private func writeReceipt(_ request: [String: Any], observation: ExitObservation, requestURL: URL) -> Bool {
        let parent = requestURL.deletingLastPathComponent()
        guard parent.resolvingSymlinksInPath() == parent else { return false }
        var payload: [String: Any] = ["schema": 1, "instance": instance, "pid": request["pid"]!, "revision": request["revision"]!,
            "nonce": request["nonce"]!, "request_created": request["created"]!,
            "checked_at": observation.checkedAt, "reasons": observation.reasons, "can_exit": observation.reasons.isEmpty]
        if let sources = observation.interfaceSources { payload["interface_sources"] = sources }
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else { return false }
        let temporary = parent.appendingPathComponent(".ui-exit-" + UUID().uuidString)
        let output = parent.appendingPathComponent("ui-exit-status.json")
        let fd = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return false }
        defer { close(fd); unlink(temporary.path) }
        let count = data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        guard count == data.count, fsync(fd) == 0, rename(temporary.path, output.path) == 0 else { return false }
        return true
    }
    private func readRequest(_ url: URL) -> [String: Any]? {
        guard url.deletingLastPathComponent().resolvingSymlinksInPath() == url.deletingLastPathComponent() else { return nil }
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var metadata = stat()
        guard fstat(fd, &metadata) == 0, metadata.st_mode & S_IFMT == S_IFREG,
              metadata.st_uid == getuid(), metadata.st_mode & 0o077 == 0,
              metadata.st_nlink == 1, metadata.st_size > 0, metadata.st_size < 4096 else { return nil }
        var bytes = [UInt8](repeating: 0, count: 4096)
        let count = read(fd, &bytes, bytes.count)
        guard count == Int(metadata.st_size), count < 4096 else { return nil }
        let data = Data(bytes.prefix(count))
        guard
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(value.keys) == Set(["pid", "revision", "nonce", "created"]), !revision.isEmpty,
              value["pid"] as? Int == Int(ProcessInfo.processInfo.processIdentifier),
              value["revision"] as? String == revision,
              let nonce = value["nonce"] as? String, UUID(uuidString: nonce)?.uuidString.lowercased() == nonce,
              let created = value["created"] as? Double,
              created.isFinite, (0...2.5).contains(Date().timeIntervalSince1970 - created) else { return nil }
        return value
    }
    func poll() {
        let url = requestURL
        guard let value = readRequest(url), let created = value["created"] as? Double else { return }
        let observation = observe(created: created)
        guard writeReceipt(value, observation: observation, requestURL: url) else { return }
        guard observation.reasons.isEmpty else { return }
        exitRequest = value
        terminate()
        exitRequest = nil
    }
}
