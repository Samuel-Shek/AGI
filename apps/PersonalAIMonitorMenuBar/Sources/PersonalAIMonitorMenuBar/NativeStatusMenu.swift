import AppKit
import Foundation
import PAMInteractionModel

/// A stable AppKit menu: ticking the countdown never rebuilds a SwiftUI menu host.
@MainActor
final class NativeStatusMenu: NSObject, NSMenuDelegate {
    static let shared = NativeStatusMenu()
    private var runner: CoreRunner?
    private var statusItem: NSStatusItem?
    private var timer: Timer?
    private var refreshTask: Task<Void, Never>?
    private let menu = NSMenu()
    private let statusLine = NSMenuItem(title: "等待读取", action: nil, keyEquivalent: "")
    private let countdown = NSMenuItem(title: "--", action: nil, keyEquivalent: "")
    private var capture: NSMenuItem!
    private var beginCapture: NSMenuItem!
    private var endCapture: NSMenuItem!
    private let fileProgress = NSMenuItem(title: "文件追踪状态待确认", action: nil, keyEquivalent: "")
    private var pause: NSMenuItem!
    private var resume: NSMenuItem!
    private var start: NSMenuItem!
    private var quit: NSMenuItem!

    func register(runner: CoreRunner) { self.runner = runner }

    // Keep the same menu reachable by keyboard when the system hides its icon.
    func showMenu() { statusItem?.button?.performClick(nil) }

