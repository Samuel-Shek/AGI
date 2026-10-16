import AppKit
import Foundation
import PAMInteractionModel
import Darwin

/// A background launch can yield only before any user interaction. Closing a
/// window does not clear this latch: SwiftUI may still hold unsaved drafts.
@MainActor
final class IdlePublicationCoordinator {
    static let shared = IdlePublicationCoordinator()
    private var untouched = CommandLine.arguments.contains("--background-maintenance")
    private var timer: Timer?
    private var monitor: Any?
    func userInteraction() { untouched = false }
    func start() {
        monitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .leftMouseDown, .rightMouseDown]) { [weak self] event in
            self?.userInteraction(); return event
        }
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.poll() }
        }
    }
    private func poll() {
        guard untouched, !NSApp.isActive, NSApp.modalWindow == nil,
              !NSApp.windows.contains(where: { $0.isVisible || $0.isMiniaturized || $0.isDocumentEdited }) else { return }
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Personal AI Monitor/core/data/feedback/ui-exit-request.json")
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return }
        defer { close(fd) }
        var metadata = stat()
        guard fstat(fd, &metadata) == 0, metadata.st_mode & S_IFMT == S_IFREG,
              metadata.st_uid == getuid(), metadata.st_mode & 0o077 == 0,
              metadata.st_nlink == 1, metadata.st_size > 0, metadata.st_size < 4096 else { return }
        var bytes = [UInt8](repeating: 0, count: 4096)
        let count = read(fd, &bytes, bytes.count)
        guard count == Int(metadata.st_size), count < 4096 else { return }
        let data = Data(bytes.prefix(count))
        guard
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              value["pid"] as? Int == Int(ProcessInfo.processInfo.processIdentifier),
              value["revision"] as? String == Bundle.main.object(forInfoDictionaryKey: "PAMGitRevision") as? String,
              let nonce = value["nonce"] as? String, UUID(uuidString: nonce) != nil,
              let created = value["created"] as? Double,
              IdlePublicationPolicy.permitsExit(untouched: untouched, active: NSApp.isActive,
                  modal: NSApp.modalWindow != nil,
                  windowAtRisk: NSApp.windows.contains(where: { $0.isVisible || $0.isMiniaturized || $0.isDocumentEdited }),
                  age: Date().timeIntervalSince1970 - created) else { return }
        // Main-thread latch is irreversible for this process. No windows/drafts
        // have ever been exposed; graceful exit leaves the background service on.
        untouched = false
        NSApp.terminate(nil)
    }
}