    func install() {
        guard statusItem == nil, runner != nil else { return }
        menu.autoenablesItems = false
        menu.delegate = self
        let heading = NSMenuItem(title: "AGI · 个人 AI 工作系统", action: nil, keyEquivalent: "")
        heading.isEnabled = false
        menu.addItem(heading)
        statusLine.isEnabled = false
        countdown.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(countdown)
        fileProgress.isEnabled = false
        menu.addItem(fileProgress)
        menu.addItem(.separator())
        menu.addItem(item("打开 AGI", #selector(openPanel)))
        menu.addItem(item("自主进化", #selector(openEvolution)))
        menu.addItem(item("供 AGI 使用的摘要…", #selector(openAGISharing)))
        menu.addItem(item("给 Codex 的上下文…", #selector(openCodexSharing)))
        menu.addItem(.separator())
        beginCapture = item("采集 30 分钟", #selector(beginScreenCapture))
        endCapture = item("提前结束屏幕采集", #selector(endScreenCapture))
        menu.addItem(beginCapture)
        menu.addItem(endCapture)
        capture = item("立即采集", #selector(captureNow))
        menu.addItem(capture)
        pause = NSMenuItem(title: "暂停", action: nil, keyEquivalent: "")
        let pauseMenu = NSMenu(title: "暂停")
        pauseMenu.autoenablesItems = false
        pauseMenu.addItem(item("暂停 15 分钟", #selector(pauseMinutes)))
        pauseMenu.addItem(item("暂停到锁屏", #selector(pauseUntilLock)))
        pauseMenu.addItem(item("今天不记录", #selector(pauseToday)))
        pause.submenu = pauseMenu
        menu.addItem(pause)
        resume = item("继续记录", #selector(resumeRecording))
        start = item("开始记录", #selector(startRecording))
        menu.addItem(resume)
        menu.addItem(start)
        menu.addItem(.separator())
        menu.addItem(item("刷新状态", #selector(refreshStatus)))
        quit = item("退出", #selector(stopAndQuit))
        menu.addItem(quit)
        let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let icon = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            NSColor.labelColor.setStroke()
            for start: CGFloat in [55, 175, 295] {
                let ring = NSBezierPath()
                ring.appendArc(withCenter: NSPoint(x: 9, y: 9), radius: 6, startAngle: start, endAngle: start + 70)
                ring.lineWidth = 2.25; ring.lineCapStyle = .butt; ring.stroke()
            }
            NSColor.labelColor.setFill()
            NSBezierPath(ovalIn: NSRect(x: 6.9, y: 6.9, width: 4.2, height: 4.2)).fill()
            return true
        }
        icon.isTemplate = true
        statusItem.button?.image = icon
        statusItem.button?.imagePosition = .imageLeading
        statusItem.button?.font = .monospacedSystemFont(ofSize: 11, weight: .medium)
        statusItem.button?.setAccessibilityLabel("AGI")
        statusItem.menu = menu
        self.statusItem = statusItem
        updateText()
        updateActions()
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.updateText() }
        }
        timer.tolerance = 0.1
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func item(_ title: String, _ action: Selector) -> NSMenuItem {
        let result = NSMenuItem(title: title, action: action, keyEquivalent: "")
        result.target = self
        return result
    }

    private func updateText() {
        guard let runner else { return }
        let now = Date()
        let title = runner.statusSnapshot.menuBarTitle(now: now)
        if statusItem?.button?.title != title { statusItem?.button?.title = title }
        if statusLine.title != runner.statusLine { statusLine.title = runner.statusLine }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let expiry = iso.date(from: runner.statusSnapshot.screenCaptureExpiresAt)
        let remaining = max(0, Int((expiry ?? now).timeIntervalSince(now)))
        let lease = runner.statusSnapshot.screenCaptureEnabled && remaining > 0
            ? "屏幕采集剩余 \(remaining / 60):\(String(format: "%02d", remaining % 60))  ·  "
            : "仅文件追踪  ·  "
        if fileProgress.title != runner.statusSnapshot.fileProgressLabel { fileProgress.title = runner.statusSnapshot.fileProgressLabel }
        let next = (runner.statusSnapshot.recordingPaused ? "记录已暂停 · " : "") + lease + runner.statusSnapshot.nextCaptureStatusLine(now: now)
            + (runner.captureFeedbackActive(at: now) ? "    已截屏" : "")
        if countdown.title != next { countdown.title = next }
    }

    private func updateActions() {
        guard let runner else { return }
        let snapshot = runner.statusSnapshot
        let presentation = RecordingControlPresentation.resolve(
            status: snapshot.status, launchAgentLoaded: snapshot.launchAgentLoaded,
            launchAgentRunning: snapshot.launchAgentRunning, pauseLabel: snapshot.pauseLabel,
            statusExplanation: snapshot.statusExplanation, recordingPaused: snapshot.recordingPaused)
        beginCapture.isHidden = snapshot.screenCaptureEnabled
        beginCapture.isEnabled = !snapshot.displayStateUnknown && snapshot.launchAgentRunning
        endCapture.isHidden = !snapshot.screenCaptureEnabled
        endCapture.isEnabled = !snapshot.displayStateUnknown
        capture.isHidden = !snapshot.screenCaptureEnabled || !snapshot.captureActionsAvailable || presentation.action != .pause
        pause.isHidden = capture.isHidden
        capture.isEnabled = !snapshot.captureInteractionBlocked
        pause.isEnabled = !snapshot.captureInteractionBlocked
        resume.isHidden = !snapshot.captureActionsAvailable || presentation.action != .resume
        start.isHidden = !snapshot.captureActionsAvailable || presentation.action != .start
        quit.isHidden = false
        quit.title = "停止 AGI 全部后台并退出"
    }

    func menuWillOpen(_ menu: NSMenu) {
        IdlePublicationCoordinator.shared.userInteraction()
        runner?.setMenuVisible(true)
        updateText()
        updateActions()
        guard refreshTask == nil, let runner else { return }
        refreshTask = Task { [weak self] in
            await runner.refreshForInteraction()
            self?.refreshTask = nil
            // Keep the item set fixed throughout menu tracking. The next open
            // applies new action availability; action handlers recheck state.
            self?.updateText()
        }
    }

    func menuDidClose(_ menu: NSMenu) { runner?.setMenuVisible(false) }

    @objc private func openCodexSharing() {
        guard let runner else { return }
        CodexSharingWindow.shared.show(runner: runner)
    }

    @objc private func openEvolution() {
        guard let runner else { return }
        AppWindowPresenter.shared.show(runner: runner, tab: .evolution)
    }
    @objc private func openAGISharing() {
        guard let runner else { return }
        CodexSharingWindow.shared.show(runner: runner, consumer: .agi)
    }
    @objc private func openPanel() {
        guard let runner else { return }
        AppWindowPresenter.shared.show(runner: runner, tab: .today)
        Task { await runner.refreshForInteraction() }
    }
    @objc private func refreshStatus() {
        guard let runner else { return }
        Task { await runner.refreshStatus() }
    }
    @objc private func beginScreenCapture() {
        guard let runner else { return }
        Task { await runner.beginScreenCapture() }
    }
    @objc private func endScreenCapture() {
        guard let runner else { return }
        Task { await runner.endScreenCapture() }
    }
    @objc private func captureNow() {
        guard let runner, runner.statusSnapshot.captureActionsAvailable,
              !runner.statusSnapshot.captureInteractionBlocked else { return }
        Task { await runner.captureNow() }
    }
    @objc private func pauseMinutes() {
        guard let runner, runner.statusSnapshot.captureActionsAvailable else { return }
        Task { await runner.pause(minutes: 15) }
    }
    @objc private func pauseUntilLock() {
        guard let runner, runner.statusSnapshot.captureActionsAvailable else { return }
        Task { await runner.pauseUntilLock() }
    }
    @objc private func pauseToday() {
        guard let runner, runner.statusSnapshot.captureActionsAvailable else { return }
        Task { await runner.pauseToday() }
    }
    @objc private func resumeRecording() {
        guard let runner, runner.statusSnapshot.captureActionsAvailable else { return }
        Task { await runner.resume() }
    }
    @objc private func startRecording() {
        guard let runner, runner.statusSnapshot.captureActionsAvailable else { return }
        Task { await runner.startRecordingService() }
    }
    @objc private func stopAndQuit() {
        guard let runner else { return }
        Task { await runner.stopRecordingAndQuit() }
    }
}
