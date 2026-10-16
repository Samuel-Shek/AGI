import AppKit
import CoreGraphics
import CryptoKit
import Darwin
import Foundation
import LocalAuthentication
import PAMInteractionModel
import SwiftUI
import UniformTypeIdentifiers
import UserNotifications

struct PersonalAIMonitorMenuBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var runner: CoreRunner
    private let phase2Enabled = false

    init() {
        let runner = CoreRunner()
        _runner = StateObject(wrappedValue: runner)
        AppWindowPresenter.shared.register(runner: runner)
        NativeStatusMenu.shared.register(runner: runner)
        AppIconInstaller.apply()
    }

    var body: some Scene {
        Settings { EmptyView() }
            .commands {
                // The application owns one native control window. Route the
                // standard Settings command there instead of opening an empty scene.
                CommandGroup(replacing: .appSettings) {
                    Button("设置…") { AppWindowPresenter.shared.showRegistered(tab: .settings) }
                        .keyboardShortcut(",", modifiers: .command)
                }
                CommandMenu("AGI") {
                    Button("打开记录菜单") { NativeStatusMenu.shared.showMenu() }
                        .keyboardShortcut("m", modifiers: [.command, .shift])
                    Button("给 Codex 的上下文…") {
                        CodexSharingWindow.shared.show(runner: runner)
                    }
                        .keyboardShortcut("c", modifiers: [.command, .shift])
                }
            }
    }
}

PersonalAIMonitorMenuBarApp.main()

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        if let bundle = Bundle.main.bundleIdentifier,
           let existing = NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
                .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier && !$0.isTerminated }) {
            existing.activate(options: [])
            NSApp.terminate(nil)
            return
        }
        AppIconInstaller.apply()
        NativeStatusMenu.shared.install()
        IdlePublicationCoordinator.shared.start()
        if !CommandLine.arguments.contains("--background-maintenance") {
            AppWindowPresenter.shared.showRegistered(tab: .today)
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        IdlePublicationCoordinator.shared.userInteraction()
        AppWindowPresenter.shared.showRegistered()
        return true
    }
}

@MainActor
final class LocalNotificationCoordinator {
    static let shared = LocalNotificationCoordinator()

    private let center = UNUserNotificationCenter.current()
    private let defaults = UserDefaults.standard
    private let episodeDigestKey = "pam.notifications.high-value-episode-digests"
    private let highValueIdentifier = "pam.confirmations.high-value"
    private let batchIdentifier = "pam.confirmations.2230-batch"
    private let anomalyIdentifier = "pam.context-console.system-anomaly"
    private let anomalyDigestKey = "pam.notifications.context-console-anomaly-digest"
    private var anomalyGeneration = 0
    private var phase2Enabled: Bool {
        ContextConsolePhase2FeatureFlag.isEnabled(in: defaults)
    }

    func update(
        snapshot: ActivityConfirmationSnapshot,
        pendingGroups: [ActivityConfirmationQuestionGroup]? = nil
    ) {
        if phase2Enabled {
            // The context console never interrupts for ordinary confirmations.
            // Enabling Phase 2 also removes requests scheduled by the legacy UI.
            clearLegacySchedules()
            defaults.removeObject(forKey: episodeDigestKey)
            return
        }
        guard !snapshot.isBlocked, snapshot.contractVersion >= 4 else {
            clearPendingSchedules()
            return
        }
        let eligibleEpisodeDigests = Set((pendingGroups ?? snapshot.pendingGroups).compactMap { group -> String? in
            guard ActivityConfirmationNotificationPolicy.isHighValueClosedEpisode(
                state: group.state,
                priority: group.priority,
                notificationEligible: group.notificationEligible,
                episodeID: group.episodeID,
                observedEnd: group.observedEnd,
                now: Date()
            ) else { return nil }
            return Self.digest(group.episodeID)
        })
        let previousEpisodeDigests = Set(defaults.stringArray(forKey: episodeDigestKey) ?? [])
        let hasNewHighValueEpisode = !eligibleEpisodeDigests.subtracting(previousEpisodeDigests).isEmpty
        let shouldScheduleBatch = ActivityConfirmationNotificationPolicy.shouldScheduleBatch(
            pendingQuestionCount: snapshot.pendingQuestionCount
        )

        if eligibleEpisodeDigests.isEmpty && !shouldScheduleBatch {
            clearPendingSchedules()
            defaults.set([], forKey: episodeDigestKey)
            return
        }

        Task {
            let authorized = await requestAuthorizationIfNeeded()
            guard authorized else { return }
            defaults.set(Array(eligibleEpisodeDigests).sorted(), forKey: episodeDigestKey)
            if hasNewHighValueEpisode {
                await scheduleHighValueNotification()
            }
            await updateBatchSchedule(enabled: shouldScheduleBatch)
        }
    }

    private func clearPendingSchedules() {
        center.removePendingNotificationRequests(
            withIdentifiers: [highValueIdentifier, batchIdentifier, anomalyIdentifier]
        )
    }

    private func clearLegacySchedules() {
        center.removePendingNotificationRequests(withIdentifiers: [highValueIdentifier, batchIdentifier])
    }

    func activatePhase2() {
        guard phase2Enabled else { return }
        clearLegacySchedules()
        defaults.removeObject(forKey: episodeDigestKey)
    }

    func updateSystemHealth(snapshot: StatusSnapshot) {
        guard phase2Enabled else { return }
        clearLegacySchedules()
        anomalyGeneration += 1
        let generation = anomalyGeneration

        guard let anomalyDigest = anomalyDigest(for: snapshot) else {
            center.removePendingNotificationRequests(withIdentifiers: [anomalyIdentifier])
            defaults.removeObject(forKey: anomalyDigestKey)
            return
        }
        guard defaults.string(forKey: anomalyDigestKey) != anomalyDigest else { return }

        // Delay the single content-free alert. A normal/paused refresh during
        // this window removes it, so transient database contention stays quiet.
        Task {
            let authorized = await requestAuthorizationIfNeeded()
            guard authorized,
                  generation == anomalyGeneration,
                  self.phase2Enabled
            else { return }
            center.removePendingNotificationRequests(withIdentifiers: [anomalyIdentifier])
            let content = UNMutableNotificationContent()
            content.title = "AGI 需要检查"
            content.body = "系统状态持续异常，请打开 PAM 查看详情。"
            content.sound = .default
            let request = UNNotificationRequest(
                identifier: anomalyIdentifier,
                content: content,
                trigger: UNTimeIntervalNotificationTrigger(timeInterval: 30, repeats: false)
            )
            do {
                try await center.add(request)
                defaults.set(anomalyDigest, forKey: anomalyDigestKey)
            } catch {
                defaults.removeObject(forKey: anomalyDigestKey)
            }
        }
    }

    private func anomalyDigest(for snapshot: StatusSnapshot) -> String? {
        let health = Phase2SystemHealth.resolve(
            displayStateUnknown: snapshot.displayStateUnknown,
            status: snapshot.status,
            statusIsDanger: snapshot.statusKind == .danger,
            hasKnownCaptureError: snapshot.hasKnownCaptureError,
            launchAgentLoaded: snapshot.launchAgentLoaded,
            launchAgentRunning: snapshot.launchAgentRunning,
            captureFreshnessIsStale: snapshot.captureFreshness.isStale,
            captureBlockedBySession: snapshot.captureBlockedBySession
        )
        guard health == .error else {
            return nil
        }
        return Self.digest("error|\(snapshot.statusLabel)|\(snapshot.reasonCode)")
    }

    private func requestAuthorizationIfNeeded() async -> Bool {
        let settings = await center.notificationSettings()
        switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral:
            return true
        case .notDetermined:
            return (try? await center.requestAuthorization(options: [.alert, .badge, .sound])) == true
        case .denied:
            return false
        @unknown default:
            return false
        }
    }

    private func scheduleHighValueNotification() async {
        let content = UNMutableNotificationContent()
        content.title = ActivityConfirmationNotificationPolicy.title
        content.body = ActivityConfirmationNotificationPolicy.highValueBody
        content.sound = .default
        let request = UNNotificationRequest(
            identifier: highValueIdentifier,
            content: content,
            trigger: UNTimeIntervalNotificationTrigger(timeInterval: 2, repeats: false)
        )
        try? await center.add(request)
    }

    private func updateBatchSchedule(enabled: Bool) async {
        center.removePendingNotificationRequests(withIdentifiers: [batchIdentifier])
        guard enabled else { return }
        let content = UNMutableNotificationContent()
        content.title = ActivityConfirmationNotificationPolicy.title
        content.body = ActivityConfirmationNotificationPolicy.batchBody
        content.sound = .default
        let request = UNNotificationRequest(
            identifier: batchIdentifier,
            content: content,
            trigger: UNCalendarNotificationTrigger(
                dateMatching: DateComponents(hour: 22, minute: 30),
                repeats: true
            )
        )
        try? await center.add(request)
    }

    private static func digest(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

private enum AppIconInstaller {
    static func apply() {
        guard let image = appIconImage() else { return }
        NSApplication.shared.applicationIconImage = image
    }

    static func appIconImage() -> NSImage? {
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
           let image = NSImage(contentsOf: url) {
            return image
        }
        return NSApplication.shared.applicationIconImage
    }
}

@MainActor
final class AppWindowState: ObservableObject {
    @Published var selectedTab: AppTab = .today
    @Published var sidebarVisibility: NavigationSplitViewVisibility = .all
    @Published var evolutionFocusFeedback = false
    @Published var quickCaptureRequestID = 0
    @Published var phase2DestinationRequestID = 0
    @Published var requestedPhase2Destination: Phase2Destination = .today
}

enum MarkdownExportKind: Equatable {
    case full
    case learning
    case quality

    var coreFormat: String {
        switch self {
        case .full: return "markdown"
        case .learning: return "learning-markdown"
        case .quality: return "markdown"
        }
    }

    var fileSuffix: String {
        switch self {
        case .full: return "cleaned-timeline"
        case .learning: return "learning-summary"
        case .quality: return "quality-report"
        }
    }

    var displayName: String {
        switch self {
        case .full: return "完整日志"
        case .learning: return "学习摘要"
        case .quality: return "质量报告"
        }
    }
}

/// Immutable provenance embedded by the release build script.
struct AppBuildInfo {
    static let current = AppBuildInfo()

    let version: String
    let build: String
    let timestamp: String
    let gitRevision: String
    let gitDirty: Bool
    let gitDirtyReason: String
    let trackedChangeCount: Int
    let untrackedCount: Int
    let sourceTreeHash: String
    let coreCodeHash: String

    init(bundle: Bundle = .main) {
        let info = bundle.infoDictionary ?? [:]
        version = info["CFBundleShortVersionString"] as? String ?? "unknown"
        build = info["CFBundleVersion"] as? String ?? "unknown"
        timestamp = info["PAMBuildTimestamp"] as? String ?? "unknown"
        gitRevision = info["PAMGitRevision"] as? String ?? "unknown"
        gitDirty = info["PAMGitDirty"] as? Bool ?? true
        gitDirtyReason = info["PAMGitDirtyReason"] as? String ?? "provenance_missing"
        trackedChangeCount = info["PAMGitTrackedChangeCount"] as? Int ?? 0
        untrackedCount = info["PAMGitUntrackedCount"] as? Int ?? 0
        sourceTreeHash = info["PAMSourceTreeHash"] as? String ?? "unknown"
        coreCodeHash = info["PAMCoreCodeHash"] as? String ?? "unknown"
    }

    var shortLabel: String {
        "v\(version) (\(build))"
    }

    var sourceLabel: String {
        gitDirty ? "\(gitRevision) + local changes" : gitRevision
    }

    var integrityLabel: String {
        gitDirty ? "开发快照" : "可复现构建"
    }

    var detailText: String {
        let hash = sourceTreeHash == "unknown" ? "hash unknown" : String(sourceTreeHash.prefix(12))
        if gitDirty {
            return "Build \(build) / \(timestamp) / \(sourceLabel) / \(gitDirtyReason) / \(hash)"
        }
        return "Build \(build) / \(timestamp) / \(sourceLabel) / \(hash)"
    }
}

enum DeletePreset: Hashable, CaseIterable, Identifiable {
    case last15
    case last60
    case today
    case yesterday

    var id: Self { self }

    var title: String {
        switch self {
        case .last15: return "最近 15 分钟"
        case .last60: return "最近 1 小时"
        case .today: return "今天"
        case .yesterday: return "昨天"
        }
    }
}

enum ActivityRangePreset: Hashable, CaseIterable, Identifiable {
    case last15
    case last60
    case today
    case yesterday

    var id: Self { self }

    var title: String {
        switch self {
        case .last15: return "15 分钟"
        case .last60: return "1 小时"
        case .today: return "今天"
        case .yesterday: return "昨天"
        }
    }
}

@MainActor
final class AppWindowToolbar: NSObject, NSToolbarDelegate {
    let state: AppWindowState
    init(state: AppWindowState) { self.state = state }
    private let sidebarID = NSToolbarItem.Identifier("AGI.sidebar")
    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] { [sidebarID, .flexibleSpace] }
    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] { [sidebarID, .flexibleSpace] }
    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier identifier: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        guard identifier == sidebarID else { return nil }
        let item = NSToolbarItem(itemIdentifier: identifier)
        item.label = "显示或隐藏侧栏"
        item.toolTip = item.label
        item.image = NSImage(systemSymbolName: "sidebar.left", accessibilityDescription: item.label)
        item.target = self
        item.action = #selector(toggleSidebar)
        return item
    }
    @objc private func toggleSidebar() {
        state.sidebarVisibility = state.sidebarVisibility == .detailOnly ? .all : .detailOnly
    }
}

@MainActor
final class AppWindowPresenter {
    static let shared = AppWindowPresenter()
    private let windowState = AppWindowState()
    private var window: NSWindow?
    private var windowToolbar: AppWindowToolbar?
    private weak var runner: CoreRunner?
    private var windowCloseObserver: NSObjectProtocol?
    private var windowMiniaturizeObserver: NSObjectProtocol?
    private var windowDeminiaturizeObserver: NSObjectProtocol?
    private var appHideObserver: NSObjectProtocol?
    private var appUnhideObserver: NSObjectProtocol?

    func register(runner: CoreRunner) {
        self.runner = runner
    }

    func applyAppearance(_ value: String) {
        window?.appearance = value == "dark" ? NSAppearance(named: .darkAqua)
            : value == "light" ? NSAppearance(named: .aqua) : nil
    }

    func showRegistered(tab: AppTab? = nil) {
        guard let runner else { return }
        show(runner: runner, tab: tab ?? windowState.selectedTab)
    }

    private func markControlWindowVisibleIfCurrentWindowIsActive() {
        guard let window, window.isVisible, !window.isMiniaturized else { return }
        runner?.setControlWindowVisible(true)
    }

    func show(runner: CoreRunner, tab: AppTab = .today) {
        IdlePublicationCoordinator.shared.userInteraction()
        register(runner: runner)
        windowState.selectedTab = tab
        if window == nil {
            let controller = NSHostingController(
                rootView: PAMAppRootView(runner: runner, windowState: windowState)
            )
            let newWindow = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 980, height: 680),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered,
                defer: false
            )
            newWindow.title = "AGI"
            let container = NSViewController()
            container.view = NSView(frame: newWindow.contentLayoutRect)
            container.addChild(controller)
            newWindow.contentViewController = container
            controller.safeAreaRegions = [] // The window layout guide supplies the inset once.
            controller.sizingOptions = [] // The resizable native window owns its size.
            controller.view.translatesAutoresizingMaskIntoConstraints = false
            container.view.addSubview(controller.view)
            NSLayoutConstraint.activate([
                controller.view.leadingAnchor.constraint(equalTo: container.view.leadingAnchor),
                controller.view.trailingAnchor.constraint(equalTo: container.view.trailingAnchor),
                controller.view.topAnchor.constraint(equalTo: (newWindow.contentLayoutGuide as! NSLayoutGuide).topAnchor),
                controller.view.bottomAnchor.constraint(equalTo: (newWindow.contentLayoutGuide as! NSLayoutGuide).bottomAnchor)
            ])
            newWindow.miniwindowImage = AppIconInstaller.appIconImage()
            newWindow.isReleasedWhenClosed = false
            let toolbarDelegate = AppWindowToolbar(state: windowState)
            let toolbar = NSToolbar(identifier: "AGI.main")
            toolbar.delegate = toolbarDelegate
            toolbar.displayMode = .iconOnly
            windowToolbar = toolbarDelegate
            newWindow.toolbar = toolbar
            newWindow.toolbarStyle = .unified
            newWindow.contentMinSize = NSSize(width: 820, height: 600)
            newWindow.setContentSize(NSSize(width: 980, height: 680))
            newWindow.center()
            window = newWindow
            applyAppearance(UserDefaults.standard.string(forKey: "agi.appearance") ?? "system")
            windowCloseObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification,
                object: newWindow,
                queue: .main
            ) { _ in
                Task { @MainActor in
                    AppWindowPresenter.shared.runner?.setControlWindowVisible(false)
                    NSApp.setActivationPolicy(.accessory)
                }
            }
            windowMiniaturizeObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.didMiniaturizeNotification,
                object: newWindow,
                queue: .main
            ) { _ in
                Task { @MainActor in
                    AppWindowPresenter.shared.runner?.setControlWindowVisible(false)
                }
            }
            windowDeminiaturizeObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.didDeminiaturizeNotification,
                object: newWindow,
                queue: .main
            ) { _ in
                Task { @MainActor in
                    AppWindowPresenter.shared.markControlWindowVisibleIfCurrentWindowIsActive()
                }
            }
            appHideObserver = NotificationCenter.default.addObserver(
                forName: NSApplication.didHideNotification,
                object: NSApp,
                queue: .main
            ) { _ in
                Task { @MainActor in
                    AppWindowPresenter.shared.runner?.setControlWindowVisible(false)
                }
            }
            appUnhideObserver = NotificationCenter.default.addObserver(
                forName: NSApplication.didUnhideNotification,
                object: NSApp,
                queue: .main
            ) { _ in
                Task { @MainActor in
                    AppWindowPresenter.shared.markControlWindowVisibleIfCurrentWindowIsActive()
                }
            }
        }
        // 主窗口可见时切回 .regular，让 Dock 图标 / 台前调度缩略图正常显示；
        // 窗口关闭时由 willCloseNotification 切回 .accessory，恢复纯菜单栏形态。
        NSApp.setActivationPolicy(.regular)
        runner.setControlWindowVisible(true)
        if tab == .confirmations {
            Task { await runner.refreshActivityConfirmationsIfNeeded() }
        } else if tab == .fileHistory {
            Task { await runner.refreshFileHistory(range: "today") }
        }
        window?.deminiaturize(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func presentQuickCapture() {
        windowState.quickCaptureRequestID += 1
    }

    func presentPhase2Destination(_ destination: Phase2Destination) {
        windowState.requestedPhase2Destination = destination
        windowState.phase2DestinationRequestID += 1
    }
}

struct ContentView: View {
    @ObservedObject var runner: CoreRunner
    @ObservedObject var windowState: AppWindowState
    @State private var query = ""
    @State private var rangeFrom = ""
    @State private var rangeTo = ""
    @State private var rangeLabel = "最近 1 小时"
    @State private var rangePreset: ActivityRangePreset = .last60
    @State private var deletePreset: DeletePreset = .last15
    @State private var deleteArmed = false
    @State private var privacyPreviewApp = ""
    @State private var privacyPreviewDomain = ""
    @State private var memoryApprovalRequest: MemoryApprovalRequest?
    @State private var decisionQueueWriteRequest: DecisionQueueWriteRequest?
    @State private var advancedSettingsExpanded = false
    @State private var confirmationImpactExpanded = false
    @State private var confirmationCalibrationsExpanded = false
    @State private var confirmationPrivacyExpanded = false
    @State private var confirmationDiagnosticsExpanded = false
    @State private var confirmationInformationInsufficientExpanded = false
    @State private var confirmationVisibleGroupCount = 12
    @State private var confirmationCardDrafts: [String: ActivityConfirmationCardDraft] = [:]
    @State private var manualActivityEntryPresented = false
    @StateObject private var attention = AttentionCenterModel()
    @ObservedObject private var feedback = FeedbackModel.shared
    @ObservedObject private var evolution = EvolutionModel.shared
    @State private var todayRefreshRequested = false
    @State private var recordsMode = 0
    @State private var settingsMode = 0
    @AppStorage("agi.appearance") private var appearance = "system"
    @State private var memoryReviewDrafts: [String: String] = [:]
    @State private var memoryReviewShowDecided = false
    @State private var fileHistoryRange = "today"
    @State private var fileHistoryUnknownExpanded = false

    var body: some View {
        NavigationSplitView(columnVisibility: $windowState.sidebarVisibility) {
            sidebar.navigationSplitViewColumnWidth(min: 185, ideal: 210, max: 250)
        } detail: {
            selectedTabView
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .navigationSplitViewStyle(.balanced)
        .frame(minWidth: 820, minHeight: 600)
        .background(Color(NSColor.windowBackgroundColor))
        .preferredColorScheme(appearance == "dark" ? .dark : appearance == "light" ? .light : nil)
        .onChange(of: appearance) { _, value in AppWindowPresenter.shared.applyAppearance(value) }
        .task {
            if rangeFrom.isEmpty || rangeTo.isEmpty {
                applyRangePreset(.last60, refresh: false)
            }
            attention.connect(runner)
            async let statusRead: Void = runner.refreshStatus()
            async let attentionRead: Void = attention.refresh(coalescingIfLoading: true)
            _ = await (statusRead, attentionRead)
        }
        .sheet(item: attentionSheetBinding) { context in
            AttentionDetailView(model: attention, runner: runner, presentationID: context.id)
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            Task { await attention.refresh(coalescingIfLoading: true) }
        }
        .onReceive(DistributedNotificationCenter.default().publisher(for: NSNotification.Name("com.apple.screenIsLocked"))) { _ in
            attention.cancelReadRetry()
            runner.clearRevealedMemory()
            attention.close()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didHideNotification)) { _ in
            attention.cancelReadRetry()
            runner.clearRevealedMemory()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didResignActiveNotification)) { _ in
            attention.cancelReadRetry()
        }
        .onChange(of: runner.statusSnapshot.status) { previous, status in
            if status == "locked" || status == "session_state_unknown" { runner.clearRevealedMemory(); attention.close() }
            let safetyTransition = [previous, status].contains { $0 == "locked" || $0 == "session_state_unknown" }
            Task { await attention.refresh(coalescingIfLoading: !safetyTransition) }
        }
        .onChange(of: windowState.selectedTab) { _, tab in
            runner.clearRevealedMemory()
            Task { await attention.refresh(coalescingIfLoading: true) }
            switch tab {
            case .today:
                Task { await runner.refreshStatus() }
            case .confirmations:
                Task { await runner.refreshActivityConfirmationsIfNeeded() }
            case .fileHistory:
                Task { await runner.refreshFileHistory(range: fileHistoryRange) }
            case .review:
                refreshReviewContent()
            case .memoryReview:
                Task { await runner.refreshMemoryReview() }
            case .systemStatus:
                Task {
                    await runner.refreshStatus()
                    await runner.refreshDiagnosticsStatus(force: true)
                }
            case .privacy:
                Task {
                    await runner.refreshPrivacyPolicyStatus()
                    await runner.refreshDocumentChangesStatus()
                }
            case .settings, .evolution, .reflection:
                break
            }
        }
        .sheet(item: $memoryApprovalRequest) { request in
            MemoryApprovalSheet(request: request, runner: runner)
        }
        .sheet(item: $decisionQueueWriteRequest) { request in
            DecisionQueueWriteSheet(request: request, runner: runner)
        }
        .sheet(isPresented: $manualActivityEntryPresented) {
            ManualActivityEntrySheet(runner: runner)
        }
    }

    private var sidebar: some View {
        List(selection: Binding<AppTab?>(get: { windowState.selectedTab }, set: { if let tab = $0 { windowState.selectedTab = tab } })) {
            Section("日常") {
                ForEach(AppTab.dailyTabs) { tab in
                    HStack {
                        Label(tab.title, systemImage: tab.systemImage)
                        Spacer()
                        if tab == .confirmations, let badge = attention.snapshot.badge {
                            Text(badge).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        }
                    }.tag(tab)
                }
            }
            Section("管理") {
                ForEach(AppTab.managementTabs) { tab in
                    Label(tab.title, systemImage: tab.systemImage).tag(tab)
                }
            }
        }.listStyle(.sidebar)
    }

    private func sidebarGroup(title: String, tabs: [AppTab]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 10)
                .accessibilityAddTraits(.isHeader)
            ForEach(tabs) { tab in
                sidebarButton(tab)
            }
        }
        .padding(.horizontal, 8)
    }

    private func sidebarButton(_ tab: AppTab) -> some View {
        let confirmationNavigation = runner.activityConfirmationSnapshot.navigationState
        let badgeText = tab == .confirmations ? confirmationNavigation.badgeText : nil
        let badgeColor = confirmationNavigation.badgeTone == .warning
            ? Color.orange
            : Color(NSColor.selectedContentBackgroundColor)
        return Button {
            windowState.selectedTab = tab
        } label: {
            HStack(spacing: 8) {
                Label(tab.title, systemImage: tab.systemImage)
                    .lineLimit(1)
                Spacer(minLength: 4)
                if let badgeText {
                    Text(badgeText)
                        .font(.caption2.monospacedDigit().weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(badgeColor)
                        .clipShape(Capsule())
                        .accessibilityHidden(true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(windowState.selectedTab == tab ? Color.primary : Color.secondary)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(windowState.selectedTab == tab ? Color.accentColor.opacity(0.14) : Color.clear)
        )
        .accessibilityLabel(
            tab == .confirmations
                ? confirmationNavigation.accessibilityLabel(tabTitle: tab.title)
                : tab.title
        )
        .accessibilityValue(windowState.selectedTab == tab ? "已选中" : "")
    }

    @ViewBuilder
    private var selectedTabView: some View {
        if windowState.selectedTab == .evolution {
            EvolutionView(runner: runner, focusFeedback: windowState.evolutionFocusFeedback)
        } else if windowState.selectedTab == .reflection {
            ReflectionView(runner: runner)
        } else if runner.statusSnapshot.displayStateUnknown {
            // No context tab may expose stale current/trusted/service/count state or
            // mutating controls while the production status read is unknown.
            statusUnknownGate
        } else {
            switch windowState.selectedTab {
            case .today:
                todayTab
            case .reflection:
                ReflectionView(runner: runner)
            case .confirmations:
                AttentionCenterView(model: attention) { windowState.selectedTab = .systemStatus }
            case .fileHistory:
                recordsTab
            case .review:
                combinedReviewTab
            case .memoryReview:
                MemoryLibraryView(runner: runner, attention: attention)
            case .systemStatus:
                overviewTab
            case .privacy:
                privacyAndDataTab
            case .settings:
                settingsContainer
            case .evolution:
                EvolutionView(runner: runner, focusFeedback: windowState.evolutionFocusFeedback)
            }
        }
    }

    private var appHeader: some View {
        HStack(spacing: 14) {
            LogoMark(size: 38)
            VStack(alignment: .leading, spacing: 4) {
                Text("AGI")
                    .font(.title3.weight(.semibold))
                Text("个人 AI 工作系统")
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
    }

    private var recordingPresentation: RecordingControlPresentation {
        RecordingControlPresentation.resolve(
            status: runner.statusSnapshot.status,
            launchAgentLoaded: runner.statusSnapshot.launchAgentLoaded,
            launchAgentRunning: runner.statusSnapshot.launchAgentRunning,
            pauseLabel: runner.statusSnapshot.pauseLabel,
            statusExplanation: runner.statusSnapshot.statusExplanation,
            recordingPaused: runner.statusSnapshot.recordingPaused
        )
    }

    private var statusUnknownGate: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                appHeader
                SettingsSection(title: "状态未知") {
                    VStack(alignment: .leading, spacing: 10) {
                        Label("状态未知", systemImage: "clock.arrow.circlepath")
                            .font(.title3.weight(.semibold))
                        Text(runner.statusSnapshot.observationTimeLabel)
                            .font(.callout.monospacedDigit())
                            .foregroundStyle(.secondary)
                        if runner.isRefreshingStatus {
                            ProgressView("正在读取完整状态，文件变化较多时可能需要几十秒…")
                                .controlSize(.small)
                        }
                        Text(runner.statusSnapshot.reasonCode == "database_busy"
                             ? "后台正在更新记录，完整状态暂不可读，稍后会自动重试。"
                             : runner.statusSnapshot.reasonCode == "service_status_unavailable"
                             ? "暂时无法核对后台服务状态，稍后会自动重试。"
                             : "完整状态尚未读取成功；旧成功快照不会继续显示。")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                        Button {
                            Task { await runner.refreshStatus() }
                        } label: {
                            Label("刷新状态", systemImage: "arrow.clockwise")
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(runner.isRefreshingStatus)
                    }
                    .padding(14)
                }
            }
            .padding(20)
        }
    }

    private var attentionSheetBinding: Binding<AttentionPresentation?> {
        let owner = attention.presentation
        return Binding(get: { attention.presentation?.mode == .sheet ? attention.presentation : nil }, set: { next in
            if next == nil, let owner, owner.mode == .sheet { attention.close(presentationID: owner.id) }
        })
    }

    private var todayRefreshBusy: Bool {
        todayRefreshRequested || runner.isRefreshingStatus || attention.loading
            || feedback.busy || evolution.busy
    }

    private var todayTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                HStack {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("今天").font(.largeTitle.weight(.semibold))
                        Text(Date.now, format: .dateTime.month(.wide).day().weekday(.wide))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        guard !todayRefreshBusy else { return }
                        todayRefreshRequested = true
                        Task {
                            defer { todayRefreshRequested = false }
                            async let statusRead: Void = runner.refreshStatus()
                            async let attentionRead: Void = attention.refresh()
                            async let feedbackRead: Void = feedback.refresh(runner)
                            async let evolutionRead = evolution.perform(.status, runner: runner)
                            _ = await (statusRead, attentionRead, feedbackRead, evolutionRead)
                        }
                    } label: {
                        if todayRefreshBusy {
                            ProgressView("正在刷新…").controlSize(.small)
                        } else {
                            Image(systemName: "arrow.clockwise")
                        }
                    }
                    .help("重新读取今天的状态")
                    .accessibilityLabel(todayRefreshBusy ? "正在刷新今天的状态" : "刷新今天的状态")
                    .accessibilityIdentifier("today-refresh")
                    .disabled(todayRefreshBusy)
                }
                todayRecordingSection
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("需要你决定").font(.title3.weight(.semibold))
                        Spacer()
                        Text(attention.snapshot.countLabel).foregroundStyle(.secondary)
                    }
                    if attention.snapshot.decisionCount == 0 {
                        QuietEmptyState(title: "目前无需你做决定", detail: "有需要判断的具体事项时，会在这里说明背景和影响。", symbol: "checkmark.circle")
                    } else if attention.snapshot.decisionCount == nil {
                        QuietEmptyState(title: "事项暂不可读取", detail: "正在核对数据。此时不会将未知数量显示成零。", symbol: "clock.arrow.circlepath")
                    } else {
                        Button { windowState.selectedTab = .confirmations } label: {
                            HStack(spacing: 16) {
                                Image(systemName: "questionmark.bubble").font(.title2).foregroundStyle(.secondary)
                                VStack(alignment: .leading, spacing: 7) {
                                    Text("背景与选项，已经整理好").font(.headline).foregroundStyle(.primary)
                                    Text(attention.snapshot.summaryLabel)
                                        .font(.callout).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text("查看分组").foregroundStyle(Color.accentColor)
                                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
                            }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
                                .background(Color(NSColor.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
                                .contentShape(Rectangle())
                        }.buttonStyle(.plain)
                    }
                }
                ForEach(attention.snapshot.systems) { issue in
                    AttentionIssueRow(issue: issue) { Task { await attention.select(issue) } }
                }
                ForEach(runner.statusSnapshot.attentionItems.filter { visibleOperationalNotice($0) }) { item in
                    StatusNotice(systemImage: item.systemImage, title: item.title, message: item.displayMessage)
                }
                VStack(alignment: .leading, spacing: 12) {
                    Text("已确认的近期变化").font(.title3.weight(.semibold))
                    todayDocumentChangesSection
                    confirmedActivityQuietSection
                }
                VStack(alignment: .leading, spacing: 12) {
                    Text("AGI 的工作状态").font(.title3.weight(.semibold))
                    ReflectionTodayCard(runner: runner) { windowState.selectedTab = .reflection }
                    FeedbackTodayCard(runner: runner) { windowState.evolutionFocusFeedback = true; windowState.selectedTab = .evolution }
                    EvolutionTodayCard(runner: runner) { windowState.evolutionFocusFeedback = false; windowState.selectedTab = .evolution }
                }
            }.padding(28).frame(maxWidth: 1000, alignment: .leading).frame(maxWidth: .infinity)
        }
    }

    private func visibleOperationalNotice(_ item: AttentionItem) -> Bool {
        if item.code == "confirmation_evidence_unavailable" || item.title.contains("其他本机服务正在使用调试端口") { return false }
        // Runtime issues use the fresh shared projection, including recovery.
        // The older full status can otherwise retain a startup warning after
        // its lightweight recording state has already recovered.
        let shared = ["service_stopped", "service_status_unavailable", "session_state_unknown", "private_purge_pending", "retention_cleanup_failed", "capture_stale", "stale_capture", "last_error", "launch_agent_not_running", "launch_agent_not_loaded"].contains(item.code)
            || item.code.hasPrefix("daemon_heartbeat_") || item.code.hasPrefix("document_worker_heartbeat_")
        return !shared || attention.snapshot.status == "unknown"
    }

    private var recordsTab: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("记录").font(.largeTitle.weight(.semibold))
                Spacer()
                Picker("记录类型", selection: $recordsMode) {
                    Text("文件历史").tag(0)
                    Text("回顾").tag(1)
                }.pickerStyle(.segmented).frame(width: 230)
            }.padding(.horizontal, 24).padding(.top, 24)
            if recordsMode == 0 { fileHistoryTab.padding(.horizontal, 24) }
            else { combinedReviewTab.padding(.horizontal, 24) }
        }.onChange(of: recordsMode) { _, mode in if mode == 1 { refreshReviewContent() } }
    }

    private var settingsContainer: some View {
        VStack(alignment: .leading, spacing: 16) {
            Picker("设置分类", selection: $settingsMode) {
                Text("常规").tag(0)
                Text("隐私与数据").tag(1)
            }.pickerStyle(.segmented).frame(width: 260).padding(24)
            if settingsMode == 0 {
                HStack {
                    Text("外观").font(.headline)
                    Spacer()
                    Picker("外观", selection: $appearance) {
                        Text("跟随系统").tag("system")
                        Text("浅色").tag("light")
                        Text("深色").tag("dark")
                    }.pickerStyle(.segmented).frame(width: 280)
                }.padding(.horizontal, 24)
                settingsTab.padding(.horizontal, 24)
            }
            else { privacyAndDataTab.padding(.horizontal, 24) }
        }.onChange(of: settingsMode) { _, mode in
            if mode == 1 { Task { await runner.refreshPrivacyPolicyStatus(); await runner.refreshDocumentChangesStatus() } }
        }
    }

    private var attentionResourcesSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("历史资料").font(.title3.weight(.semibold))
            Text("资料缺口与旧问题保留在这里，不会成为首页的逐条待办。")
                .font(.callout).foregroundStyle(.secondary)
            ForEach(attention.snapshot.resources) { issue in
                AttentionIssueRow(issue: issue) { Task { await attention.select(issue) } }
            }
            ForEach(attention.snapshot.systems) { issue in
                AttentionIssueRow(issue: issue) { Task { await attention.select(issue) } }
            }
        }
    }

    private var todayConfirmationPrompt: some View {
        let navigation = runner.activityConfirmationSnapshot.navigationState
        return SettingsSection(title: "需要你确认") {
            Button {
                windowState.selectedTab = .confirmations
            } label: {
                SettingsRow(
                    systemImage: "questionmark.bubble",
                    title: navigation.todayPromptTitle,
                    subtitle: navigation.canDisplayContent
                        ? "确认后会修正对应日期的记录，并帮助系统下次更准确地区分真实活动、旧任务和参考资料。"
                        : "当前只保留可验证的导航状态；内容和操作已安全关闭，请刷新后再处理。"
                ) {
                    Label("去确认", systemImage: "chevron.right")
                        .foregroundStyle(Color.accentColor)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(navigation.todayPromptTitle)，前往待确认")
        }
    }

    private var recordingColor: Color {
        runner.statusSnapshot.recordingControlAvailable && !runner.statusSnapshot.recordingPaused
            && runner.statusSnapshot.status == "paused" && !runner.statusSnapshot.hasKnownCaptureError
            && !runner.statusSnapshot.screenCaptureEnabled && runner.statusSnapshot.launchAgentRunning
            ? .secondary : runner.statusSnapshot.statusKind.color
    }

    private var todayRecordingSection: some View {
        SettingsSection(title: "记录状态") {
            HStack(spacing: 14) {
                Image(systemName: recordingPresentation.systemImage)
                    .font(.system(size: 24, weight: .semibold))
                    .foregroundStyle(recordingColor)
                    .frame(width: 38, height: 38)
                    .background(recordingColor.opacity(0.10))
                    .clipShape(Circle())

                VStack(alignment: .leading, spacing: 4) {
                    Text(recordingPresentation.headline)
                        .font(.title3.weight(.semibold))
                    Text(recordingPresentation.detail)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }

                Spacer(minLength: 16)
                recordingPrimaryControl
                recordingMoreMenu
            }
            .padding(14)
        }
    }

    @ViewBuilder
    private var recordingPrimaryControl: some View {
        if runner.statusSnapshot.recordingControlAvailable {
            switch recordingPresentation.action {
            case .pause:
                Menu {
                    Button("暂停 15 分钟") {
                        Task { await runner.pause(minutes: 15) }
                    }
                    Button("暂停到下次锁屏") {
                        Task { await runner.pauseUntilLock() }
                    }
                    Button("今天不记录") {
                        Task { await runner.pauseToday() }
                    }
                } label: {
                    Label(recordingPresentation.title, systemImage: recordingPresentation.systemImage)
                }
                .menuStyle(.borderlessButton)
                .buttonStyle(.borderedProminent)
                .disabled(!runner.statusSnapshot.recordingControlAvailable)
            case .resume:
                Button {
                    Task { await runner.resume() }
                } label: {
                    Label(recordingPresentation.title, systemImage: recordingPresentation.systemImage)
                }
                .buttonStyle(.borderedProminent)
            case .start:
                Button {
                    Task { await runner.startRecordingService() }
                } label: {
                    Label(recordingPresentation.title, systemImage: recordingPresentation.systemImage)
                }
                .buttonStyle(.borderedProminent)
            case .none:
                EmptyView()
            }
        }
    }

    private var recordingMoreMenu: some View {
        Menu {
            if runner.statusSnapshot.captureActionsAvailable {
                Button("立即采集") {
                    Task { await runner.captureNow() }
                }
                .disabled(runner.statusSnapshot.captureInteractionBlocked || runner.statusSnapshot.status == "paused")
                if runner.statusSnapshot.launchAgentRunning {
                    Button("停止自动记录") {
                        Task { await runner.stopRecordingService() }
                    }
                }
                Divider()
            }
            Button("刷新状态") {
                Task { await runner.refreshStatus() }
            }
        } label: {
            Image(systemName: "ellipsis.circle")
                .font(.title3)
        }
        .menuStyle(.borderlessButton)
        .accessibilityLabel("更多记录操作")
    }

    private var todayHumanSummarySection: some View {
        let confirmed = runner.statusSnapshot.confirmedActivity
        let confirmationNavigation = runner.activityConfirmationSnapshot.navigationState
        return SettingsSection(title: "今天") {
            HStack(spacing: 0) {
                quietMetric(
                    title: "已确认",
                    value: confirmed.isAvailable ? "\(confirmed.confirmedCount) 条" : "暂不可读",
                    detail: confirmed.isAvailable
                        ? confirmationNavigation.todayMetricDetail
                        : "不会回退到未确认活动"
                )
                Divider().frame(height: 54)
                quietMetric(
                    title: "累计时长",
                    value: confirmed.isAvailable ? confirmed.totalDurationLabel : "暂不可读",
                    detail: confirmed.isAvailable
                        ? "\(confirmed.confirmedCount) 个已确认时段"
                        : "confirmed_activity 契约不可用"
                )
                Divider().frame(height: 54)
                quietMetric(
                    title: "隐私保护",
                    value: "\(runner.statusSnapshot.privacyEvents) 次",
                    detail: runner.statusSnapshot.gaps > 0 ? "另有 \(runner.statusSnapshot.gaps) 个缺口" : "没有未解释缺口"
                )
            }
            .padding(.vertical, 8)
        }
    }

    private func quietMetric(title: String, value: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.headline)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
            Text(detail)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14)
    }

    private var confirmedActivityQuietSection: some View {
        let confirmed = runner.statusSnapshot.confirmedActivity
        return SettingsSection(title: "已确认主要活动") {
            if !confirmed.isAvailable {
                SettingsRow(
                    systemImage: "exclamationmark.shield",
                    title: "确认事实暂不可读",
                    subtitle: "核心端没有返回安全的 confirmed_activity 契约；这里不会用未确认活动补位。"
                ) {
                    StatusPill(text: "已停止展示", kind: .warning)
                }
            } else if let primary = confirmed.primaryActivity {
                SettingsRow(
                    systemImage: "checkmark.seal",
                    title: primary.summary,
                    subtitle: primary.detailLine
                ) {
                    Button {
                        windowState.selectedTab = .review
                    } label: {
                        Label("查看回顾", systemImage: "chevron.right")
                    }
                    .buttonStyle(.plain)
                }
            } else {
                SettingsRow(
                    systemImage: "checkmark.circle",
                    title: "今天还没有已确认的主要活动",
                    subtitle: "这里只展示已经确认的事实；需要判断的事项集中在「需要你决定」。"
                ) {
                    EmptyView()
                }
            }
        }
    }

    @ViewBuilder
    private var todayDocumentChangesSection: some View {
        let changes = runner.statusSnapshot.documentChanges
        if changes.isBlocked {
            StatusNotice(
                systemImage: "lock.shield",
                title: "文档修改已停止展示",
                message: changes.limitations.first ?? "文档变更接口没有满足安全边界。"
            )
        } else if changes.isAvailable {
            SettingsSection(
                title: "今天已确认变化的文档",
                footer: changes.coverage.isComplete ? nil : changes.coverageExplanation
            ) {
                SettingsRow(
                    systemImage: "doc.badge.clock",
                    title: "已确认变化 \(changes.totals.distinctFiles) 个文件",
                    subtitle: documentCoverageSummary(changes.coverage)
                ) {
                    StatusPill(
                        text: changes.coverageLabel,
                        kind: changes.coverage.eventGapCount > 0 ? .warning : .neutral
                    )
                }
                ForEach(DocumentChangesPresentation.todayItems(from: changes)) { item in
                    Divider().padding(.leading, 52)
                    SettingsRow(
                        systemImage: documentChangeSystemImage(item.changeKind),
                        title: item.displayName,
                        subtitle: item.safeSummary
                    ) {
                        Text(documentChangeKindLabel(item.changeKind))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                let remaining = max(0, changes.totals.distinctFiles - min(3, changes.items.count))
                if remaining > 0 {
                    Divider().padding(.leading, 52)
                    Button("到回顾查看其余 \(remaining) 个文件（含仅计数项）") {
                        windowState.selectedTab = .review
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Color.accentColor)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                }
            }
        } else {
            SettingsSection(title: "今天已确认变化的文档") {
                Text("今天的文档变化暂不可读；这不等于今天没有文件变化。")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(14)
            }
        }
    }

    private var combinedReviewTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 10) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("回顾")
                            .font(.title2.bold())
                        Text("按时间浏览、搜索和导出自己的清洗后活动。")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    reviewExportsMenu
                }

                DisclosureGroup("会话与应用排行") {
                    VStack(alignment: .leading, spacing: 14) {
                        currentActivitySessionDetailSection
                        todayActivitySessionsSection
                        activitySessionBoundarySection
                    }.padding(.top, 12)
                }
                rangeSection
                reviewSearchSection
                if !searchQuery.isEmpty || !runner.searchText.isEmpty {
                    ResultPanel(
                        title: "搜索结果 - \(rangeLabel)",
                        text: runner.searchText,
                        emptyText: "没有找到匹配的清洗后记录。"
                    )
                }
                reviewAtAGlanceSection
                reviewDocumentChangesSection
                ResultPanel(
                    title: "活动摘要 - \(rangeLabel)",
                    text: runner.dailyReviewText,
                    emptyText: "正在整理这个时间范围的活动摘要。"
                )
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private var reviewDocumentChangesSection: some View {
        let changes = runner.reviewDocumentChanges
        if changes.isBlocked {
            StatusNotice(
                systemImage: "lock.shield",
                title: "文档修改已停止展示",
                message: changes.limitations.first ?? "文档变更接口没有满足安全边界。"
            )
        } else if changes.isAvailable {
            SettingsSection(
                title: "文档修改 - \(rangeLabel)",
                footer: changes.coverage.isComplete ? nil : "该时间范围的覆盖不完整；不能把未发现修改解释为没有修改。"
            ) {
                SettingsRow(
                    systemImage: "doc.text.magnifyingglass",
                    title: changes.source == "document_owner_local_observation"
                        ? "本机观察到 \(changes.totals.distinctFiles) 个文件变化"
                        : "确认变化 \(changes.totals.distinctFiles) 个文件",
                    subtitle: "新建 \(changes.totals.created) · 修改 \(changes.totals.modified) · 重命名 \(changes.totals.renamed) · 删除 \(changes.totals.deleted)"
                ) {
                    StatusPill(
                        text: changes.source == "document_owner_local_observation" ? "尚未核验" : "文件追踪",
                        kind: changes.source == "document_owner_local_observation" || !changes.coverage.isComplete ? .warning : .ok
                    )
                }
                if changes.items.isEmpty {
                    Divider().padding(.leading, 52)
                    Text(reviewDocumentChangesEmptyMessage(changes))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(14)
                } else {
                    ForEach(changes.items) { item in
                        Divider().padding(.leading, 52)
                        SettingsRow(
                            systemImage: documentChangeSystemImage(item.changeKind),
                            title: item.displayName,
                            subtitle: documentReviewItemSubtitle(item)
                        ) {
                            VStack(alignment: .trailing, spacing: 3) {
                                Text(documentChangeKindLabel(item.changeKind))
                                if item.versionCount > 1 {
                                    Text("\(item.versionCount) 个版本")
                                }
                            }
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        }
                    }
                    let hiddenCount = max(0, changes.totals.distinctFiles - changes.items.count)
                    if hiddenCount > 0 {
                        Divider().padding(.leading, 52)
                        Text("另有 \(hiddenCount) 个文件仅计数或未展开；受保护文件不会逐项显示。")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .padding(14)
                    }
                }
            }
        } else {
            SettingsSection(title: "文档修改 - \(rangeLabel)") {
                Text("该时间范围的文档变化暂不可读；这不等于没有文件变化。")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(14)
            }
        }
    }

    private var reviewSearchSection: some View {
        SettingsSection(title: "查找记录") {
            HStack(spacing: 10) {
                TextField("输入关键词", text: $query)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit {
                        runner.search(query: searchQuery, from: rangeFrom, to: rangeTo)
                    }
                Button {
                    runner.search(query: searchQuery, from: rangeFrom, to: rangeTo)
                } label: {
                    Label("搜索", systemImage: "magnifyingglass")
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.return, modifiers: [])
                .disabled(searchQuery.isEmpty)
            }
            .padding(12)
        }
    }

    private var reviewAtAGlanceSection: some View {
        let snapshot = runner.aiReviewSnapshot
        return SettingsSection(title: "一眼回顾") {
            SettingsRow(
                systemImage: "sparkles.rectangle.stack",
                title: snapshot.projects.isEmpty ? "活动正在整理" : "识别到 \(snapshot.projects.count) 个活动主题",
                subtitle: snapshot.message
            ) {
                Button {
                    refreshReviewContent()
                } label: {
                    Label("更新", systemImage: "arrow.clockwise")
                }
            }
        }
    }

    private var reviewExportsMenu: some View {
        Menu {
            Button("完整日志") {
                Task { await runner.exportTimelineMarkdown(from: rangeFrom, to: rangeTo, label: rangeLabel, kind: .full) }
            }
            Button("活动摘要") {
                Task { await runner.exportTimelineMarkdown(from: rangeFrom, to: rangeTo, label: rangeLabel, kind: .learning) }
            }
            Button("AI 接手包") {
                Task { await runner.exportAIHandoff(from: rangeFrom, to: rangeTo, label: rangeLabel) }
            }
            Button("质量报告") {
                Task { await runner.exportQualityMarkdown(from: rangeFrom, to: rangeTo, label: rangeLabel) }
            }
            Button("HA 安全包") {
                Task { await runner.exportFullMirror(from: rangeFrom, to: rangeTo, label: rangeLabel) }
            }
        } label: {
            Label("更多", systemImage: "ellipsis.circle")
        }
        .menuStyle(.borderlessButton)
    }

    private var settingsTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("设置")
                        .font(.title2.bold())
                    Text("常用管理已拆到系统状态和隐私与数据；这里保留本地智能与低频工具。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }

                activityConfirmationModeSection
                advancedSettingsSection
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var memoryReviewTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("记忆库")
                        .font(.title2.bold())
                    Text("决定哪些长期记忆还能被 AI 调用。裁决只改变可召回性，不删除任何记忆。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }

                memoryReviewStatusSection
                memoryReviewQueueSection
                if !runner.memoryReviewRevealedID.isEmpty {
                    memoryReviewRevealSection
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task {
            await runner.refreshMemoryReview()
        }
    }

    private var memoryReviewStatusSection: some View {
        let status = runner.memoryReviewStatus
        return SettingsSection(
            title: "概览",
            footer: status.supersedeSupported ? nil : "「替换为新记忆」需要写入记忆库，本页面不提供；其余五种裁决都可用。"
        ) {
            if status.retrievalBlocked {
                SettingsRow(
                    systemImage: "exclamationmark.triangle.fill",
                    title: "候选清单不可读",
                    subtitle: status.errorMessage.isEmpty
                        ? "AI 已停止调用共享长期记忆，直到清单恢复。"
                        : status.errorMessage
                ) {
                    EmptyView()
                }
            } else {
                SettingsRow(
                    systemImage: "brain",
                    title: "待审 \(status.pending) 条 / 共 \(status.total) 条",
                    subtitle: memoryReviewDecisionSummary(status.byDecision)
                ) {
                    Button("重新载入") {
                        Task { await runner.refreshMemoryReview(force: true) }
                    }
                    .disabled(runner.memoryReviewLoading)
                }
            }
            Divider()
            SettingsRow(
                systemImage: "externaldrive.connected.to.line.below",
                title: "数据来源",
                subtitle: memoryReviewSourceSummary(status)
            ) {
                EmptyView()
            }
            if !runner.memoryReviewMessage.isEmpty {
                Divider()
                SettingsRow(
                    systemImage: "clock.arrow.circlepath",
                    title: "最近一次操作",
                    subtitle: runner.memoryReviewMessage
                ) {
                    EmptyView()
                }
            }
        }
    }

    private func memoryReviewDecisionSummary(_ counts: [String: Int]) -> String {
        let order = [
            "pending", "keep_active", "review_later",
            "mark_legacy", "mark_expired", "quarantine_wrong_subject",
        ]
        let labels = [
            "pending": "未处理",
            "keep_active": "继续有效",
            "review_later": "稍后复核",
            "mark_legacy": "标为历史",
            "mark_expired": "标为过期",
            "quarantine_wrong_subject": "已隔离",
        ]
        let parts = order.compactMap { key -> String? in
            guard let value = counts[key], value > 0 else { return nil }
            return "\(labels[key] ?? key) \(value)"
        }
        return parts.isEmpty ? "队列为空。" : parts.joined(separator: " · ")
    }

    private func memoryReviewSourceSummary(_ status: MemoryReviewStatus) -> String {
        var parts: [String] = []
        parts.append(status.queuePresent ? "候选清单已就绪" : "候选清单缺失")
        parts.append(status.ledgerPresent ? "裁决台账已就绪" : "裁决台账尚未建立")
        parts.append(status.storePresent ? "记忆库已挂载" : "记忆库未挂载（无法查看正文）")
        return parts.joined(separator: " · ")
    }

    private var memoryReviewVisibleCandidates: [MemoryReviewCandidate] {
        runner.memoryReviewList.items.filter { memoryReviewShowDecided || $0.reviewState == "pending" }
    }

    private var memoryReviewQueueSection: some View {
        let candidates = memoryReviewVisibleCandidates
        return SettingsSection(title: "候选记忆") {
            SettingsRow(
                systemImage: "line.3.horizontal.decrease.circle",
                title: memoryReviewShowDecided ? "显示全部候选" : "只显示未处理",
                subtitle: "当前列出 \(candidates.count) 条；已处理的记忆保留在清单里，方便你改主意。"
            ) {
                Toggle("显示已处理", isOn: $memoryReviewShowDecided)
                    .toggleStyle(.switch)
                    .labelsHidden()
            }
            if candidates.isEmpty {
                Divider()
                SettingsRow(
                    systemImage: "checkmark.seal",
                    title: "没有需要处理的记忆",
                    subtitle: "下次记忆体检扫出新候选时会出现在这里。"
                ) {
                    EmptyView()
                }
            }
            ForEach(candidates) { candidate in
                Divider()
                memoryReviewRow(candidate)
            }
            if !memoryReviewDrafts.isEmpty {
                Divider()
                memoryReviewSubmitRow
            }
        }
    }

    private func memoryReviewRow(_ candidate: MemoryReviewCandidate) -> some View {
        let currentLabel = candidate.reviewState == "pending"
            ? "未处理"
            : "\(candidate.decisionTitle)\(candidate.retrievable ? "（AI 可用）" : "（AI 不可用）")"
        return SettingsRow(
            systemImage: candidate.retrievable ? "checkmark.circle" : "circle.slash",
            title: "记忆 · \(candidate.fingerprint)",
            subtitle: "\(currentLabel)　原因：\(candidate.reasonCodes.prefix(2).joined(separator: "、"))"
        ) {
            HStack(spacing: 8) {
                Button("看正文") {
                    Task { await runner.revealMemory(candidate.memoryID) }
                }
                .disabled(runner.memoryReviewLoading || !runner.memoryReviewStatus.storePresent)
                Picker("", selection: memoryReviewBinding(for: candidate.memoryID)) {
                    Text("不改动").tag("")
                    Text("继续有效").tag("keep_active")
                    Text("稍后复核").tag("review_later")
                    Text("标为历史").tag("mark_legacy")
                    Text("标为过期").tag("mark_expired")
                    Text("隔离").tag("quarantine_wrong_subject")
                }
                .labelsHidden()
                .frame(width: 130)
            }
        }
    }

    private func memoryReviewBinding(for memoryID: String) -> Binding<String> {
        Binding(
            get: { memoryReviewDrafts[memoryID] ?? "" },
            set: { newValue in
                if newValue.isEmpty {
                    memoryReviewDrafts.removeValue(forKey: memoryID)
                } else {
                    memoryReviewDrafts[memoryID] = newValue
                }
                // Any edit invalidates a digest the reviewer already previewed.
                runner.memoryReviewPreview = MemoryReviewPreview()
            }
        )
    }

    private var memoryReviewSubmitRow: some View {
        let preview = runner.memoryReviewPreview
        return SettingsRow(
            systemImage: "tray.and.arrow.down",
            title: "已选择 \(memoryReviewDrafts.count) 条",
            subtitle: preview.isReady
                ? "预览已生成：\(memoryReviewDecisionSummary(preview.byAction))。确认后写入。"
                : "先生成预览，看清楚会改什么，再确认写入。"
        ) {
            HStack(spacing: 8) {
                Button("清空选择") {
                    memoryReviewDrafts.removeAll()
                    runner.memoryReviewPreview = MemoryReviewPreview()
                }
                .disabled(runner.memoryReviewLoading)
                Button("生成预览") {
                    Task { await runner.previewMemoryReviewDecisions(memoryReviewDrafts) }
                }
                .disabled(runner.memoryReviewLoading)
                Button("确认写入") {
                    Task {
                        if await runner.applyMemoryReviewDecisions(memoryReviewDrafts) {
                            memoryReviewDrafts.removeAll()
                        }
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(runner.memoryReviewLoading || !preview.isReady)
            }
        }
    }

    private var memoryReviewRevealSection: some View {
        SettingsSection(
            title: "记忆正文",
            footer: "正文只在这里临时显示，关闭后不保留，也不会写进任何报告。"
        ) {
            SettingsRow(
                systemImage: "doc.text.magnifyingglass",
                title: "记忆 · \(runner.memoryReviewRevealedID.prefix(8))",
                subtitle: runner.memoryReviewRevealedText.isEmpty ? "读取中……" : runner.memoryReviewRevealedText
            ) {
                Button("关闭") {
                    runner.clearRevealedMemory()
                }
            }
        }
    }

    private var privacyAndDataTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("隐私与数据")
                        .font(.title2.bold())
                    Text("管理采集边界、授权目录、权限、隐私规则和本机删除。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }

                privacyEssentialsSection
                documentTrackingSettingsSection
                SettingsSection(title: "权限") {
                    permissionRow
                }
                privacyPolicySection
                deletionSection
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var documentTrackingSettingsSection: some View {
        let changes = runner.documentTrackingSnapshot
        let statusTitle: String
        let statusSubtitle: String
        let statusIcon: String
        if changes.isBlocked {
            statusTitle = "安全边界异常"
            statusSubtitle = changes.limitations.first ?? "安全边界异常，已停止展示和操作。"
            statusIcon = "lock.shield"
        } else if !changes.isAvailable {
            statusTitle = "状态不可读"
            statusSubtitle = "文档追踪状态暂不可读，已停止展示和操作。"
            statusIcon = "questionmark.folder"
        } else {
            statusTitle = changes.coverage.enabled ? "已启用" : "未启用"
            statusSubtitle = documentCoverageSummary(changes.coverage)
            statusIcon = changes.coverage.enabled ? "doc.text.fill" : "doc.text"
        }
        return SettingsSection(
            title: "文档修改追踪",
            footer: "只监听你授权的目录。真实路径仅保存在本机 0600 权限的运行配置中，不写入 SQLite、日报或导出；界面只显示脱敏标签。"
        ) {
            SettingsRow(
                systemImage: statusIcon,
                title: statusTitle,
                subtitle: statusSubtitle
            ) {
                Toggle(
                    "",
                    isOn: Binding(
                        get: { changes.coverage.enabled },
                        set: { value in Task { await runner.configureDocumentChanges(enabled: value) } }
                    )
                )
                .labelsHidden()
                .disabled(!changes.isAvailable || runner.documentChangesLoading)
            }

            if changes.isAvailable {
                Divider().padding(.leading, 52)
                HStack(spacing: 10) {
                    Button {
                        Task { await runner.addDocumentWatchRoot() }
                    } label: {
                        Label("添加目录", systemImage: "folder.badge.plus")
                    }
                    Button {
                        Task { await runner.resetDocumentChangesBaseline() }
                    } label: {
                        Label("重建安全基线", systemImage: "arrow.triangle.2.circlepath")
                    }
                    Spacer()
                    Button {
                        Task { await runner.refreshDocumentChangesStatus() }
                    } label: {
                        Label("刷新", systemImage: "arrow.clockwise")
                    }
                }
                .buttonStyle(.bordered)
                .disabled(runner.documentChangesLoading)
                .padding(14)

                ForEach(changes.roots) { root in
                    Divider().padding(.leading, 52)
                    SettingsRow(
                        systemImage: "folder",
                        title: root.displayLabel,
                        subtitle: root.displayPathRedacted.isEmpty ? "路径已脱敏" : root.displayPathRedacted
                    ) {
                        HStack(spacing: 8) {
                            StatusPill(text: root.enabled ? "监听中" : "已停用", kind: root.enabled ? .ok : .neutral)
                            Button {
                                Task { await runner.suspendDocumentWatchRoot(rootRef: root.rootRef) }
                            } label: {
                                Text("停止追踪")
                            }
                            .buttonStyle(.plain)
                            .disabled(runner.documentChangesLoading)
                            .accessibilityLabel("停止追踪 \(root.displayLabel)，保留历史")
                        }
                    }
                }
            } else if changes.isBlocked {
                Divider().padding(.leading, 52)
                VStack(alignment: .leading, spacing: 8) {
                    Button {
                        Task { await runner.reauthorizeDocumentWatchRoot() }
                    } label: {
                        Label("重新授权目录", systemImage: "folder.badge.plus")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(runner.documentChangesLoading)
                    Text("将撤销所有无法核验的旧目录，只保留你这次明确选择的目录，并重新建立安全基线。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(14)
            } else {
                Divider().padding(.leading, 52)
                Text("文档追踪状态暂不可读，已停用相关操作。")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(14)
            }

            if !runner.documentChangesMessage.isEmpty {
                Divider().padding(.leading, 52)
                Text(runner.documentChangesMessage)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(14)
            }
        }
    }

    private func documentCoverageSummary(_ coverage: DocumentChangeCoverage) -> String {
        let baseline: String
        switch coverage.baselineStatus {
        case "ready", "complete": baseline = "基线完成"
        case "building", "in_progress": baseline = "正在建立安全基线"
        case "partial", "baseline_gap": baseline = "基线不完整"
        default: baseline = "基线待确认"
        }
        let gaps = coverage.eventGapCount > 0 ? "，\(coverage.eventGapCount) 个覆盖缺口" : "，没有未解释缺口"
        let lastEvent = coverage.lastEventAt.isEmpty ? "，尚无确认事件" : "，最后事件 \(Self.localTimestamp(coverage.lastEventAt))"
        let pending = coverage.pendingEventCount.map { $0 > 0 ? "，\($0) 条新事件正在处理" : "" } ?? ""
        return "\(coverage.rootCount) 个目录，\(baseline)\(pending)\(gaps)\(lastEvent)"
    }

    private func documentChangeSystemImage(_ kind: String) -> String {
        switch kind {
        case "created": return "doc.badge.plus"
        case "renamed": return "pencil.line"
        case "deleted": return "doc.badge.minus"
        default: return "doc.badge.clock"
        }
    }

    private func documentChangeKindLabel(_ kind: String) -> String {
        switch kind {
        case "created": return "新建"
        case "modified": return "修改"
        case "renamed": return "重命名"
        case "deleted": return "删除"
        default: return "变化"
        }
    }

    private func documentReviewItemSubtitle(_ item: DocumentChangeItem) -> String {
        let project = item.projectLabel.isEmpty ? "" : "\(item.projectLabel) · "
        return "\(project)\(item.safeSummary)"
    }

    private func reviewDocumentChangesEmptyMessage(_ changes: DocumentChangesSnapshot) -> String {
        if !changes.coverage.isComplete {
            return "当前没有可安全展示的文件项，且追踪覆盖不完整。"
        }
        if changes.totals.protected > 0 {
            return "检测到 \(changes.totals.protected) 个受保护文件变化；只保留计数，不显示文件名、项目或内容摘要。"
        }
        return changes.source == "document_owner_local_observation"
            ? "该时间范围未观察到文件变化；观察记录尚未核验。"
            : "该时间范围没有确认的文件变化。"
    }

    private var privacyEssentialsSection: some View {
        SettingsSection(title: "隐私与数据") {
            SettingsRow(
                systemImage: "rectangle.on.rectangle",
                title: "只在本机记录",
                subtitle: "记录已授权屏幕的 keyframe，并只保留清洗后的活动摘要。"
            ) {
                Text("本机")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.secondary)
            }
            Divider().padding(.leading, 52)
            SettingsRow(
                systemImage: "hand.raised",
                title: "不会记录",
                subtitle: "音频、键盘输入、剪贴板、聊天原文、长期原始截图或短视频。"
            ) {
                EmptyView()
            }
            Divider().padding(.leading, 52)
            SettingsRow(
                systemImage: "calendar",
                title: "只保留今天和昨天",
                subtitle: "更早的清洗后活动会自动过期；长期记忆写入保持关闭。"
            ) {
                Button("打开数据") { runner.openDataFolder() }
            }
        }
    }

    private var deletionSection: some View {
        SettingsSection(title: "删除本地记录", footer: "删除前会要求 Touch ID 或本机密码确认，操作不可撤销。") {
            VStack(alignment: .leading, spacing: 12) {
                Picker("范围", selection: $deletePreset) {
                    ForEach(DeletePreset.allCases) { preset in
                        Text(preset.title).tag(preset)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: deletePreset) { _, _ in
                    deleteArmed = false
                }
                Toggle("我知道此操作不可撤销", isOn: $deleteArmed)
                    .toggleStyle(.checkbox)
                HStack {
                    Text(deleteRangeSummary)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button(role: .destructive) {
                        let range = deleteRange(for: deletePreset)
                        Task {
                            await runner.deleteRange(from: range.from, to: range.to, reason: "user_deleted_from_app")
                        }
                    } label: {
                        Label("确认并删除", systemImage: "trash")
                    }
                    .disabled(!deleteArmed)
                }
                if !runner.deleteText.isEmpty {
                    ResultPanel(title: "删除结果", text: runner.deleteText, emptyText: "")
                }
            }
            .padding(14)
        }
    }

    private var activityConfirmationModeSection: some View {
        SettingsSection(
            title: "待确认召回强度",
            footer: "只改变候选进入待确认的数量目标，不放宽 Privacy Firewall、raw_access=false 或事实确认门槛。"
        ) {
            VStack(alignment: .leading, spacing: 10) {
                Picker(
                    "待确认召回强度",
                    selection: Binding(
                        get: { runner.activityConfirmationMode },
                        set: { mode in Task { await runner.configureActivityConfirmationMode(mode) } }
                    )
                ) {
                    ForEach(ActivityConfirmationModePreset.allCases) { mode in
                        Text(mode.title).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .disabled(runner.activityConfirmationConfigurationLoading)
                .accessibilityHint("精简约每天 5 到 10 条，标准约每天 10 到 20 条，最大召回会保留更多候选")

                Text(runner.activityConfirmationMode.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                if !runner.activityConfirmationConfigurationMessage.isEmpty {
                    Text(runner.activityConfirmationConfigurationMessage)
                        .font(.caption)
                        .foregroundStyle(
                            ActivityConfirmationMessagePolicy.isWarning(runner.activityConfirmationConfigurationMessage)
                                ? Color.orange
                                : Color.secondary
                        )
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(14)
        }
    }

    private var advancedSettingsSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.16)) {
                    advancedSettingsExpanded.toggle()
                }
            } label: {
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("高级设置")
                            .font(.headline)
                        Text("AI 连接器与只读记忆治理。")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 12)
                    Image(systemName: advancedSettingsExpanded ? "chevron.down" : "chevron.right")
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("高级设置")
            .accessibilityValue(advancedSettingsExpanded ? "已展开" : "已收起")
            .accessibilityHint(advancedSettingsExpanded ? "按下以收起" : "按下以展开")
            .onKeyPress(.return) {
                withAnimation(.easeInOut(duration: 0.16)) {
                    advancedSettingsExpanded.toggle()
                }
                return .handled
            }

            if advancedSettingsExpanded {
                Divider()
                    .padding(.horizontal, 14)
                VStack(alignment: .leading, spacing: 14) {
                    aiConnectorSection
                    memoryGovernanceOverviewSection
                    memorySafetySection
                    memoryAuditSection
                }
                .padding(14)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .background(Color(NSColor.controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Color.primary.opacity(0.07))
        }
        .onChange(of: advancedSettingsExpanded) { _, expanded in
            guard expanded else { return }
            Task {
                await runner.refreshMemoryGovernanceIfNeeded()
                await runner.refreshAIConnectorStatus()
            }
        }
    }

    private var advancedRecordingSection: some View {
        SettingsSection(title: "记录服务") {
            SettingsRow(
                systemImage: "record.circle",
                title: runner.statusSnapshot.serviceLabelCN,
                subtitle: runner.statusSnapshot.statusExplanation
            ) {
                HStack(spacing: 8) {
                    Button("刷新") {
                        Task { await runner.refreshStatus() }
                    }
                    if runner.statusSnapshot.captureActionsAvailable {
                        if runner.statusSnapshot.launchAgentRunning {
                            Button("停止") {
                                Task { await runner.stopRecordingService() }
                            }
                        } else {
                            Button("启动") {
                                Task { await runner.startRecordingService() }
                            }
                        }
                    }
                }
            }
        }
    }



    private var overviewTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("系统状态").font(.title2.bold())
                        Text("查看运行状态，按需展开详细信息。").font(.callout).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 12)
                    Button { Task { await runner.refreshStatus(); await attention.refresh() } } label: {
                        Label("刷新", systemImage: "arrow.clockwise")
                    }
                    .disabled(runner.isRefreshingStatus)
                }
                statusRuntimeOverview
                statusProblemsOverview
                todayMetricsSection
                currentActivitySessionOverviewSection
                DisclosureGroup("稳定性与覆盖详情") {
                    VStack(alignment: .leading, spacing: 14) {
                        stabilityScorecardSection
                        healthCenterSection
                    }.padding(.top, 12)
                }
                DisclosureGroup("诊断与运行环境") {
                    VStack(alignment: .leading, spacing: 14) {
                        diagnosticsCenterSection
                        runtimeEnvironmentSection
                    }.padding(.top, 12)
                }
                DisclosureGroup("历史资料") {
                    attentionResourcesSection.padding(.top, 12)
                }
            }
            .frame(maxWidth: 1080, alignment: .leading)
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var statusRuntimeOverview: some View {
        let snapshot = runner.statusSnapshot
        return SettingsSection(title: "运行状态") {
            VStack(alignment: .leading, spacing: 12) {
                SystemStatusLine(title: "后台服务", value: snapshot.serviceLabelCN, symbol: "server.rack")
                SystemStatusLine(title: "屏幕采集", value: snapshot.displayStateUnknown ? "状态未知" : (snapshot.screenCaptureEnabled ? snapshot.statusLabelCN : "屏幕采集已关闭"), symbol: "rectangle.dashed")
                SystemStatusLine(title: "文件追踪", value: snapshot.documentChanges.isAvailable ? (snapshot.documentChanges.coverage.enabled ? (snapshot.documentChanges.coverage.isComplete ? "已开启 · 覆盖完整" : "已开启 · 覆盖待核对") : "已关闭") : "状态未知", symbol: "doc.text.magnifyingglass")
                Divider()
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) { statusRecordingActions }
                    VStack(alignment: .leading, spacing: 8) { statusRecordingActions }
                }
            }.padding(14)
        }
    }

    @ViewBuilder
    private var statusRecordingActions: some View {
        let snapshot = runner.statusSnapshot
        if snapshot.captureActionsAvailable {
            if !snapshot.launchAgentRunning {
                Button("启动服务") { Task { await runner.startRecordingService() } }
            } else if !snapshot.screenCaptureEnabled {
                Button("采集设置") { windowState.selectedTab = .privacy }
            } else if snapshot.status == "paused" {
                Button("恢复采集") { Task { await runner.resume() } }.disabled(snapshot.captureInteractionBlocked)
            } else {
                Menu("暂停采集") {
                    Button("暂停 15 分钟") { Task { await runner.pause(minutes: 15) } }
                    Button("暂停到下次锁屏") { Task { await runner.pauseUntilLock() } }
                    Button("今天不记录") { Task { await runner.pauseToday() } }
                }.disabled(snapshot.captureInteractionBlocked)
            }
            if snapshot.launchAgentRunning {
                Menu("更多") {
                if snapshot.screenCaptureEnabled && snapshot.status != "paused" {
                    Button("立即采集") { Task { await runner.captureNow() } }.disabled(snapshot.captureInteractionBlocked)
                }
                if snapshot.launchAgentRunning {
                    Button("停止后台服务") { Task { await runner.stopRecordingService() } }
                }
                }
            }
        }
    }

    private var statusProblemsOverview: some View {
        let problems = healthSignals.filter { $0.kind == .danger || $0.kind == .warning }
        return SettingsSection(title: "当前需要留意") {
            VStack(alignment: .leading, spacing: 10) {
                if runner.statusSnapshot.displayStateUnknown {
                    Text("状态暂不可读取，请刷新后核对。操作将在取得当前状态后恢复。").font(.callout).foregroundStyle(.secondary)
                } else if problems.isEmpty && attention.snapshot.systems.isEmpty {
                    Label("未发现需要处理的故障", systemImage: "checkmark.circle").foregroundStyle(.secondary)
                } else {
                    ForEach(problems) { signal in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Label(signal.title, systemImage: signal.systemImage).font(.callout.weight(.semibold))
                                Spacer()
                                Button(signal.actionTitle) { performHealthAction(signal.action) }
                            }
                            Text(signal.message).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                ForEach(attention.snapshot.systems) { issue in
                    AttentionIssueRow(issue: issue) { Task { await attention.select(issue) } }
                }
                ForEach(runner.statusSnapshot.attentionItems.filter { visibleOperationalNotice($0) }) { item in
                    StatusNotice(systemImage: item.systemImage, title: item.title, message: item.displayMessage)
                }
                if runner.statusSnapshot.runtimeIdentity.integrityMismatch || runner.statusSnapshot.runtimeIdentity.integrityUnverified {
                    Text(runner.statusSnapshot.runtimeIdentity.integritySummary).font(.callout).foregroundStyle(.orange)
                }
            }.padding(14)
        }
    }

    private var currentActivitySessionOverviewSection: some View {
        SettingsSection(title: "本次会话") {
            VStack(alignment: .leading, spacing: 10) {
                if let session = runner.statusSnapshot.currentActivitySession {
                    Text("已解锁 \(session.durationLabel) · \(session.statusLabel)").font(.callout)
                    Text(runner.statusSnapshot.screenCaptureEnabled ? session.summary : "屏幕采集已关闭；这里仅显示解锁时长。").font(.callout).foregroundStyle(.secondary)
                } else {
                    Text("暂无可确认的会话统计").font(.callout).foregroundStyle(.secondary)
                }
                Button("查看回顾") { recordsMode = 1; windowState.selectedTab = .fileHistory }
            }.padding(14)
        }
    }

    private var activityTimeTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                appHeader
                currentActivitySessionDetailSection
                todayActivitySessionsSection
                activitySessionBoundarySection
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var currentActivitySessionDetailSection: some View {
        SettingsSection(title: "本次解锁会话") {
            if let session = runner.statusSnapshot.currentActivitySession {
                VStack(alignment: .leading, spacing: 12) {
                    SettingsRow(
                        systemImage: "clock",
                        title: session.durationLabel,
                        subtitle: "\(session.statusLabel)；\(session.screenTimeLine)"
                    ) {
                        HStack(spacing: 6) {
                            StatusPill(text: session.statusLabel, kind: session.statusKind)
                            StatusPill(text: session.attributionQuality.label, kind: session.attributionQuality.kind)
                            StatusPill(text: "覆盖 \(session.coverageLabel)", kind: session.coverageRatio < 0.6 ? .warning : .ok)
                            Button {
                                Task { await runner.refreshStatus() }
                            } label: {
                                Label("刷新", systemImage: "arrow.clockwise")
                            }
                        }
                    }
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 132), spacing: 8)], spacing: 8) {
                        MetricCard(title: "总时长", value: session.durationLabel, subtitle: session.statusLabel)
                        MetricCard(title: "主要活动", value: session.topPrimaryLabel, subtitle: "主焦点")
                        MetricCard(title: "工作 / AI", value: session.workShareLabel, subtitle: "主焦点")
                        MetricCard(title: "媒体娱乐", value: session.mediaShareLabel, subtitle: "主焦点")
                        MetricCard(title: "样本", value: "\(session.sampleCount)", subtitle: "时间片")
                        MetricCard(title: "保护/缺口", value: "\(session.privacyEventCount)/\(session.gapEventCount)", subtitle: "内容不外露")
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(spacing: 6) {
                            Label("归因质量", systemImage: "scope")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.secondary)
                            StatusPill(text: session.attributionQuality.label, kind: session.attributionQuality.kind)
                        }
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 132), spacing: 8)], spacing: 8) {
                            MetricCard(title: "Unknown", value: session.attributionQuality.unknownShareLabel, subtitle: ActivitySessionRollup.durationLabel(session.attributionQuality.unknownSeconds))
                            MetricCard(title: "低置信", value: session.attributionQuality.lowConfidenceShareLabel, subtitle: ActivitySessionRollup.durationLabel(session.attributionQuality.lowConfidenceSeconds))
                            MetricCard(title: "副屏媒体", value: session.attributionQuality.visibleMediaLabel, subtitle: "可见曝光 \(session.attributionQuality.visibleMediaShareLabel)")
                            if let topReason = session.attributionQuality.reasonCounts.first {
                                MetricCard(title: "主要原因", value: topReason.label, subtitle: "\(topReason.count) 次")
                            }
                        }
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(Array(session.attributionQuality.explanationLines.prefix(4)).indices, id: \.self) { index in
                                Text(session.attributionQuality.explanationLines[index])
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    HStack(alignment: .top, spacing: 12) {
                        ActivityRollupCompactList(title: "主焦点占比", rows: session.primaryFocusShare, emptyText: "暂无主焦点归因。")
                        ActivityRollupCompactList(title: "多屏可见曝光", rows: session.visibleScreenShare, emptyText: "暂无多屏曝光。")
                    }
                    HStack(alignment: .top, spacing: 12) {
                        ActivityRollupCompactList(title: "Top Apps", rows: session.topApps, emptyText: "暂无 App。")
                        ActivityRollupCompactList(title: "可信网页域名", rows: session.topDomains, emptyText: "暂无可信网页域名。")
                    }
                    if !session.visibleApps.isEmpty || !session.visibleDomains.isEmpty {
                        HStack(alignment: .top, spacing: 12) {
                            ActivityRollupCompactList(title: "可见 App", rows: session.visibleApps, emptyText: "暂无。")
                            ActivityRollupCompactList(title: "可见网页域名", rows: session.visibleDomains, emptyText: "暂无。")
                        }
                    }
                }
                .padding(12)
            } else {
                SettingsRow(
                    systemImage: "clock.badge.questionmark",
                    title: runner.statusSnapshot.status == "locked" ? "锁屏中，没有进行中的会话" : "等待解锁会话",
                    subtitle: "会话从解锁后开始，到再次锁屏时关闭。"
                ) {
                    Button {
                        Task { await runner.refreshStatus() }
                    } label: {
                        Label("刷新", systemImage: "arrow.clockwise")
                    }
                }
            }
        }
    }

    private var todayActivitySessionsSection: some View {
        let today = runner.statusSnapshot.todayActivitySessions
        let summary = today.summary
        return SettingsSection(title: "今天的解锁会话") {
            VStack(alignment: .leading, spacing: 12) {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 132), spacing: 8)], spacing: 8) {
                    MetricCard(title: "会话数", value: "\(today.sessionCount)", subtitle: "今天")
                    MetricCard(title: "总时长", value: summary.totalDurationLabel, subtitle: "解锁墙钟时间")
                    MetricCard(title: "工作 / AI", value: summary.workShareLabel, subtitle: ActivitySessionRollup.durationLabel(summary.workSeconds))
                    MetricCard(title: "媒体娱乐", value: summary.mediaShareLabel, subtitle: ActivitySessionRollup.durationLabel(summary.mediaSeconds))
                }
                HStack(alignment: .top, spacing: 12) {
                    ActivityRollupCompactList(title: "活动类别", rows: summary.topCategories, emptyText: "今天还没有可汇总的会话。")
                    ActivityRollupCompactList(title: "Top Apps", rows: summary.topApps, emptyText: "今天还没有 App 汇总。")
                }
            }
            .padding(12)
        }
    }

    private var activitySessionBoundarySection: some View {
        let boundary = runner.statusSnapshot.todayActivitySessions.screenTimeBoundary
        return SettingsSection(title: "数据边界") {
            VStack(alignment: .leading, spacing: 0) {
                SettingsRow(
                    systemImage: "hourglass.badge.exclamationmark",
                    title: "系统 Screen Time 明细",
                    subtitle: boundary.reason
                ) {
                    StatusPill(text: boundary.statusLabel, kind: boundary.kind)
                }
                Divider().padding(.leading, 52)
                SettingsRow(
                    systemImage: "shield.lefthalf.filled",
                    title: "网页和内容粒度",
                    subtitle: "只展示安全域名、清洗标题、类别和停留时长；不展示完整 URL、query、raw OCR 或截图。"
                ) {
                    StatusPill(text: "raw=false", kind: .ok)
                }
            }
        }
    }

    private var todayMetricsSection: some View {
        SettingsSection(title: "今日记录") {
            SystemStatusMetricLayout {
                SystemStatusMetric(title: "可回顾记录", value: runner.statusSnapshot.countsStateKnown ? "\(runner.statusSnapshot.activities)" : "未知")
                SystemStatusMetric(title: "局部清洗", value: runner.statusSnapshot.countsStateKnown && runner.statusSnapshot.partialCleaningCountKnown ? "\(runner.statusSnapshot.privacyHealth.partialRedactedCount)" : "未知")
                SystemStatusMetric(title: "自动保护", value: runner.statusSnapshot.countsStateKnown ? "\(runner.statusSnapshot.privacyEvents)" : "未知")
                SystemStatusMetric(title: "未记录缺口", value: runner.statusSnapshot.countsStateKnown ? "\(runner.statusSnapshot.gaps)" : "未知")
            }.padding(14)
        }
    }

    private var stabilityScorecardSection: some View {
        let scorecard = runner.statusSnapshot.soakScorecard
        let transition = runner.statusSnapshot.sessionTransitionEvidence
        return SettingsSection(
            title: "7 天稳定性",
            footer: "只统计运行指标、状态证据和人工确认次数；不读取 raw 截图、OCR 原文、窗口标题或完整 URL。"
        ) {
            VStack(alignment: .leading, spacing: 0) {
                SettingsRow(
                    systemImage: "checklist.checked",
                    title: "Trust & Soak 门禁",
                    subtitle: scorecard.summary
                ) {
                    StatusPill(text: scorecard.statusLabel, kind: scorecard.kind)
                }
                Divider().padding(.leading, 52)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 124), spacing: 8)], spacing: 8) {
                    MetricCard(title: "观察天数", value: "\(scorecard.observedDays)/7", subtitle: "真实运行自然日")
                    MetricCard(title: "已通过", value: "\(scorecard.passedCount)/\(scorecard.totalCount)", subtitle: "版本门禁")
                    MetricCard(title: "待观察", value: "\(scorecard.pendingCount)", subtitle: "证据尚不足")
                    MetricCard(title: "未通过", value: "\(scorecard.failedCount)", subtitle: "优先修复")
                    MetricCard(title: "会话证据", value: transition.stateLabel, subtitle: transition.evidenceLabel)
                    MetricCard(title: "最近有效", value: transition.lastValidStateLabel, subtitle: transition.lastValidLabel)
                }
                .padding(12)
                ForEach(Array(scorecard.visibleGates.enumerated()), id: \.element.id) { index, gate in
                    if index > 0 {
                        Divider().padding(.leading, 52)
                    }
                    SettingsRow(
                        systemImage: gate.systemImage,
                        title: gate.label,
                        subtitle: gate.summary
                    ) {
                        StatusPill(text: gate.statusLabel, kind: gate.kind)
                    }
                }
            }
        }
    }

    private var healthCenterSection: some View {
        let signals = healthSignals
        return HealthCenterView(
            overallLabel: healthCenterLabel(for: signals),
            overallKind: healthCenterKind(for: signals),
            overallMessage: healthCenterMessage(for: signals),
            updatedAt: runner.statusUpdatedLabel,
            signals: signals
        ) { action in
            performHealthAction(action)
        }
    }

    private var diagnosticsCenterSection: some View {
        SettingsSection(title: "诊断中心", footer: "只读诊断摘要；不读取 raw 截图、OCR 原文、SQLite 原始表或 HA package 正文。") {
            VStack(alignment: .leading, spacing: 0) {
                SettingsRow(
                    systemImage: "stethoscope",
                    title: "本机诊断",
                    subtitle: runner.diagnosticsStatus.summary
                ) {
                    HStack(spacing: 8) {
                        StatusPill(text: runner.diagnosticsStatus.statusLabel, kind: runner.diagnosticsStatus.kind)
                        StatusPill(text: runner.diagnosticsStatus.modeLabel, kind: .neutral)
                        StatusPill(text: "raw=false", kind: runner.diagnosticsStatus.rawAccess ? .danger : .ok)
                        Button {
                            Task { await runner.refreshDiagnosticsStatus(force: true) }
                        } label: {
                            Label(runner.diagnosticsLoading ? "检查中" : "刷新", systemImage: "arrow.clockwise")
                        }
                        .disabled(runner.diagnosticsLoading)
                        Button {
                            Task { await runner.refreshDiagnosticsStatus(force: true, mode: "full") }
                        } label: {
                            Label("完整", systemImage: "stethoscope")
                        }
                        .disabled(runner.diagnosticsLoading)
                    }
                }
                Divider()
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 120), spacing: 8)], spacing: 8) {
                    MetricCard(title: "检查项", value: "\(runner.diagnosticsStatus.probes.count)", subtitle: runner.diagnosticsStatus.updatedLabel)
                    MetricCard(title: "耗时", value: runner.diagnosticsStatus.elapsedLabel, subtitle: runner.diagnosticsStatus.targetLabel)
                    MetricCard(title: "异常", value: "\(runner.diagnosticsStatus.dangerCount)", subtitle: "需要处理")
                    MetricCard(title: "观察", value: "\(runner.diagnosticsStatus.warningCount)", subtitle: "建议复查")
                    MetricCard(title: "只读", value: "\(runner.diagnosticsStatus.readOnlyActionCount)", subtitle: "不会改数据")
                    MetricCard(title: "预览", value: "\(runner.diagnosticsStatus.previewableActionCount)", subtitle: "先看结果")
                    MetricCard(title: "确认", value: "\(runner.diagnosticsStatus.confirmRequiredActionCount)", subtitle: "人工触发")
                }
                .padding(12)
                if !runner.diagnosticsStatus.probes.isEmpty {
                    Divider()
                    ForEach(Array(runner.diagnosticsStatus.visibleProbes.enumerated()), id: \.element.id) { index, probe in
                        SettingsRow(
                            systemImage: probe.systemImage,
                            title: probe.label,
                            subtitle: probe.timedSummary
                        ) {
                            StatusPill(text: probe.statusLabel, kind: probe.kind)
                        }
                        if index < runner.diagnosticsStatus.visibleProbes.count - 1 {
                            Divider().padding(.leading, 52)
                        }
                    }
                }
                if !runner.diagnosticsStatus.actions.isEmpty {
                    Divider()
                    ForEach(Array(runner.diagnosticsStatus.actions.prefix(4).enumerated()), id: \.element.id) { index, action in
                        SettingsRow(
                            systemImage: action.systemImage,
                            title: action.label,
                            subtitle: action.summary
                        ) {
                            StatusPill(text: action.kindLabel, kind: action.statusKind)
                        }
                        if index < min(runner.diagnosticsStatus.actions.count, 4) - 1 {
                            Divider().padding(.leading, 52)
                        }
                    }
                }
            }
        }
    }

    private var healthSignals: [HealthSignal] {
        [
            recordingHealthSignal,
            privacyHealthSignal,
            coverageHealthSignal,
            automationHealthSignal,
            hygieneHealthSignal,
        ]
    }

    private var recordingHealthSignal: HealthSignal {
        let snapshot = runner.statusSnapshot
        if snapshot.launchAgentLoaded && !snapshot.launchAgentRunning {
            return HealthSignal(
                systemImage: "record.circle",
                title: "记录状态",
                statusLabel: "异常",
                kind: .danger,
                message: "记录服务已安装但没有运行；不会继续自动采集。",
                actionTitle: "重启记录",
                actionImage: "arrow.clockwise.circle",
                action: .startRecording
            )
        }
        if !snapshot.launchAgentLoaded && snapshot.status != "capturing" {
            return HealthSignal(
                systemImage: "record.circle",
                title: "记录状态",
                statusLabel: "已停止",
                kind: .warning,
                message: "记录服务未启动；当前不会自动采集新 keyframe。",
                actionTitle: "启动记录",
                actionImage: "record.circle",
                action: .startRecording
            )
        }
        if snapshot.status == "session_state_unknown" {
            return HealthSignal(
                systemImage: "lock.trianglebadge.exclamationmark",
                title: "记录状态",
                statusLabel: "会话未知",
                kind: .danger,
                message: "无法确认当前登录或锁屏状态；已按隐私优先停止新采集。",
                actionTitle: "刷新",
                actionImage: "arrow.clockwise",
                action: .refresh
            )
        }
        if snapshot.hasKnownCaptureError {
            return HealthSignal(
                systemImage: "exclamationmark.triangle",
                title: "记录状态",
                statusLabel: "错误",
                kind: .danger,
                message: "最近采集有错误；即使当前刷新繁忙或失败，也不会降低这条已知风险。",
                actionTitle: "刷新",
                actionImage: "arrow.clockwise",
                action: .refresh
            )
        }
        if snapshot.status == "busy" {
            return HealthSignal(
                systemImage: "externaldrive.badge.timemachine",
                title: "记录状态",
                statusLabel: "写库中",
                kind: .warning,
                message: "后台正在写入数据库；保留上一次健康证据，稍后自动重试。",
                actionTitle: "刷新",
                actionImage: "arrow.clockwise",
                action: .refresh
            )
        }
        if snapshot.status == "unavailable" || snapshot.status == "unknown" {
            return HealthSignal(
                systemImage: "questionmark.circle",
                title: "记录状态",
                statusLabel: "暂不可读",
                kind: .warning,
                message: "状态刷新失败；已保留上一次健康证据，并暂停依赖实时状态的操作。",
                actionTitle: "刷新",
                actionImage: "arrow.clockwise",
                action: .refresh
            )
        }
        if !snapshot.screenCaptureEnabled {
            return HealthSignal(systemImage: "rectangle.dashed", title: "屏幕采集", statusLabel: "已关闭", kind: .neutral,
                message: "屏幕采集已关闭；后台服务和文件追踪独立运行。", actionTitle: "采集设置", actionImage: "gearshape", action: .openPrivacy)
        }
        if snapshot.status == "paused" {
            return HealthSignal(
                systemImage: "pause.circle",
                title: "记录状态",
                statusLabel: "暂停中",
                kind: .warning,
                message: "\(snapshot.pauseLabel)；暂停期间不会保存关键帧、OCR 或摘要。",
                actionTitle: "恢复",
                actionImage: "play.circle",
                action: .resumeRecording
            )
        }
        if snapshot.status == "locked" {
            return HealthSignal(
                systemImage: "lock",
                title: "记录状态",
                statusLabel: "锁屏",
                kind: .warning,
                message: "锁屏期间停止采集新画面，只允许处理已经排队的本地复核任务。",
                actionTitle: "刷新",
                actionImage: "arrow.clockwise",
                action: .refresh
            )
        }
        if snapshot.captureFreshness.isStale {
            return HealthSignal(
                systemImage: "clock.badge.exclamationmark",
                title: "记录状态",
                statusLabel: "停滞",
                kind: .danger,
                message: "服务在跑，但最近没有新采集；需要复查 LaunchAgent 或权限。",
                actionTitle: "刷新",
                actionImage: "arrow.clockwise",
                action: .refresh
            )
        }
        return HealthSignal(
            systemImage: "record.circle",
            title: "记录状态",
            statusLabel: "正常",
            kind: .ok,
            message: snapshot.nextCaptureSubtitle(now: Date()),
            actionTitle: "暂停 15 分钟",
            actionImage: "pause.circle",
            action: .pause15
        )
    }

    private var privacyHealthSignal: HealthSignal {
        if runner.screenRecordingWarningActive {
            let denied = runner.statusSnapshot.screenRecordingPermission.denied
            return HealthSignal(
                systemImage: "lock.shield",
                title: "隐私保护",
                statusLabel: denied ? "未授权" : "待确认",
                kind: .warning,
                message: runner.statusSnapshot.screenRecordingPermission.summary,
                actionTitle: denied ? "系统设置" : "刷新",
                actionImage: denied ? "gearshape" : "arrow.clockwise",
                action: denied ? .openScreenRecordingSettings : .refresh
            )
        }
        let privacy = runner.statusSnapshot.privacyHealth
        let kind: StatusKind = privacy.reviewRequiredCount > 0 ? .warning : .ok
        let label = privacy.reviewRequiredCount > 0 ? "需观察" : "正常"
        let message = privacy.summary.isEmpty ? "只展示清洗后的安全活动，不展示 raw 截图或 OCR 原文。" : privacy.summary
        return HealthSignal(
            systemImage: "hand.raised",
            title: "隐私保护",
            statusLabel: label,
            kind: kind,
            message: message,
            actionTitle: "隐私页",
            actionImage: "slider.horizontal.3",
            action: .openPrivacy
        )
    }

    private var coverageHealthSignal: HealthSignal {
        let snapshot = runner.statusSnapshot
        let coverage = snapshot.coverageHealth
        if snapshot.captureFreshness.isStale {
            return HealthSignal(
                systemImage: "chart.xyaxis.line",
                title: "覆盖率",
                statusLabel: "停滞",
                kind: .danger,
                message: snapshot.captureFreshness.summary,
                actionTitle: "刷新",
                actionImage: "arrow.clockwise",
                action: .refresh
            )
        }
        if !snapshot.screenCaptureEnabled {
            return HealthSignal(systemImage: "chart.xyaxis.line", title: "覆盖率", statusLabel: "未采集", kind: .neutral,
                message: "屏幕采集已关闭；没有新增采集统计。", actionTitle: "设置", actionImage: "gearshape", action: .openPrivacy)
        }
        if coverage.framesSampled == 0 {
            return HealthSignal(
                systemImage: "chart.xyaxis.line",
                title: "覆盖率",
                statusLabel: "等待",
                kind: .warning,
                message: "今天还没有采集统计；启动记录后再观察覆盖健康。",
                actionTitle: "刷新",
                actionImage: "arrow.clockwise",
                action: .refresh
            )
        }
        let droppedRatio = Double(coverage.framesDropped) / Double(max(coverage.framesSampled, 1))
        let kind: StatusKind = coverage.gapCount > 0 || droppedRatio > 0.20 ? .warning : .ok
        let label = kind == .ok ? "正常" : "观察"
        return HealthSignal(
            systemImage: "chart.xyaxis.line",
            title: "覆盖率",
            statusLabel: label,
            kind: kind,
            message: coverage.summary,
            actionTitle: "质量报告",
            actionImage: "doc.text.magnifyingglass",
            action: .exportQualityReport
        )
    }

    private var automationHealthSignal: HealthSignal {
        let ha = runner.statusSnapshot.haAutomation
        let blocked = ha.failedCount > 0 || ha.unresolvedDeadLetterCount > 0
        let pending = ha.pendingCount > 0 || ha.stalePendingCount > 0 || ha.memoryReportFailedCount > 0 || ha.memoryReportPendingCount > 0
        return HealthSignal(systemImage: "bolt.horizontal.circle", title: "自动化服务",
            statusLabel: blocked ? "阻断" : (pending ? "观察" : "正常"),
            kind: blocked ? .danger : (pending ? .warning : .ok),
            message: "待处理 \(ha.pendingCount)，失败 \(ha.failedCount)，未解决异常 \(ha.unresolvedDeadLetterCount)。",
            actionTitle: "隐私页", actionImage: "slider.horizontal.3", action: .openPrivacy)
    }

    private var hygieneHealthSignal: HealthSignal {
        let snapshot = runner.statusSnapshot
        let hygiene = snapshot.hygieneCleanup
        if snapshot.rawResidue > 0 {
            return HealthSignal(
                systemImage: "externaldrive.badge.exclamationmark",
                title: "数据卫生",
                statusLabel: "临时残留",
                kind: .warning,
                message: "当前有 \(snapshot.rawResidue) 个临时 raw residue；通常会在下个采集周期清掉。",
                actionTitle: "只读检查",
                actionImage: "checklist",
                action: .runBackupHygiene
            )
        }
        let kind = hygiene.kind
        let label = kind == .ok ? "正常" : hygiene.label
        let message = runner.backupHygieneMessage.isEmpty ? hygiene.summary : runner.backupHygieneMessage
        return HealthSignal(
            systemImage: "externaldrive.badge.checkmark",
            title: "数据卫生",
            statusLabel: label,
            kind: kind,
            message: message,
            actionTitle: runner.backupHygieneRunning ? "检查中" : "只读检查",
            actionImage: "checklist",
            action: .runBackupHygiene
        )
    }

    private func healthCenterKind(for signals: [HealthSignal]) -> StatusKind {
        if signals.contains(where: { $0.kind == .danger }) { return .danger }
        if signals.contains(where: { $0.kind == .warning }) { return .warning }
        return .ok
    }

    private func healthCenterLabel(for signals: [HealthSignal]) -> String {
        switch healthCenterKind(for: signals) {
        case .danger: return "需要处理"
        case .warning: return "需要留意"
        case .ok: return "正常"
        case .neutral: return "等待状态"
        }
    }

    private func healthCenterMessage(for signals: [HealthSignal]) -> String {
        let dangerCount = signals.filter { $0.kind == .danger }.count
        let warningCount = signals.filter { $0.kind == .warning }.count
        if dangerCount > 0 {
            return "\(dangerCount) 项需要处理，\(warningCount) 项需要留意。"
        }
        if warningCount > 0 {
            return "\(warningCount) 项需要留意；未发现阻断性故障。"
        }
        return "记录、隐私、覆盖、自动化和数据卫生都在正常范围。"
    }

    private func performHealthAction(_ action: HealthAction) {
        switch action {
        case .refresh:
            Task { await runner.refreshStatus() }
        case .startRecording:
            Task { await runner.startRecordingService() }
        case .pause15:
            Task { await runner.pause(minutes: 15) }
        case .resumeRecording:
            Task { await runner.resume() }
        case .openScreenRecordingSettings:
            runner.openScreenRecordingSettings()
        case .openPrivacy:
            windowState.selectedTab = .settings
        case .exportQualityReport:
            Task { await runner.exportQualityMarkdown(from: rangeFrom, to: rangeTo, label: rangeLabel) }
        case .runBackupHygiene:
            Task { await runner.runBackupHygieneCheck() }
        }
    }

    private var commonOperationsSection: some View {
        SettingsSection(title: "常用操作") {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 128), spacing: 8)], alignment: .leading, spacing: 8) {
                if runner.statusSnapshot.captureActionsAvailable {
                    primaryRecordingButton
                    Button {
                        Task { await runner.captureNow() }
                    } label: {
                        Label("立即采集", systemImage: "camera.viewfinder")
                            .frame(maxWidth: .infinity, minHeight: 30)
                    }
                    .disabled(runner.statusSnapshot.captureInteractionBlocked || runner.statusSnapshot.status == "paused")
                    Menu {
                        Button("暂停 15 分钟") {
                            Task { await runner.pause(minutes: 15) }
                        }
                        Button("暂停到下次锁屏") {
                            Task { await runner.pauseUntilLock() }
                        }
                        Button("今天不记录") {
                            Task { await runner.pauseToday() }
                        }
                    } label: {
                        Label("暂停", systemImage: "pause.circle")
                            .frame(maxWidth: .infinity, minHeight: 30)
                    }
                    .disabled(runner.statusSnapshot.captureInteractionBlocked)
                    Button {
                        Task { await runner.resume() }
                    } label: {
                        Label("恢复", systemImage: "play.circle")
                            .frame(maxWidth: .infinity, minHeight: 30)
                    }
                    .disabled(runner.statusSnapshot.captureInteractionBlocked)
                }
                Button {
                    Task { await runner.refreshStatus() }
                } label: {
                    Label("刷新", systemImage: "arrow.clockwise")
                        .frame(maxWidth: .infinity, minHeight: 30)
                }
            }
            .buttonStyle(.bordered)
            .buttonBorderShape(.roundedRectangle(radius: 8))
            .controlSize(.regular)
            .font(.callout.weight(.medium))
            .padding(12)
        }
    }

    private var runtimeEnvironmentSection: some View {
        SettingsSection(title: "运行环境") {
            VStack(spacing: 0) {
                SettingsRow(
                    systemImage: "gearshape.2",
                    title: "记录服务",
                    subtitle: runner.statusSnapshot.statusExplanation
                ) {
                    StatusPill(text: runner.statusSnapshot.serviceLabelCN, kind: runner.statusSnapshot.statusKind)
                }
                Divider().padding(.leading, 52)
                SettingsRow(
                    systemImage: "shippingbox",
                    title: "运行核心",
                    subtitle: runner.statusSnapshot.runtimeIdentity.displaySummary
                ) {
                    StatusPill(
                        text: runner.statusSnapshot.runtimeIdentity.kindLabel,
                        kind: runner.statusSnapshot.runtimeIdentity.kind
                    )
                }
                Divider().padding(.leading, 52)
                SettingsRow(
                    systemImage: "network",
                    title: "调试面板",
                    subtitle: runner.statusSnapshot.dashboardHealth.summary
                ) {
                    StatusPill(
                        text: runner.statusSnapshot.dashboardHealth.label,
                        kind: runner.statusSnapshot.dashboardHealth.kind
                    )
                }
                Divider().padding(.leading, 52)
                SettingsRow(
                    systemImage: "clock.badge.checkmark",
                    title: "采集新鲜度",
                    subtitle: runner.statusSnapshot.captureFreshness.summary
                ) {
                    StatusPill(
                        text: runner.statusSnapshot.captureFreshness.label,
                        kind: runner.statusSnapshot.captureFreshness.kind
                    )
                }
                Divider().padding(.leading, 52)

                Divider().padding(.leading, 52)
                SettingsRow(
                    systemImage: "info.circle",
                    title: "版本",
                    subtitle: AppBuildInfo.current.shortLabel
                ) {
                    StatusPill(text: "Alpha", kind: .neutral)
                }
                Divider().padding(.leading, 52)
                SettingsRow(
                    systemImage: "lock.shield",
                    title: "原始访问",
                    subtitle: "关闭；界面只展示通过隐私过滤后的活动数据。"
                ) {
                    StatusPill(text: "关闭", kind: .ok)
                }
                Divider().padding(.leading, 52)
                SettingsRow(
                    systemImage: "icloud.slash",
                    title: "云端同步",
                    subtitle: "关闭；当前记录默认保存在本机。"
                ) {
                    StatusPill(text: "关闭", kind: .ok)
                }
                Divider().padding(.leading, 52)
                SettingsRow(
                    systemImage: "calendar",
                    title: "保留范围",
                    subtitle: "默认只保留今天和昨天的清洗后活动数据。"
                ) {
                    StatusPill(text: "今日+昨日", kind: .neutral)
                }
            }
        }
    }

    private var searchTab: some View {
        VStack(alignment: .leading, spacing: 16) {
            rangeSection
            SettingsSection(title: "搜索") {
                HStack(spacing: 10) {
                    TextField("关键词", text: $query)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit {
                            runner.search(query: searchQuery, from: rangeFrom, to: rangeTo)
                        }
                    Button {
                        runner.search(query: searchQuery, from: rangeFrom, to: rangeTo)
                    } label: {
                        Label("搜索", systemImage: "magnifyingglass")
                    }
                    .keyboardShortcut(.return, modifiers: [])
                    .disabled(searchQuery.isEmpty)
                }
                .padding(14)
                ResultPanel(
                    title: "结果 - \(rangeLabel)",
                    text: runner.searchText,
                    emptyText: "输入关键词后搜索；上面的时间按钮只限定搜索范围。"
                )
                    .padding([.horizontal, .bottom], 14)
            }
        }
    }

    private var reviewTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                rangeSection
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .center, spacing: 10) {
                        Text("回顾").font(.title2.bold())
                        Spacer()
                        HStack(spacing: 6) {
                            StatusPill(text: "仅清洗后内容", kind: .ok)
                            StatusPill(text: "不上云", kind: .neutral)
                        }
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text("导出：完整日志 / 摘要 / 质量报告 / HA 安全包 / AI 接手包")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                        ScrollView(.horizontal) {
                            HStack(spacing: 10) {
                                Button {
                                    refreshReviewContent()
                                } label: {
                                    Label("刷新", systemImage: "arrow.clockwise")
                                }
                                .help("重新读取当前时间范围")
                                Button {
                                    Task { await runner.exportAIHandoff(from: rangeFrom, to: rangeTo, label: rangeLabel) }
                                } label: {
                                    Label("AI 接手包", systemImage: "person.text.rectangle")
                                }
                                .help("生成只读 clear_only AI 接手包 Markdown；界面只显示计数和隐私扫描状态")
                                Button {
                                    Task { await runner.exportTimelineMarkdown(from: rangeFrom, to: rangeTo, label: rangeLabel, kind: .full) }
                                } label: {
                                    Label("完整日志", systemImage: "doc.text")
                                }
                                .help("把当前时间范围的完整日志保存为 Markdown")
                                Button {
                                    Task { await runner.exportTimelineMarkdown(from: rangeFrom, to: rangeTo, label: rangeLabel, kind: .learning) }
                                } label: {
                                    Label("下载摘要", systemImage: "square.and.arrow.down")
                                }
                                .help("下载当前时间范围的学习摘要 Markdown")
                                Button {
                                    Task { await runner.exportQualityMarkdown(from: rangeFrom, to: rangeTo, label: rangeLabel) }
                                } label: {
                                    Label("质量报告", systemImage: "checklist")
                                }
                                .help("导出当前时间范围的清洗质量、DocumentSignal 和记忆候选报告")
                                Button {
                                    Task { await runner.exportFullMirror(from: rangeFrom, to: rangeTo, label: rangeLabel) }
                                } label: {
                                    Label("下载 HA 安全包", systemImage: "shippingbox")
                                }
                                .help("下载当前时间范围的 HA Source Package JSON；这是完整清洗后结构化数据，不包含原始截图或 OCR 原文")
                            }
                            .fixedSize(horizontal: true, vertical: false)
                        }
                        .scrollIndicators(.visible)
                    }
                }
                if !runner.markdownExportMessage.isEmpty {
                    exportStatusBar
                }
                aiReviewSection
                aiHandoffSection
                aiConnectorSection
                qualityReportSection
                ResultPanel(
                    title: "统计概览 - \(rangeLabel)",
                    text: runner.dailyReviewText,
                    emptyText: "选择时间范围后刷新，查看本地统计概览。"
                )
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollIndicators(.visible)
    }

    private var aiReviewSection: some View {
        AIReviewPanel(
            snapshot: runner.aiReviewSnapshot,
            loading: runner.aiReviewLoading,
            rangeLabel: rangeLabel,
            onRefresh: {
                Task { await runner.refreshAIReview(from: rangeFrom, to: rangeTo, label: rangeLabel) }
            },
            onExport: {
                Task { await runner.exportAIHandoff(from: rangeFrom, to: rangeTo, label: rangeLabel) }
            }
        )
    }

    private var aiHandoffSection: some View {
        SettingsSection(title: "AI 接手包", footer: "只生成当前选择范围的安全包；不读取 raw 截图、OCR 原文或 SQLite 原始表，不执行记忆写入。") {
            SettingsRow(
                systemImage: "lock.doc",
                title: "只读安全交接",
                subtitle: runner.aiHandoffLastSummary
            ) {
                HStack(spacing: 6) {
                    StatusPill(text: "clear_only", kind: .ok)
                    StatusPill(text: "raw=false", kind: .ok)
                    Button {
                        Task { await runner.exportAIHandoff(from: rangeFrom, to: rangeTo, label: rangeLabel) }
                    } label: {
                        Label("生成", systemImage: "square.and.arrow.down")
                    }
                    .help("导出给 Codex / Hermes / ChatGPT 使用的只读 AI 接手包")
                }
            }
        }
    }

    private var aiConnectorSection: some View {
        SettingsSection(title: "本机 AI 连接器", footer: "手动管理的 loopback GET-only 接口；App 启动时不会自动常驻，不提供写入、删除、审批或规则修改入口。") {
            VStack(alignment: .leading, spacing: 0) {
                SettingsRow(
                    systemImage: "point.3.connected.trianglepath.dotted",
                    title: "连接器状态",
                    subtitle: runner.aiConnectorStatus.summary
                ) {
                    LazyVGrid(
                        columns: [GridItem(.adaptive(minimum: 82), spacing: 6)],
                        alignment: .trailing,
                        spacing: 6
                    ) {
                        StatusPill(text: runner.aiConnectorStatus.statusLabel, kind: runner.aiConnectorStatus.kind)
                        StatusPill(text: "GET-only", kind: .ok)
                        StatusPill(text: "raw=false", kind: runner.aiConnectorStatus.rawAccess ? .danger : .ok)
                        if runner.aiConnectorStatus.portConflict {
                            StatusPill(text: "端口占用", kind: .warning)
                        }
                        Button {
                            Task { await runner.refreshAIConnectorStatus() }
                        } label: {
                            Label(runner.aiConnectorLoading ? "读取中" : "刷新", systemImage: "arrow.clockwise")
                        }
                        .disabled(runner.aiConnectorLoading)
                    }
                    .frame(minWidth: 180, maxWidth: 360, alignment: .trailing)
                }
                Divider()
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 130), spacing: 10)],
                    alignment: .leading,
                    spacing: 10
                ) {
                    MetricCard(title: "地址", value: runner.aiConnectorStatus.baseURLLabel, subtitle: "只允许本机回环")
                    MetricCard(title: "端点", value: "\(runner.aiConnectorStatus.endpointCount)", subtitle: "/status /package /diagnostics")
                    MetricCard(title: "写入", value: runner.aiConnectorStatus.writeLabel, subtitle: "无 POST 写入入口")
                    MetricCard(title: "进程", value: runner.aiConnectorStatus.processLabel, subtitle: runner.aiConnectorStatus.listenerStatusLabel)
                }
                .padding(12)
                SettingsRow(
                    systemImage: "switch.2",
                    title: "手动控制",
                    subtitle: runner.aiConnectorStatus.controlSummary
                ) {
                    LazyVGrid(
                        columns: [GridItem(.adaptive(minimum: 76), spacing: 6)],
                        alignment: .trailing,
                        spacing: 6
                    ) {
                        Button {
                            Task { await runner.startAIConnector() }
                        } label: {
                            Label("启动", systemImage: "play.fill")
                        }
                        .disabled(runner.aiConnectorLoading || runner.aiConnectorStatus.serverRunning)
                        Button {
                            Task { await runner.stopAIConnector() }
                        } label: {
                            Label("停止", systemImage: "stop.fill")
                        }
                        .disabled(runner.aiConnectorLoading || !runner.aiConnectorStatus.serverRunning)
                        Button {
                            runner.copyAIConnectorBaseURL()
                        } label: {
                            Label("复制地址", systemImage: "link")
                        }
                        .disabled(runner.aiConnectorStatus.baseURL.isEmpty)
                        Button {
                            runner.copyAIConnectorStartCommand()
                        } label: {
                            Label("命令", systemImage: "terminal")
                        }
                        .disabled(runner.aiConnectorStatus.startCommand.isEmpty)
                    }
                    .frame(minWidth: 180, maxWidth: 360, alignment: .trailing)
                }
            }
        }
    }

    private var qualityReportSection: some View {
        let quality = runner.statusSnapshot.qualityHealth
        return SettingsSection(title: "质量报告") {
            VStack(alignment: .leading, spacing: 12) {
                SettingsRow(
                    systemImage: "checklist",
                    title: "清洗质量",
                    subtitle: quality.summary
                ) {
                    StatusPill(text: quality.label, kind: quality.kind)
                }
                Divider()
                HStack(spacing: 10) {
                    MetricCard(
                        title: "DocumentSignal",
                        value: "\(quality.documentMatchCount)/\(quality.documentCandidateCount)",
                        subtitle: quality.documentLine
                    )
                    MetricCard(
                        title: "长期记忆",
                        value: "\(quality.eligibleCandidateCount)/\(quality.totalCandidateCount)",
                        subtitle: quality.memoryWriteLine
                    )
                    MetricCard(
                        title: "人工反馈",
                        value: "\(quality.feedbackCount)",
                        subtitle: quality.feedbackLine
                    )
                }
                Divider()
                HStack(spacing: 10) {
                    MetricCard(
                        title: "Unknown",
                        value: "\(quality.attributionHealth.unknownAppFrameCount)",
                        subtitle: quality.attributionHealth.unknownLine
                    )
                    MetricCard(
                        title: "结构化信号",
                        value: "\(quality.attributionHealth.extensionBrowserSignalCount)",
                        subtitle: quality.attributionHealth.structuredLine
                    )
                    MetricCard(
                        title: "归因可信",
                        value: quality.attributionHealth.confidenceLabel,
                        subtitle: quality.attributionHealth.summary
                    )
                    MetricCard(
                        title: "跨屏边界",
                        value: quality.attributionHealth.crossDisplayInheritance == "disabled" ? "关闭" : "复查",
                        subtitle: quality.attributionHealth.boundaryLine
                    )
                }
                Divider()
                SettingsRow(
                    systemImage: "memorychip",
                    title: "长期记忆审计",
                    subtitle: quality.memoryAuditLine
                ) {
                    HStack(spacing: 6) {
                        StatusPill(text: "写入 \(quality.latestMemoriesWrittenCount)", kind: .neutral)
                        StatusPill(text: "拦下 \(quality.latestSkippedCandidateCount)", kind: .neutral)
                        Button {
                            windowState.selectedTab = .settings
                        } label: {
                            Label("记忆页", systemImage: "memorychip")
                        }
                        .help("打开只读记忆治理台")
                    }
                }
                Divider()
                SettingsRow(
                    systemImage: "chart.line.uptrend.xyaxis",
                    title: "记忆治理趋势",
                    subtitle: quality.memoryGovernanceTrend.summary
                ) {
                    HStack(spacing: 6) {
                        StatusPill(text: "\(quality.memoryGovernanceTrend.windowJobCount) 次", kind: .neutral)
                        StatusPill(text: "风险 \(quality.memoryGovernanceTrend.subjectRiskCandidateCount)", kind: quality.memoryGovernanceTrend.subjectRiskCandidateCount > 0 ? .warning : .ok)
                        StatusPill(text: "降级 \(quality.memoryGovernanceTrend.singleEvidenceDemotedCount)", kind: .neutral)
                    }
                }
                Divider()
                SettingsRow(
                    systemImage: "sparkles",
                    title: "Hermes 质量顾问",
                    subtitle: runner.statusSnapshot.qualityAdvice.summary
                ) {
                    HStack(spacing: 6) {
                        StatusPill(
                            text: runner.statusSnapshot.qualityAdvice.label,
                            kind: runner.statusSnapshot.qualityAdvice.kind
                        )
                        StatusPill(
                            text: "\(runner.statusSnapshot.qualityAdvice.latestRecommendationCount) 条建议",
                            kind: .neutral
                        )
                        StatusPill(
                            text: "写入 \(runner.statusSnapshot.haAutomation.latestMemoriesWrittenCount)",
                            kind: .neutral
                        )
                    }
                }
                if !quality.recommendations.isEmpty {
                    Divider()
                    Text(quality.recommendations.prefix(2).joined(separator: "\n"))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(14)
        }
    }

    private var fileHistoryTab: some View {
        let snapshot = runner.fileHistorySnapshot
        return ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("文件历史")
                            .font(.title2.bold())
                        Text("只读查看 PAM 已记录的文件变化；原始路径、内容和会话信息不会显示在这里。")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        Task { await runner.refreshFileHistory(range: fileHistoryRange) }
                    } label: {
                        Label(runner.fileHistoryLoading ? "读取中" : "刷新", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(.bordered)
                    .disabled(runner.fileHistoryLoading)
                }

                Picker("时间范围", selection: $fileHistoryRange) {
                    Text("今天").tag("today")
                    Text("昨天").tag("yesterday")
                    Text("7 天").tag("7d")
                    Text("30 天").tag("30d")
                    Text("90 天").tag("90d")
                }
                .pickerStyle(.segmented)
                .onChange(of: fileHistoryRange) { _, range in
                    Task { await runner.refreshFileHistory(range: range) }
                }

                if runner.fileHistoryLoading && !snapshot.isAvailable {
                    SettingsSection(title: "正在读取") {
                        HStack(spacing: 10) {
                            ProgressView()
                            Text("正在整理安全文件历史…")
                                .foregroundStyle(.secondary)
                            Spacer()
                        }
                        .padding(14)
                    }
                } else if snapshot.isBlocked || !snapshot.isAvailable {
                    StatusNotice(
                        systemImage: "exclamationmark.shield",
                        title: "文件历史暂不可读",
                        message: snapshot.limitations.first ?? runner.fileHistoryMessage
                    )
                } else {
                    fileHistoryCoverageSection(snapshot)
                    fileHistoryCategorySection(
                        title: "AI 直接增加、你未修改过",
                        classification: "ai_only",
                        total: snapshot.totals.aiOnly,
                        items: snapshot.items
                    )
                    fileHistoryCategorySection(
                        title: "你修改过",
                        classification: "user_modified",
                        total: snapshot.totals.userModified,
                        items: snapshot.items
                    )
                    fileHistoryUnknownSection(snapshot)
                }

                if !runner.fileHistoryMessage.isEmpty {
                    Text(runner.fileHistoryMessage)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
        }
    }

    private func fileHistoryCoverageSection(_ snapshot: DocumentChangesSnapshot) -> some View {
        let coverage = snapshot.coverage
        let prefix = coverage.isComplete ? "记录到" : "至少记录到"
        return SettingsSection(title: "覆盖状态") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Label(
                        "\(prefix) \(snapshot.totals.distinctFiles) 个文件",
                        systemImage: coverage.isComplete ? "checkmark.circle" : "exclamationmark.triangle"
                    )
                    Spacer()
                    StatusPill(
                        text: coverage.isComplete ? "连续" : "不完整",
                        kind: coverage.isComplete ? .ok : .warning
                    )
                }
                Text(
                    coverage.isComplete
                        ? "三个分类互斥，合计与文件总数一致。"
                        : "仍展示已经确认的事实；覆盖中断不会再把整段历史伪装成 0。"
                )
                .font(.caption)
                .foregroundStyle(.secondary)
                if !coverage.coverageStartedAt.isEmpty {
                    Text("连续监测起点：\(fileHistoryTimestamp(coverage.coverageStartedAt))")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
                if snapshot.totals.protected > 0 {
                    Text("其中 \(snapshot.totals.protected) 个受保护文件只计数，不显示名称。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(12)
        }
    }

    private func fileHistoryCategorySection(
        title: String,
        classification: String,
        total: Int,
        items: [DocumentChangeItem]
    ) -> some View {
        let matching = items.filter { $0.actorClassification == classification }
        let grouped = Dictionary(grouping: matching) {
            $0.projectLabel.isEmpty ? "未归类" : $0.projectLabel
        }
        return SettingsSection(title: "\(title)（\(total)）") {
            VStack(alignment: .leading, spacing: 10) {
                if total == 0 {
                    Text("暂无可确认内容")
                        .foregroundStyle(.secondary)
                } else if matching.isEmpty {
                    Text("本分类有记录，但明细因受保护规则或查询上限未显示。")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(grouped.keys.sorted(), id: \.self) { project in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(project)
                                .font(.headline)
                            ForEach(grouped[project] ?? []) { item in
                                fileHistoryItemRow(item)
                            }
                        }
                    }
                    if total > matching.count {
                        Text("另有 \(total - matching.count) 个未显示明细，包含受保护文件或查询上限外项目。")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(12)
        }
    }

    private func fileHistoryUnknownSection(_ snapshot: DocumentChangesSnapshot) -> some View {
        let matching = snapshot.items.filter { $0.actorClassification == "unattributed" }
        let grouped = Dictionary(grouping: matching) {
            $0.projectLabel.isEmpty ? "未归类" : $0.projectLabel
        }
        return SettingsSection(title: "待确认归因") {
            DisclosureGroup(isExpanded: $fileHistoryUnknownExpanded) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("缺少明确人工编辑证据，或 AI 创建后的每次修改凭据与连续覆盖条件不完整；不会默认算作你修改。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    ForEach(grouped.keys.sorted(), id: \.self) { project in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(project)
                                .font(.headline)
                            ForEach(grouped[project] ?? []) { item in
                                fileHistoryItemRow(item)
                            }
                        }
                    }
                    if snapshot.totals.unattributed > matching.count {
                        Text("另有 \(snapshot.totals.unattributed - matching.count) 个未显示明细，包含受保护文件或查询上限外项目。")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.top, 8)
            } label: {
                HStack {
                    Label(
                        "\(snapshot.totals.unattributed) 个文件",
                        systemImage: "questionmark.circle"
                    )
                    Spacer()
                    Text("默认收起")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(12)
        }
    }

    private func fileHistoryItemRow(_ item: DocumentChangeItem) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "doc")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.displayName)
                    .font(.body.weight(.medium))
                Text("\(fileHistoryActionLabel(item.changeKind)) · \(fileHistoryTimestamp(item.lastChangedAt))")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(.vertical, 4)
    }

    private func fileHistoryActionLabel(_ value: String) -> String {
        switch value {
        case "created": return "创建"
        case "renamed": return "重命名"
        case "deleted": return "删除"
        default: return "修改"
        }
    }

    private func fileHistoryTimestamp(_ value: String) -> String {
        Self.localTimestamp(value)
    }

    private var activityConfirmationsTab: some View {
        let snapshot = runner.activityConfirmationSnapshot
        let diagnosticsSnapshot = runner.activityConfirmationUsesFastCards
            ? runner.activityConfirmationDiagnosticsSnapshot
            : snapshot
        let pendingGroups = runner.activityConfirmationPendingGroups
        let visibleGroups = runner.activityConfirmationUsesFastCards
            ? pendingGroups
            : Array(pendingGroups.prefix(confirmationVisibleGroupCount))
        let visibleAnswerableGroups = visibleGroups.filter { !$0.isInformationInsufficient }
        let visibleHighPriorityGroups = visibleAnswerableGroups.filter { $0.priority == .high }
        let visibleOtherGroups = visibleAnswerableGroups.filter { $0.priority != .high }
        // Fast-cards pages now carry only answerable cards, so the "load
        // more" budget counts against the answerable queue, not the whole
        // 7-day card population.
        let remainingGroupCount = runner.activityConfirmationUsesFastCards
            ? max(0, snapshot.pendingQuestionCount - pendingGroups.count)
            : max(0, pendingGroups.count - visibleGroups.count)
        let fastCardsHeader = snapshot.pendingQuestionCount == 0
            ? (
                snapshot.informationInsufficientCount == 0
                    ? "没有需要你处理的问题"
                    : "没有需要你处理的问题 · \(snapshot.informationInsufficientCount) 条信息不足（默认收起）"
            )
            : "\(snapshot.pendingQuestionCount) 个问题需要确认"
                + (
                    snapshot.informationInsufficientCount == 0
                        ? ""
                        : " · \(snapshot.informationInsufficientCount) 条信息不足（默认收起）"
                )
        // A plain VStack, not LazyVStack: the enclosing ScrollView has to ask
        // for the full content height anyway, so laziness buys nothing here
        // while its placement cache can enter a self-feeding update loop
        // (placeSubviews -> AllItemsPhaseMutation -> updateItemPhases ->
        // propagate_dirty -> re-measure) that pins the main thread at 100%.
        // Observed 2026-08-07 with a 1-card queue plus the collapsed clue
        // panel; 6bf6ab7 already made the same swap for the inner stacks.
        return ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .center, spacing: 12) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("待确认")
                            .font(.title2.bold())
                        Text(
                            runner.activityConfirmationUsesFastCards
                                ? fastCardsHeader
                                : snapshot.navigationState.pageHeaderText(
                                    pendingSourceCount: snapshot.pendingSourceCount
                                )
                        )
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 12)
                    Button {
                        manualActivityEntryPresented = true
                    } label: {
                        Label("补记系统遗漏", systemImage: "plus.circle")
                    }
                    .buttonStyle(.bordered)
                    .disabled(runner.manualActivityEntryLoading || snapshot.isBlocked)
                    .accessibilityHint("补记一项确定由你本人完成、但系统没有记录到的本机活动")
                    Button {
                        Task { await runner.refreshActivityConfirmations() }
                    } label: {
                        Label(runner.activityConfirmationLoading ? "读取中" : "刷新", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(.bordered)
                    .disabled(
                        runner.activityConfirmationLoading
                            || runner.activityConfirmationLoadingMore
                            || !runner.activityConfirmationWorkingGroupKeys.isEmpty
                            || !runner.activityConfirmationClarifyingGroupKeys.isEmpty
                    )
                }

                if snapshot.contractVersion >= 4,
                   let v4Summary = snapshot.v4Summary,
                   !snapshot.isBlocked {
                    activityConfirmationV4SummarySection(v4Summary)
                }

                Text(runner.activityConfirmationUpdatedLabel)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, alignment: .trailing)

                if !runner.activityConfirmationMessage.isEmpty {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: runner.activityConfirmationMessageSeverity.systemImage)
                            .foregroundStyle(runner.activityConfirmationMessageSeverity.color)
                        Text(runner.activityConfirmationMessage)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer()
                    }
                    .padding(12)
                    .background(runner.activityConfirmationMessageSeverity.color.opacity(0.10))
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                }

                if runner.activityConfirmationLoading && snapshot.status == "unknown" {
                    SettingsSection(title: "正在读取") {
                        HStack(spacing: 10) {
                            ProgressView()
                            Text("正在整理需要你确认的安全摘要…")
                                .foregroundStyle(.secondary)
                            Spacer()
                        }
                        .padding(14)
                    }
                } else if snapshot.isBlocked {
                    StatusNotice(
                        systemImage: "exclamationmark.shield",
                        title: "待确认内容暂不可读",
                        message: snapshot.summaryLine
                    )
                } else if pendingGroups.isEmpty {
                    SettingsSection(title: "没有待确认事项") {
                        SettingsRow(
                            systemImage: "checkmark.circle",
                            title: "当前没有需要确认的事项",
                            subtitle: "出现需要你判断的活动时，会直接显示在这里。"
                        ) {
                            StatusPill(text: "已清空", kind: .ok)
                        }
                    }
                } else {
                    VStack(alignment: .leading, spacing: 10) {
                        if runner.activityConfirmationUsesFastCards {
                            if !visibleAnswerableGroups.isEmpty {
                                confirmationGroupHeading(
                                    title: "待确认",
                                    detail: "这里只保留需要你判断、且机器还没有独立结果证据的现实事件。",
                                    systemImage: "questionmark.bubble"
                                )
                                ForEach(visibleAnswerableGroups) { group in
                                    activityConfirmationCard(group)
                                }
                            }
                            if snapshot.informationInsufficientCount > 0 {
                                ConfirmationDisclosurePanel(
                                    title: "信息不足",
                                    systemImage: "questionmark.diamond",
                                    badge: "\(snapshot.informationInsufficientCount)",
                                    isExpanded: $confirmationInformationInsufficientExpanded
                                ) {
                                    if confirmationInformationInsufficientExpanded {
                                        VStack(alignment: .leading, spacing: 10) {
                                            Text("系统没有识别出具体对象；这些线索不要求你回答“是否发生”，默认收起以减少干扰。")
                                                .font(.caption)
                                                .foregroundStyle(.secondary)
                                                .fixedSize(horizontal: false, vertical: true)
                                            if runner.activityConfirmationInsufficientGroups.isEmpty
                                                && runner.activityConfirmationInsufficientLoading {
                                                HStack(spacing: 8) {
                                                    ProgressView()
                                                    Text("正在读取信息不足线索…")
                                                        .font(.caption)
                                                        .foregroundStyle(.secondary)
                                                }
                                            }
                                            ForEach(runner.activityConfirmationInsufficientGroups) { group in
                                                activityConfirmationCard(group)
                                            }
                                            if runner.activityConfirmationInsufficientHasMore {
                                                Button {
                                                    Task {
                                                        await runner.loadInsufficientActivityConfirmationCards(reset: false)
                                                    }
                                                } label: {
                                                    Label(
                                                        runner.activityConfirmationInsufficientLoading
                                                            ? "正在读取更多…"
                                                            : "再显示更多线索",
                                                        systemImage: "chevron.down.circle"
                                                    )
                                                    .frame(maxWidth: .infinity)
                                                }
                                                .buttonStyle(.bordered)
                                                .disabled(runner.activityConfirmationInsufficientLoading)
                                            }
                                        }
                                        .padding(.top, 10)
                                    }
                                }
                                .onChange(of: confirmationInformationInsufficientExpanded) { _, expanded in
                                    if expanded, runner.activityConfirmationInsufficientGroups.isEmpty {
                                        Task {
                                            await runner.loadInsufficientActivityConfirmationCards(reset: true)
                                        }
                                    }
                                }
                            }
                        } else {
                            if !visibleHighPriorityGroups.isEmpty {
                                confirmationGroupHeading(
                                    title: "高优先事件",
                                    detail: "这些事件可能影响业务复盘或后续动作；仍需你确认后才进入事实出口。",
                                    systemImage: "exclamationmark.circle.fill"
                                )
                                ForEach(visibleHighPriorityGroups) { group in
                                    activityConfirmationCard(group)
                                }
                            }
                            if !visibleOtherGroups.isEmpty {
                                confirmationGroupHeading(
                                    title: visibleHighPriorityGroups.isEmpty ? "待确认事件" : "其他待确认",
                                    detail: "按时间和优先级排序；暂时跳过不会放行事实。",
                                    systemImage: "questionmark.bubble"
                                )
                                ForEach(visibleOtherGroups) { group in
                                    activityConfirmationCard(group)
                                }
                            }
                        }

                        if remainingGroupCount > 0 {
                            Button {
                                if runner.activityConfirmationUsesFastCards {
                                    Task { await runner.loadMoreActivityConfirmationCards() }
                                } else {
                                    confirmationVisibleGroupCount += 12
                                }
                            } label: {
                                Group {
                                    if runner.activityConfirmationLoadingMore {
                                        Label("正在读取更多…", systemImage: "hourglass")
                                    } else {
                                        Label(
                                            "再显示 \(min(12, remainingGroupCount)) 项（剩余 \(remainingGroupCount) 项）",
                                            systemImage: "chevron.down.circle"
                                        )
                                    }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.bordered)
                            .disabled(runner.activityConfirmationLoadingMore)
                            .padding(.top, 2)
                            .accessibilityHint("分批加载更多待确认卡片；已填写的内容不会丢失")
                        }
                    }
                }

                if !snapshot.isBlocked && snapshot.status != "unknown" {
                    ConfirmationDisclosurePanel(
                        title: "确认漏斗与过滤诊断",
                        systemImage: "line.3.horizontal.decrease.circle",
                        badge: diagnosticsSnapshot.v4Summary.map { "过滤 \($0.candidateFunnel.hardFiltered)" },
                        isExpanded: $confirmationDiagnosticsExpanded
                    ) {
                        if runner.activityConfirmationDiagnosticsLoading {
                            HStack(spacing: 8) {
                                ProgressView().controlSize(.small)
                                Text("正在按需读取诊断…")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            .padding(.top, 10)
                        } else if diagnosticsSnapshot.status == "unknown" {
                            Text("展开后才读取完整诊断，平时不会拖慢卡片列表。")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .padding(.top, 10)
                        } else {
                            activityConfirmationFunnelSection(diagnosticsSnapshot)
                                .padding(.top, 10)
                        }
                    }

                    ConfirmationDisclosurePanel(
                        title: "反馈如何生效",
                        systemImage: "slider.horizontal.3",
                        isExpanded: $confirmationImpactExpanded
                    ) {
                        activityConfirmationImpactSection
                            .padding(.top, 10)
                    }

                    ConfirmationDisclosurePanel(
                        title: "最近校准",
                        systemImage: "scope",
                        badge: diagnosticsSnapshot.calibrations.isEmpty ? nil : "\(diagnosticsSnapshot.calibrations.count)",
                        isExpanded: $confirmationCalibrationsExpanded
                    ) {
                        if runner.activityConfirmationDiagnosticsLoading {
                            HStack(spacing: 8) {
                                ProgressView().controlSize(.small)
                                Text("正在按需读取最近校准…")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        } else if diagnosticsSnapshot.calibrations.isEmpty {
                            SettingsRow(
                                systemImage: "scope",
                                title: "还没有校准记录",
                                subtitle: "你对待确认项目作出否定或纠错后，安全摘要和原因会显示在这里。"
                            ) {
                                StatusPill(text: "0 条", kind: .neutral)
                            }
                        } else {
                            VStack(alignment: .leading, spacing: 10) {
                                ForEach(diagnosticsSnapshot.calibrations.prefix(10)) { calibration in
                                    ActivityConfirmationCalibrationRow(
                                        calibration: calibration,
                                        isLoading: runner.activityConfirmationLoading
                                    ) {
                                        Task {
                                            await runner.revokeActivityConfirmationCalibration(calibration)
                                        }
                                    }
                                }
                            }
                        }
                    }

                    ConfirmationDisclosurePanel(
                        title: "隐私与事实出口",
                        systemImage: "lock.shield",
                        isExpanded: $confirmationPrivacyExpanded
                    ) {
                        VStack(alignment: .leading, spacing: 10) {
                            confirmationImpactLine(
                                systemImage: "text.viewfinder",
                                title: "只展示已清洗内容",
                                detail: "卡片和展开区都来自本机隐私清洗后的文本，不展示原始截图或过滤前 OCR。"
                            )
                            Divider()
                            confirmationImpactLine(
                                systemImage: "desktopcomputer",
                                title: "重新梳理只在本机进行",
                                detail: "只使用组内清洗文本和本机安全上下文，不调用 Hermes，也不会放行任何事实。"
                            )
                            Divider()
                            confirmationImpactLine(
                                systemImage: "arrow.triangle.branch",
                                title: "未确认继续关闭下游",
                                detail: "未确认、否定和暂时跳过的内容不会进入 Today、Review、搜索、导出、Hermes 或飞书的确定事实区。"
                            )
                        }
                        .padding(.top, 10)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task {
            runner.setActivityConfirmationViewVisible(true)
            await runner.refreshActivityConfirmationsIfNeeded()
        }
        .onDisappear {
            runner.setActivityConfirmationViewVisible(false)
        }
        .onChange(of: confirmationDiagnosticsExpanded) { _, expanded in
            guard expanded else { return }
            Task { await runner.loadActivityConfirmationDiagnosticsIfNeeded() }
        }
        .onChange(of: confirmationCalibrationsExpanded) { _, expanded in
            guard expanded else { return }
            Task { await runner.loadActivityConfirmationDiagnosticsIfNeeded() }
        }
        .onChange(of: runner.activityConfirmationDiagnosticsSnapshot.status) { _, status in
            guard ActivityConfirmationDiagnosticsLoadPolicy.shouldReloadAfterCardsReplacement(
                diagnosticsStatus: status,
                diagnosticsExpanded: confirmationDiagnosticsExpanded,
                calibrationsExpanded: confirmationCalibrationsExpanded
            ) else { return }
            Task { await runner.loadActivityConfirmationDiagnosticsIfNeeded() }
        }
    }

    private func activityConfirmationCard(_ group: ActivityConfirmationQuestionGroup) -> some View {
        ActivityConfirmationQuestionCard(
            group: group,
            draft: activityConfirmationCardDraftBinding(for: group),
            isLoading: runner.activityConfirmationWorkingGroupKeys.contains(group.groupKey),
            isClarifying: runner.activityConfirmationClarifyingGroupKeys.contains(group.groupKey),
            clarification: group.clarification ?? runner.activityConfirmationClarifications[group.groupKey],
            answer: { answer, reason, note, workstream, outcome in
                Task {
                    await runner.answerActivityConfirmation(
                        group: group,
                        answer: answer,
                        reason: reason,
                        note: note,
                        workstream: workstream,
                        outcome: outcome
                    )
                }
            },
            clarify: { note in
                Task { await runner.clarifyActivityConfirmation(group, note: note) }
            }
        )
    }

    private func activityConfirmationCardDraftBinding(
        for group: ActivityConfirmationQuestionGroup
    ) -> Binding<ActivityConfirmationCardDraft> {
        Binding(
            get: {
                confirmationCardDrafts[group.groupKey] ?? ActivityConfirmationCardDraft(group: group)
            },
            set: { draft in
                confirmationCardDrafts[group.groupKey] = draft
            }
        )
    }

    private func confirmationGroupHeading(title: String, detail: String, systemImage: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: systemImage)
                .foregroundStyle(Color.accentColor)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.headline)
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
        }
        .padding(.top, 4)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }

    private func activityConfirmationV4SummarySection(_ summary: ActivityConfirmationV4Summary) -> some View {
        SettingsSection(title: "业务归因与结果") {
            HStack(spacing: 0) {
                quietMetric(
                    title: "高优先",
                    value: "\(summary.priorityCounts.high) 条",
                    detail: "普通 \(summary.priorityCounts.normal) · 低 \(summary.priorityCounts.low)"
                )
                Divider().frame(height: 54)
                quietMetric(
                    title: "业务归因",
                    value: "\(summary.pendingByKind.businessContext) 条",
                    detail: "需要补充工作流"
                )
                Divider().frame(height: 54)
                quietMetric(
                    title: "业务结果",
                    value: "\(summary.pendingByKind.outcome) 条",
                    detail: "需要补充结果"
                )
            }
            .padding(.vertical, 8)
        }
    }

    @ViewBuilder
    private func activityConfirmationFunnelSection(_ snapshot: ActivityConfirmationSnapshot) -> some View {
        if let funnel = snapshot.v4Summary?.candidateFunnel {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    StatusPill(text: "清洗活动 \(funnel.clearActivities)", kind: .neutral)
                    StatusPill(text: "事件信号 \(funnel.eventSignals)", kind: .neutral)
                    StatusPill(text: "可提问 \(funnel.eligibleQuestions)", kind: .neutral)
                    StatusPill(text: "待确认 \(funnel.pending)", kind: funnel.pending > 0 ? .warning : .ok)
                    StatusPill(text: "可报告 \(funnel.reportable)", kind: .ok)
                }
                .accessibilityElement(children: .combine)

                if funnel.filterReasonCounts.isEmpty {
                    Text("当前没有按原因拆分的硬过滤计数。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(
                        funnel.filterReasonCounts.sorted { left, right in
                            left.value == right.value ? left.key < right.key : left.value > right.value
                        },
                        id: \.key
                    ) { reason, count in
                        HStack {
                            Text(ActivityConfirmationFilterReasonPresentation.label(for: reason))
                            Spacer()
                            Text("\(count)")
                                .monospacedDigit()
                                .foregroundStyle(.secondary)
                        }
                        .font(.caption)
                        .accessibilityElement(children: .combine)
                    }
                }
                Text("只显示 content-free 计数，不显示被过滤内容。")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        } else {
            Text("当前 Core 使用兼容契约，尚未提供确认漏斗与过滤原因计数。")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    private var activityConfirmationImpactSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            confirmationImpactLine(
                systemImage: "checkmark.circle",
                title: "一次回答处理整组线索",
                detail: "同一现实问题只回答一次；系统会原子更新这张卡里的全部来源，不做部分提交。"
            )
            Divider()
            confirmationImpactLine(
                systemImage: "slider.horizontal.3",
                title: "每组反馈最多校准一次",
                detail: "否定或纠错只按 App、事件类型、截图区域和可操作性等内容无关特征影响未来判断，不会按组内线索数重复计数。"
            )
            Divider()
            confirmationImpactLine(
                systemImage: "arrow.down.right.circle",
                title: "一次先降置信度，两次才自动抑制",
                detail: "第一次同类负反馈仍会再次询问；相同原因累计两次后，才会自动挡住同类噪音。"
            )
            Divider()
            confirmationImpactLine(
                systemImage: "text.badge.xmark",
                title: "不按公司关键词永久屏蔽",
                detail: "不会因为公司名或正文关键词，把未来真实活动一并屏蔽；正反馈也只修正当前问题。"
            )
        }
    }

    private func confirmationImpactLine(systemImage: String, title: String, detail: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: systemImage)
                .foregroundStyle(Color.accentColor)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.callout.weight(.semibold))
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
        }
        .accessibilityElement(children: .combine)
    }

    private var decisionsTab: some View {
        let snapshot = runner.decisionQueueSnapshot
        return ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .center, spacing: 10) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("待决策").font(.title2.bold())
                        Text("系统只把需要你表态的安全摘要放在这里；每张卡都会直接问你该不该记住。")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    HStack(spacing: 6) {
                        StatusPill(text: "raw_access=false", kind: snapshot.rawAccess ? .danger : .ok)
                        StatusPill(text: "先预览", kind: .ok)
                        StatusPill(text: "不自动写", kind: .neutral)
                    }
                }

                HStack(spacing: 10) {
                    Button {
                        Task { await runner.refreshDecisionQueue() }
                    } label: {
                        Label(runner.decisionQueueLoading ? "读取中" : "重新读取", systemImage: "arrow.clockwise")
                    }
                    .disabled(runner.decisionQueueLoading)
                    Spacer()
                    Text(runner.decisionQueueUpdatedLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.bordered)
                .buttonBorderShape(.roundedRectangle(radius: 8))

                if !runner.decisionQueueMessage.isEmpty {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: runner.decisionQueueMessageIsFailure ? "exclamationmark.triangle" : "checkmark.circle")
                            .foregroundStyle(runner.decisionQueueMessageIsFailure ? .orange : .green)
                        Text(runner.decisionQueueMessage)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer()
                    }
                    .padding(12)
                    .background((runner.decisionQueueMessageIsFailure ? Color.orange : Color.green).opacity(0.10))
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                }

                decisionQueueHowToSection

                SettingsSection(title: "队列概览") {
                    VStack(alignment: .leading, spacing: 12) {
                        SettingsRow(
                            systemImage: "checklist",
                            title: snapshot.statusLabel,
                            subtitle: snapshot.summaryLine
                        ) {
                            StatusPill(text: "\(snapshot.itemCount) 条", kind: snapshot.kind)
                        }
                        Divider()
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 126), spacing: 8)], spacing: 8) {
                            MetricCard(title: "待我判断", value: "\(snapshot.itemCount)", subtitle: "需要你表态")
                            MetricCard(title: "长期候选", value: "\(snapshot.memoryCandidateCount)", subtitle: "可能要记住")
                            MetricCard(title: "Review 线索", value: "\(snapshot.reviewSignalCount)", subtitle: "需要改写确认")
                            MetricCard(title: "自动处理", value: "\(snapshot.autoHandledCount)", subtitle: "噪声/重复/不适合")
                            MetricCard(title: "已按规则隐藏", value: "\(snapshot.hiddenByDecisionCount)", subtitle: "你之前处理过")
                            MetricCard(title: "最近决策", value: "\(snapshot.recentDecisions.count)", subtitle: "只保存安全摘要")
                        }
                        .padding(.horizontal, 14)
                        .padding(.bottom, 14)
                    }
                }

                decisionQueueGroupSection(
                    title: "待我判断：长期记忆候选",
                    subtitle: "这些内容可能值得记住，但系统不会替你决定；写入前仍必须先预览。",
                    items: snapshot.memoryItems,
                    emptyText: "当前没有待处理的长期记忆候选。"
                )

                decisionQueueGroupSection(
                    title: "待我判断：Review 线索",
                    subtitle: "这些线索不是可直接保存的记忆；如果要记住，需要先改成一句稳定事实。",
                    items: snapshot.reviewItems,
                    emptyText: "当前没有待处理的 Review 线索。"
                )

                if !snapshot.autoHandledItems.isEmpty {
                    decisionQueueAutoHandledSection(snapshot)
                }

                if !snapshot.recentDecisions.isEmpty {
                    decisionQueueRecentSection(snapshot.recentDecisions)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task {
            await runner.refreshDecisionQueueIfNeeded()
        }
    }

    private var decisionQueueHowToSection: some View {
        SettingsSection(title: "你要判断什么") {
            VStack(spacing: 0) {
                SettingsRow(
                    systemImage: "quote.bubble",
                    title: "系统识别到了什么",
                    subtitle: "每张卡先用一句话说明候选内容，只展示安全摘要，不展示截图或原始 OCR。"
                ) {
                    StatusPill(text: "安全摘要", kind: .ok)
                }
                Divider().padding(.leading, 52)
                SettingsRow(
                    systemImage: "questionmark.bubble",
                    title: "为什么问你",
                    subtitle: "只有系统不确定该不该写进长期记忆时，才会把原因写明让你选。"
                ) {
                    StatusPill(text: "人工判断", kind: .warning)
                }
                Divider().padding(.leading, 52)
                SettingsRow(
                    systemImage: "checklist.checked",
                    title: "你可以怎么答",
                    subtitle: "选择记住、改写后记住、仅本次参考，或让同类内容以后少问。"
                ) {
                    StatusPill(text: "选择题", kind: .neutral)
                }
            }
        }
    }

    private func decisionQueueGroupSection(title: String, subtitle: String, items: [DecisionQueueItem], emptyText: String) -> some View {
        SettingsSection(title: title, footer: subtitle) {
            VStack(alignment: .leading, spacing: 10) {
                if items.isEmpty {
                    Text(emptyText)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(14)
                } else {
                    ForEach(Array(items.prefix(40))) { item in
                        DecisionQueueItemRow(
                            item: item,
                            directWrite: {
                                decisionQueueWriteRequest = .directWrite(item: item)
                            },
                            rewriteWrite: {
                                decisionQueueWriteRequest = .rewriteWrite(item: item)
                            },
                            contextOnly: {
                                Task { await runner.recordDecisionQueueAction(item: item, action: "context-only") }
                            },
                            neverWrite: {
                                Task { await runner.recordDecisionQueueAction(item: item, action: "never-write") }
                            }
                        )
                        .disabled(runner.decisionQueueLoading)
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 14)
                }
            }
        }
    }

    private func decisionQueueAutoHandledSection(_ snapshot: DecisionQueueSnapshot) -> some View {
        SettingsSection(title: "系统已自动处理", footer: "这里不展示原文，只告诉你系统为什么没有把它们放进主决策列表。") {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(snapshot.autoHandledItems) { item in
                    HStack(spacing: 10) {
                        Image(systemName: item.systemImage)
                            .foregroundStyle(.secondary)
                            .frame(width: 20)
                        Text(item.label)
                            .font(.callout.weight(.medium))
                        Spacer()
                        StatusPill(text: "\(item.count) 条", kind: .neutral)
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    if item.id != snapshot.autoHandledItems.last?.id {
                        Divider().padding(.leading, 52)
                    }
                }
            }
            .padding(.vertical, 6)
        }
    }

    private func decisionQueueRecentSection(_ decisions: [DecisionQueueDecision]) -> some View {
        SettingsSection(title: "最近处理") {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(decisions.prefix(8)) { decision in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: decision.systemImage)
                            .foregroundStyle(decision.kind.color)
                            .frame(width: 20)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(decision.titleLine)
                                .font(.callout.weight(.medium))
                            Text(decision.safeSummary)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                    }
                }
            }
            .padding(14)
        }
    }

    private var memoryTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .center, spacing: 10) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("记忆治理").font(.title2.bold())
                        Text("历史台账只读；仅允许对来源明确、属于本人且可追溯的候选生成审批计划。")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    HStack(spacing: 6) {
                        StatusPill(text: "raw_access=false", kind: runner.memoryRawAccessEnabled ? .danger : .ok)
                        StatusPill(text: "历史只读", kind: .ok)
                        StatusPill(text: "不上云", kind: .neutral)
                    }
                }

                HStack(spacing: 10) {
                    Button {
                        Task { await runner.refreshMemoryGovernance() }
                    } label: {
                        Label(runner.memoryGovernanceLoading ? "读取中" : "重新读取", systemImage: "arrow.clockwise")
                    }
                    .disabled(runner.memoryGovernanceLoading)
                    Button {
                        runner.openMemoryLedger()
                    } label: {
                        Label("打开台账", systemImage: "book.closed")
                    }
                    .disabled(runner.memoryGovernanceLoading)
                    Button {
                        Task { await runner.exportMemoryAuditMarkdown() }
                    } label: {
                        Label("导出审计报告", systemImage: "square.and.arrow.down")
                    }
                    .disabled(runner.memoryGovernanceLoading)
                    if !runner.memoryAuditExportPath.isEmpty {
                        Button {
                            runner.openMemoryAuditExport()
                        } label: {
                            Label("打开报告", systemImage: "doc.text")
                        }
                    }
                    Spacer()
                    Text(runner.memoryGovernanceUpdatedLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.bordered)
                .buttonBorderShape(.roundedRectangle(radius: 8))

                if !runner.memoryGovernanceMessage.isEmpty {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: runner.memoryGovernanceMessageIsFailure ? "exclamationmark.triangle" : "checkmark.circle")
                            .foregroundStyle(runner.memoryGovernanceMessageIsFailure ? .orange : .green)
                        Text(runner.memoryGovernanceMessage)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer()
                    }
                    .padding(12)
                    .background((runner.memoryGovernanceMessageIsFailure ? Color.orange : Color.green).opacity(0.10))
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                }

                memoryGovernanceOverviewSection
                memorySafetySection
                memoryLedgerSection
                memoryApprovalCandidatesSection
                memoryApprovalRecordsSection
                memoryAuditSection
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task {
            await runner.refreshMemoryGovernanceIfNeeded()
        }
    }

    private var memoryGovernanceOverviewSection: some View {
        let candidates = runner.memoryApprovalCandidates
        let audit = runner.memoryAuditStatus
        return SettingsSection(title: "治理总览", footer: "医疗可疑项只读提示，不自动删除；Unknown 可写和主体风险用于判断是否需要继续收紧门槛。") {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 118), spacing: 8)], spacing: 8) {
                MetricCard(title: "可写池", value: "\(candidates.eligibleCandidateCount)", subtitle: "eligible")
                MetricCard(title: "复核池", value: "\(candidates.reviewOnlyCandidateCount)", subtitle: "review-only")
                MetricCard(title: "临时降级", value: "\(candidates.ephemeralCandidateCount)", subtitle: "ephemeral")
                MetricCard(title: "医疗风险", value: "\(audit.personalMedicalRiskCount)", subtitle: "只读台账复核")
                MetricCard(title: "主体风险", value: "\(candidates.subjectRiskCount)", subtitle: "不应直写")
                MetricCard(title: "Unknown 可写", value: "\(candidates.unknownEligibleCount)", subtitle: "目标为 0")
                MetricCard(title: "单证据降级", value: "\(candidates.singleEvidenceDemotedCount)", subtitle: "默认观察")
                MetricCard(title: "媒体/文件降级", value: "\(candidates.mediaOrFileEphemeralCount)", subtitle: "默认临时")
            }
            .padding(14)
        }
    }

    private var memorySafetySection: some View {
        SettingsSection(title: "安全边界") {
            SettingsRow(
                systemImage: "lock.shield",
                title: "原始访问",
                subtitle: "只读取台账状态和审计安全摘要；不读取截图、OCR、SQLite 原始活动表或 HA package 正文。"
            ) {
                StatusPill(text: runner.memoryRawAccessEnabled ? "异常" : "关闭", kind: runner.memoryRawAccessEnabled ? .danger : .ok)
            }
            Divider().padding(.leading, 52)
            SettingsRow(
                systemImage: "trash.slash",
                title: "写入动作",
                subtitle: "历史台账不再改写或删除；新候选必须满足本人主体、明确来源和人工确认，再由受控 writer 执行。"
            ) {
                StatusPill(text: runner.memoryDestructiveActionsEnabled ? "异常" : "受控", kind: runner.memoryDestructiveActionsEnabled ? .danger : .ok)
            }
            Divider().padding(.leading, 52)
            SettingsRow(
                systemImage: "text.badge.checkmark",
                title: "展示字段",
                subtitle: "只展示 safe_summary、issue type、recommendation 和审批原因；不展示 memory id、metadata 原文、HA package 正文或长文本。"
            ) {
                StatusPill(text: "安全摘要", kind: .ok)
            }
        }
    }

    private var memoryLedgerSection: some View {
        let ledger = runner.effectiveMemoryLedgerStatus
        return SettingsSection(title: "历史台账（只读）") {
            VStack(alignment: .leading, spacing: 12) {
                SettingsRow(
                    systemImage: "book.pages",
                    title: "AGI 长期记忆台账",
                    subtitle: ledger.summaryLine
                ) {
                    StatusPill(text: ledger.statusLabel, kind: ledger.kind)
                }
                Divider()
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 118), spacing: 8)], spacing: 8) {
                    MetricCard(title: "历史记忆", value: "\(ledger.currentMemoryCount)", subtitle: "只读盘点")
                    MetricCard(title: "历史清单", value: "\(ledger.entryCount)", subtitle: "不作为实时规则源")
                    MetricCard(title: "旧可同步", value: "\(ledger.canSyncCount)", subtitle: "仅供审计，不执行")
                    MetricCard(title: "旧待处理", value: "\(ledger.pendingActionCount)", subtitle: "仅供人工查证")
                    MetricCard(title: "人工查找", value: "\(ledger.needsManualLookupCount)", subtitle: "需要人工定位")
                    MetricCard(title: "自由新增", value: "\(ledger.unmatchedNewLineCount)", subtitle: "未匹配新增行")
                    MetricCard(title: "复杂改动", value: "\(ledger.complexChangeCount)", subtitle: "不自动处理")
                }
                .padding(.horizontal, 14)
                .padding(.bottom, 14)
            }
        }
    }

    private var memoryApprovalCurrentSection: some View {
        SettingsSection(title: "当前记忆") {
            VStack(alignment: .leading, spacing: 10) {
                SettingsRow(
                    systemImage: "memorychip",
                    title: "AGI 已写入记忆",
                    subtitle: runner.memoryApprovalStatus.currentMemorySummary
                ) {
                    StatusPill(text: "\(runner.memoryApprovalStatus.currentMemories.count) 条", kind: .neutral)
                }
                if runner.memoryApprovalStatus.currentMemories.isEmpty {
                    Divider()
                    Text("当前没有可从 app 审批处理的 PAM 长期记忆。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 14)
                        .padding(.bottom, 14)
                } else {
                    Divider()
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(runner.memoryApprovalStatus.currentMemories.prefix(12)) { item in
                            VStack(alignment: .leading, spacing: 8) {
                                HStack(alignment: .top, spacing: 8) {
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text(item.safeSummary)
                                            .font(.callout)
                                            .fixedSize(horizontal: false, vertical: true)
                                        HStack(spacing: 6) {
                                            StatusPill(text: item.subjectScopeLabel, kind: .neutral)
                                            StatusPill(text: item.sourceCategoryLabel, kind: .neutral)
                                            StatusPill(text: item.syncStatusLabel, kind: item.canRewrite ? .ok : .warning)
                                        }
                                    }
                                    Spacer()
                                    HStack(spacing: 6) {
                                        Button {
                                            memoryApprovalRequest = .rewrite(memory: item)
                                        } label: {
                                            Image(systemName: "pencil")
                                        }
                                        .help("预览改写这条记忆")
                                        .disabled(!item.canRewrite || runner.memoryGovernanceLoading)
                                        Button {
                                            memoryApprovalRequest = .delete(memory: item)
                                        } label: {
                                            Image(systemName: "trash")
                                        }
                                        .help("预览删除这条记忆")
                                        .disabled(!item.canDelete || runner.memoryGovernanceLoading)
                                    }
                                    .buttonStyle(.bordered)
                                }
                            }
                            .padding(10)
                            .background(Color(NSColor.textBackgroundColor))
                            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.bottom, 14)
                }
            }
        }
    }

    private var memoryApprovalCandidatesSection: some View {
        let candidates = runner.memoryApprovalCandidates
        return SettingsSection(title: "候选审批") {
            VStack(alignment: .leading, spacing: 12) {
                SettingsRow(
                    systemImage: "checklist",
                    title: "HA 安全候选",
                    subtitle: candidates.summaryLine
                ) {
                    StatusPill(text: candidates.statusLabel, kind: candidates.kind)
                }
                Divider()
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 126), spacing: 8)], spacing: 8) {
                    MetricCard(title: "Eligible", value: "\(candidates.eligibleCandidateCount)", subtitle: "可直接预览写入")
                    MetricCard(title: "Review only", value: "\(candidates.reviewOnlyCandidateCount)", subtitle: "人工改写后提升")
                    MetricCard(title: "Ephemeral", value: "\(candidates.ephemeralCandidateCount)", subtitle: "永不写入")
                    MetricCard(title: "总候选", value: "\(candidates.totalCandidateCount)", subtitle: "安全摘要级候选")
                }
                .padding(.horizontal, 14)
                if candidates.allDisplayItems.isEmpty {
                    Text("当前没有可展示的候选记忆。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 14)
                        .padding(.bottom, 14)
                } else {
                    Divider()
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(candidates.allDisplayItems.prefix(24)) { item in
                            MemoryCandidateRow(item: item) {
                                memoryApprovalRequest = .approve(candidate: item, packageID: candidates.packageID)
                            } promote: {
                                memoryApprovalRequest = .promote(candidate: item, packageID: candidates.packageID)
                            }
                            .disabled(runner.memoryGovernanceLoading)
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.bottom, 14)
                }
            }
        }
    }

    private var memoryApprovalRecordsSection: some View {
        SettingsSection(title: "审批记录") {
            VStack(alignment: .leading, spacing: 10) {
                SettingsRow(
                    systemImage: "clock.arrow.circlepath",
                    title: "最近本机审批",
                    subtitle: runner.memoryApprovalStatus.recordSummary
                ) {
                    StatusPill(text: "\(runner.memoryApprovalStatus.records.count) 条", kind: .neutral)
                }
                if !runner.memoryApprovalStatus.records.isEmpty {
                    Divider()
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(runner.memoryApprovalStatus.records.prefix(8)) { record in
                            HStack(alignment: .top, spacing: 10) {
                                Image(systemName: record.status == "applied" ? "checkmark.circle" : "exclamationmark.triangle")
                                    .foregroundStyle(record.status == "applied" ? .green : .orange)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(record.titleLine)
                                        .font(.callout.weight(.medium))
                                    Text(record.message)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                Spacer()
                            }
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.bottom, 14)
                }
            }
        }
    }

    private var memoryAuditSection: some View {
        let audit = runner.memoryAuditStatus
        return SettingsSection(title: "只读审计") {
            VStack(alignment: .leading, spacing: 12) {
                SettingsRow(
                    systemImage: "magnifyingglass.circle",
                    title: "审计结果",
                    subtitle: audit.summaryLine
                ) {
                    StatusPill(text: audit.statusLabel, kind: audit.kind)
                }
                Divider()
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 126), spacing: 8)], spacing: 8) {
                    MetricCard(title: "检查数量", value: "\(audit.itemsChecked)", subtitle: "本次只读扫描")
                    MetricCard(title: "可疑项", value: "\(audit.itemCount)", subtitle: "仅展示安全摘要")
                    MetricCard(title: "Issue 类型", value: "\(audit.issueRows.count)", subtitle: audit.issueSummaryLine)
                }
                .padding(.horizontal, 14)
                if !audit.issueRows.isEmpty {
                    Divider()
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(audit.issueRows) { issue in
                            HStack {
                                Text(issue.label)
                                    .font(.callout)
                                Spacer()
                                StatusPill(text: "\(issue.count)", kind: .warning)
                            }
                        }
                    }
                    .padding(.horizontal, 14)
                }
                if !audit.items.isEmpty {
                    Divider()
                    VStack(alignment: .leading, spacing: 10) {
                        Text("可疑项安全摘要")
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(.secondary)
                        ForEach(Array(audit.items.prefix(50))) { item in
                            VStack(alignment: .leading, spacing: 5) {
                                HStack {
                                    Text(item.issueLabel)
                                        .font(.callout.weight(.medium))
                                    Spacer()
                                    StatusPill(text: "只读", kind: .neutral)
                                }
                                Text(item.safeSummary)
                                    .font(.callout)
                                    .fixedSize(horizontal: false, vertical: true)
                                if !item.recommendation.isEmpty {
                                    Text(item.recommendation)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            .padding(10)
                            .background(Color(NSColor.textBackgroundColor))
                            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.bottom, 14)
                } else {
                    Text("当前没有可疑长期记忆项。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 14)
                        .padding(.bottom, 14)
                }
            }
        }
    }

    private var exportStatusBar: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: runner.exportStatusSystemImage)
                .foregroundStyle(runner.exportStatusColor)
            Text(runner.markdownExportMessage)
                .font(.callout)
                .lineLimit(2)
                .truncationMode(.middle)
                .textSelection(.enabled)
            Spacer()
            if !runner.lastMarkdownExportPath.isEmpty {
                Button {
                    runner.openLastMarkdownExport()
                } label: {
                    Label("打开", systemImage: "doc.text")
                }
                Button {
                    runner.revealLastMarkdownExport()
                } label: {
                    Label("Finder", systemImage: "folder")
                }
            }
        }
        .padding(12)
        .background(Color.green.opacity(0.10))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var privacyTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                SettingsSection(title: "边界") {
                    SettingsRow(
                        systemImage: "rectangle.on.rectangle",
                        title: "会记录",
                        subtitle: "已授权屏幕的 keyframe 和清洗后的活动摘要。"
                    ) {
                        StatusPill(text: "本机", kind: .ok)
                    }
                    Divider().padding(.leading, 52)
                    SettingsRow(
                        systemImage: "hand.raised",
                        title: "不会记录",
                        subtitle: "音频、键盘输入、剪贴板、聊天原文、长期原始截图或短视频。"
                    ) {
                        StatusPill(text: "不保留原文", kind: .ok)
                    }
                    Divider().padding(.leading, 52)
                    SettingsRow(
                        systemImage: "externaldrive",
                        title: "本地数据",
                        subtitle: "默认只保留今天和昨天的清洗后活动数据；更早内容自动过期删除；raw_access=false。"
                    ) {
                        Button("打开") { runner.openDataFolder() }
                    }
                    Divider().padding(.leading, 52)
                    SettingsRow(
                        systemImage: "arrow.triangle.2.circlepath",
                        title: "旧数据自动清理",
                        subtitle: runner.statusSnapshot.hygieneCleanup.summary
                    ) {
                        StatusPill(
                            text: runner.statusSnapshot.hygieneCleanup.label,
                            kind: runner.statusSnapshot.hygieneCleanup.kind
                        )
                    }
                    Divider().padding(.leading, 52)
                    SettingsRow(
                        systemImage: "tray.and.arrow.up",
                        title: "HA 自动包",
                        subtitle: runner.statusSnapshot.haAutomation.summary
                    ) {
                        StatusPill(
                            text: runner.statusSnapshot.haAutomation.label,
                            kind: runner.statusSnapshot.haAutomation.kind
                        )
                    }
                    Divider().padding(.leading, 52)
                    SettingsRow(
                        systemImage: "paperplane",
                        title: "记忆写入汇报",
                        subtitle: runner.statusSnapshot.haAutomation.memoryReportLine
                    ) {
                        StatusPill(
                            text: runner.statusSnapshot.haAutomation.memoryReportFailedCount > 0 ? "失败" : (runner.statusSnapshot.haAutomation.memoryReportPendingCount > 0 ? "待补投" : "正常"),
                            kind: runner.statusSnapshot.haAutomation.memoryReportFailedCount > 0 || runner.statusSnapshot.haAutomation.memoryReportPendingCount > 0 ? .warning : .ok
                        )
                    }
                }

                privacyPolicySection

                SettingsSection(title: "权限") {
                    permissionRow
                }

                SettingsSection(title: "数据卫生检查", footer: "这里只运行只读检查，不删除、不清理、不移动任何文件。") {
                    SettingsRow(
                        systemImage: "checklist",
                        title: "只读卫生检查",
                        subtitle: runner.backupHygieneMessage.isEmpty ? "检查旧备份、HA cache、导出缓存和 runtime 权限候选；不会执行删除。" : runner.backupHygieneMessage
                    ) {
                        Button {
                            Task { await runner.runBackupHygieneCheck() }
                        } label: {
                            Label(runner.backupHygieneRunning ? "检查中" : "运行", systemImage: "checkmark.shield")
                        }
                        .disabled(runner.backupHygieneRunning)
                    }
                }



                SettingsSection(title: "版本") {
                    SettingsRow(
                        systemImage: "shippingbox",
                        title: "当前应用",
                        subtitle: AppBuildInfo.current.detailText
                    ) {
                        StatusPill(
                            text: AppBuildInfo.current.integrityLabel,
                            kind: AppBuildInfo.current.gitDirty ? .warning : .ok
                        )
                    }
                }

                SettingsSection(title: "危险操作", footer: "删除会先要求 Touch ID / 本机密码确认。不会通过网页确认码。") {
                    VStack(alignment: .leading, spacing: 12) {
                        Picker("范围", selection: $deletePreset) {
                            ForEach(DeletePreset.allCases) { preset in
                                Text(preset.title).tag(preset)
                            }
                        }
                        .pickerStyle(.segmented)
                        .onChange(of: deletePreset) { _, _ in
                            deleteArmed = false
                        }
                        Text(deleteRangeSummary)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                        Toggle("我知道此操作不可撤销", isOn: $deleteArmed)
                            .toggleStyle(.checkbox)
                        HStack {
                            Spacer()
                            Button(role: .destructive) {
                                let range = deleteRange(for: deletePreset)
                                Task {
                                    await runner.deleteRange(from: range.from, to: range.to, reason: "user_deleted_from_app")
                                }
                            } label: {
                                Label("本机确认并删除", systemImage: "trash")
                            }
                            .disabled(!deleteArmed)
                        }
                        ResultPanel(title: "删除结果", text: runner.deleteText, emptyText: "选择删除范围后再执行删除。")
                            .frame(minHeight: 120)
                    }
                    .padding(14)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var privacyPolicySection: some View {
        SettingsSection(title: "隐私规则预览", footer: "只读查看和预览；本版不新增、不删除、不停用防火墙规则。") {
            VStack(alignment: .leading, spacing: 12) {
                SettingsRow(
                    systemImage: "shield.lefthalf.filled",
                    title: "规则边界",
                    subtitle: runner.privacyPolicyStatus.summary
                ) {
                    HStack(spacing: 6) {
                        StatusPill(text: runner.privacyPolicyStatus.statusLabel, kind: runner.privacyPolicyStatus.kind)
                        StatusPill(text: "写规则=false", kind: runner.privacyPolicyStatus.ruleWriteActions ? .danger : .ok)
                        Button {
                            Task { await runner.refreshPrivacyPolicyStatus() }
                        } label: {
                            Label("刷新", systemImage: "arrow.clockwise")
                        }
                    }
                }
                Divider()
                HStack(spacing: 10) {
                    MetricCard(title: "硬保护 App", value: "\(runner.privacyPolicyStatus.hardBlockAppCount)", subtitle: runner.privacyPolicyStatus.hardBlockExamples)
                    MetricCard(title: "沟通 App", value: "\(runner.privacyPolicyStatus.communicationAppCount)", subtitle: runner.privacyPolicyStatus.communicationExamples)
                    MetricCard(title: "高风险域名", value: "\(runner.privacyPolicyStatus.highRiskDomainHintCount)", subtitle: runner.privacyPolicyStatus.highRiskDomainExamples)
                }
                Divider()
                SettingsRow(
                    systemImage: "slider.horizontal.3",
                    title: "隐私档位",
                    subtitle: "切换前先做 profile dry-run 预览；不新增、不删除任意 app/domain 规则。"
                ) {
                    HStack(spacing: 6) {
                        Button {
                            Task { await runner.configurePrivacyProfile("strict") }
                        } label: {
                            Label("严格", systemImage: "lock.fill")
                        }
                        .disabled(runner.privacyPolicyLoading || runner.privacyPolicyStatus.profile == "strict")
                        Button {
                            Task { await runner.configurePrivacyProfile("personal_high_precision") }
                        } label: {
                            Label("高精度", systemImage: "scope")
                        }
                        .disabled(runner.privacyPolicyLoading || runner.privacyPolicyStatus.profile == "personal_high_precision")
                    }
                }
                Divider()
                VStack(alignment: .leading, spacing: 10) {
                    Text("只读影响预览")
                        .font(.headline)
                    HStack(spacing: 8) {
                        TextField("App，例如 Safari", text: $privacyPreviewApp)
                            .textFieldStyle(.roundedBorder)
                        TextField("Domain，例如 paypal.example", text: $privacyPreviewDomain)
                            .textFieldStyle(.roundedBorder)
                        Button {
                            Task { await runner.previewPrivacyPolicy(app: privacyPreviewApp, domain: privacyPreviewDomain) }
                        } label: {
                            Label("预览", systemImage: "eye")
                        }
                        .disabled(privacyPreviewApp.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && privacyPreviewDomain.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                    SettingsRow(
                        systemImage: "doc.text.magnifyingglass",
                        title: runner.privacyPolicyPreview.title,
                        subtitle: runner.privacyPolicyPreview.summary
                    ) {
                        HStack(spacing: 6) {
                            StatusPill(text: runner.privacyPolicyPreview.effectLabel, kind: runner.privacyPolicyPreview.kind)
                            StatusPill(text: runner.privacyPolicyPreview.reasonLabel, kind: .neutral)
                        }
                    }
                }
            }
            .padding(14)
        }
    }

    private var rangeSection: some View {
        SettingsSection(title: "时间范围") {
            VStack(alignment: .leading, spacing: 10) {
                Picker("", selection: $rangePreset) {
                    ForEach(ActivityRangePreset.allCases) { preset in
                        Text(preset.title).tag(preset)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .onChange(of: rangePreset) { _, preset in
                    applyRangePreset(preset, refresh: true)
                }
                Text(rangeDisplayText)
                    .font(.footnote.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .padding(12)
        }
    }

    private var primaryRecordingButton: some View {
        Button {
            Task {
                if runner.statusSnapshot.launchAgentRunning {
                    await runner.stopRecordingService()
                } else {
                    await runner.startRecordingService()
                }
            }
        } label: {
            Label(
                runner.statusSnapshot.recordingButtonTitle,
                systemImage: runner.statusSnapshot.launchAgentRunning ? "stop.circle" : "record.circle"
            )
            .frame(maxWidth: .infinity, minHeight: 30)
        }
        .buttonStyle(.borderedProminent)
    }

    private var permissionRow: some View {
        let permissionText: String
        let permissionPill: String
        let permissionKind: StatusKind
        if runner.statusSnapshot.screenRecordingPermission.denied {
            permissionText = runner.statusSnapshot.screenRecordingPermission.summary
            permissionPill = "核心未授权"
            permissionKind = .danger
        } else if runner.statusSnapshot.screenRecordingPermission.observedGranted {
            permissionText = runner.statusSnapshot.screenRecordingPermission.summary
            permissionPill = "Core 正常"
            permissionKind = .ok
        } else if runner.screenRecordingAllowed {
            permissionText = "菜单栏 App 已授权，但记录核心尚无新鲜成功证据。"
            permissionPill = "Core 待确认"
            permissionKind = .warning
        } else {
            permissionText = runner.statusSnapshot.launchAgentRunning
                ? "核心尚无新鲜采集证据；请检查系统授权。"
                : "记录服务未运行，当前无法从后台采集证据判断权限。"
            permissionPill = runner.statusSnapshot.launchAgentRunning ? "待确认" : "未知"
            permissionKind = .warning
        }
        return SettingsRow(
            systemImage: "lock.shield",
            title: "屏幕录制权限",
            subtitle: permissionText
        ) {
            HStack(spacing: 8) {
                StatusPill(
                    text: permissionPill,
                    kind: permissionKind
                )
                Button("系统设置") { runner.openScreenRecordingSettings() }
            }
        }
    }

    private var deleteRangeSummary: String {
        "将删除\(deletePreset.title)内的本地活动，并撤销重叠 HA 包。"
    }

    private var searchQuery: String {
        query.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var rangeDisplayText: String {
        "\(Self.localTimestamp(rangeFrom))  到  \(Self.localTimestamp(rangeTo))"
    }

    private func applyRangePreset(_ preset: ActivityRangePreset, refresh: Bool = false) {
        rangePreset = preset
        switch preset {
        case .last15:
            setRange(minutes: 15, label: "最近 15 分钟", refresh: refresh)
        case .last60:
            setRange(minutes: 60, label: "最近 1 小时", refresh: refresh)
        case .today:
            setRangeToday(refresh: refresh)
        case .yesterday:
            setRangeYesterday(refresh: refresh)
        }
    }

    private func setRange(minutes: Int, label: String, refresh: Bool = false) {
        let end = Date()
        let start = end.addingTimeInterval(-Double(minutes) * 60)
        rangeFrom = Self.isoFormatter.string(from: start)
        rangeTo = Self.isoFormatter.string(from: end)
        rangeLabel = label
        if refresh {
            refreshRangeContent(from: rangeFrom, to: rangeTo)
        }
    }

    private func setRangeToday(refresh: Bool = false) {
        let range = Self.todayRange()
        rangeFrom = range.from
        rangeTo = range.to
        rangeLabel = "今天"
        if refresh {
            refreshRangeContent(from: range.from, to: range.to)
        }
    }

    private func setRangeYesterday(refresh: Bool = false) {
        let range = Self.yesterdayRange()
        rangeFrom = range.from
        rangeTo = range.to
        rangeLabel = "昨天"
        if refresh {
            refreshRangeContent(from: rangeFrom, to: rangeTo)
        }
    }

    private func refreshRangeContent(from: String, to: String) {
        switch windowState.selectedTab {
        case .review:
            let trimmedQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmedQuery.isEmpty {
                runner.clearSearchResults()
            } else {
                runner.search(query: trimmedQuery, from: from, to: to)
            }
            refreshReviewContent()
        case .today, .reflection, .evolution, .confirmations, .fileHistory, .memoryReview, .systemStatus, .privacy, .settings:
            break
        }
    }

    private func refreshReviewContent() {
        let requestedFrom = rangeFrom
        let requestedTo = rangeTo
        let requestedLabel = rangeLabel
        let requestID = runner.beginDailyReviewRequest()
        Task {
            guard await runner.loadDailyReview(requestID: requestID, from: requestedFrom, to: requestedTo) else { return }
            await runner.refreshAIReview(from: requestedFrom, to: requestedTo, label: requestedLabel)
            await runner.refreshAIConnectorStatus()
        }
    }

    private static func todayRange() -> (from: String, to: String) {
        let calendar = Calendar.current
        let start = calendar.startOfDay(for: Date())
        return (isoFormatter.string(from: start), isoFormatter.string(from: Date()))
    }

    private static func yesterdayRange() -> (from: String, to: String) {
        let calendar = Calendar.current
        let today = calendar.startOfDay(for: Date())
        let start = calendar.date(byAdding: .day, value: -1, to: today) ?? today
        return (isoFormatter.string(from: start), isoFormatter.string(from: today))
    }

    private func deleteRange(for preset: DeletePreset) -> (from: String, to: String) {
        switch preset {
        case .last15:
            return relativeRange(minutes: 15)
        case .last60:
            return relativeRange(minutes: 60)
        case .today:
            return Self.todayRange()
        case .yesterday:
            return Self.yesterdayRange()
        }
    }

    private func relativeRange(minutes: Int) -> (from: String, to: String) {
        let end = Date()
        let start = end.addingTimeInterval(-Double(minutes) * 60)
        return (Self.isoFormatter.string(from: start), Self.isoFormatter.string(from: end))
    }

    private static let isoFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = .current
        return formatter
    }()

    private static let localDisplayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }()

    private static func localTimestamp(_ value: String) -> String {
        return LocalStatusTime.label(value)
    }
}

struct LogoMark: View {
    let size: CGFloat

    var body: some View {
        AGIMark(size: size)
            .shadow(color: .black.opacity(0.12), radius: 4, y: 2)
    }
}

struct MenuBarMark: View {
    let title: String

    var body: some View {
        Text(title)
            .font(.system(size: 11, weight: .medium, design: .monospaced))
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
        .frame(height: 18)
        .accessibilityLabel("AGI \(title)")
    }
}

struct MonitorGlyph: View {
    let size: CGFloat
    let filled: Bool
    let accent: Color

    var body: some View {
        ZStack {
            if filled {
                filledGlyph
            } else {
                outlineGlyph
            }
        }
        .frame(width: size, height: size)
    }

    private var filledGlyph: some View {
        ZStack {
            RoundedRectangle(cornerRadius: unit(14), style: .continuous)
                .fill(color(0x111318))
                .overlay {
                    RoundedRectangle(cornerRadius: unit(13.5), style: .continuous)
                        .stroke(Color.white.opacity(0.10), lineWidth: max(0.5, unit(0.5)))
                }
                .frame(width: unit(52), height: unit(52))
                .position(x: unit(32), y: unit(32))

            keyframeCard(x: 12.5, y: 22, width: 32, height: 22, opacity: 0.18, strokeOpacity: 0.25)
            keyframeCard(x: 15.7, y: 25.2, width: 32, height: 22, opacity: 0.42, strokeOpacity: 0.55)
            keyframeCard(x: 19.5, y: 18.6, width: 32, height: 22, opacity: 1, strokeOpacity: 0.92)

            filledViewfinder
            filledActivityLines
            filledLens
        }
    }

    private var outlineGlyph: some View {
        ZStack {
            RoundedRectangle(cornerRadius: unit(3.6), style: .continuous)
                .stroke(accent, lineWidth: max(1.1, unit(2.2)))
                .frame(width: unit(36), height: unit(24))
                .position(x: unit(32), y: unit(32))

            Path { path in
                path.move(to: point(18.5, 26))
                path.addLine(to: point(18.5, 23.5))
                path.addLine(to: point(21, 23.5))

                path.move(to: point(45.5, 26))
                path.addLine(to: point(45.5, 23.5))
                path.addLine(to: point(43, 23.5))

                path.move(to: point(18.5, 38))
                path.addLine(to: point(18.5, 40.5))
                path.addLine(to: point(21, 40.5))

                path.move(to: point(45.5, 38))
                path.addLine(to: point(45.5, 40.5))
                path.addLine(to: point(43, 40.5))
            }
            .stroke(accent, style: StrokeStyle(lineWidth: max(1.1, unit(2.6)), lineCap: .round, lineJoin: .round))

            Circle()
                .stroke(accent, lineWidth: max(0.9, unit(1.8)))
                .frame(width: unit(4.8), height: unit(4.8))
                .position(x: unit(44.5), y: unit(39))
            Circle()
                .fill(accent)
                .frame(width: unit(2), height: unit(2))
                .position(x: unit(44.5), y: unit(39))
        }
    }

    private func keyframeCard(
        x: CGFloat,
        y: CGFloat,
        width: CGFloat,
        height: CGFloat,
        opacity: Double,
        strokeOpacity: Double
    ) -> some View {
        RoundedRectangle(cornerRadius: unit(3.6), style: .continuous)
            .fill(color(0xE8EEF5, opacity: opacity))
            .overlay {
                RoundedRectangle(cornerRadius: unit(3.6), style: .continuous)
                    .stroke(color(opacity == 1 ? 0xFFFFFF : 0xE8EEF5, opacity: strokeOpacity), lineWidth: max(0.4, unit(0.4)))
            }
            .frame(width: unit(width), height: unit(height))
            .position(x: unit(x + width / 2), y: unit(y + height / 2))
    }

    private var filledViewfinder: some View {
        Path { path in
            path.move(to: point(22.6, 25))
            path.addLine(to: point(22.6, 22.7))
            path.addLine(to: point(25, 22.7))

            path.move(to: point(48.5, 25))
            path.addLine(to: point(48.5, 22.7))
            path.addLine(to: point(46, 22.7))

            path.move(to: point(22.6, 34.5))
            path.addLine(to: point(22.6, 36.8))
            path.addLine(to: point(25, 36.8))

            path.move(to: point(48.5, 34.5))
            path.addLine(to: point(48.5, 36.8))
            path.addLine(to: point(46, 36.8))
        }
        .stroke(color(0x0A0D14, opacity: 0.65), style: StrokeStyle(lineWidth: max(0.75, unit(0.95)), lineCap: .round, lineJoin: .round))
    }

    private var filledActivityLines: some View {
        ZStack {
            line(x: 26.5, y: 27.0, width: 18)
            line(x: 26.5, y: 29.4, width: 18)
            line(x: 26.5, y: 31.8, width: 13)
        }
    }

    private var filledLens: some View {
        ZStack {
            Circle()
                .fill(color(0x67E8F9, opacity: 0.35))
                .frame(width: unit(9.2), height: unit(9.2))
            Circle()
                .fill(color(0x0A0D14))
                .frame(width: unit(6.4), height: unit(6.4))
            Circle()
                .fill(color(0x0E1117))
                .frame(width: unit(5.2), height: unit(5.2))
            Circle()
                .fill(color(0x67E8F9))
                .frame(width: unit(3.4), height: unit(3.4))
            Circle()
                .fill(Color.white.opacity(0.6))
                .frame(width: unit(1), height: unit(1))
                .position(x: unit(31.3), y: unit(30.3))
        }
        .frame(width: unit(64), height: unit(64))
        .position(x: unit(47), y: unit(35.5))
    }

    private func line(x: CGFloat, y: CGFloat, width: CGFloat) -> some View {
        RoundedRectangle(cornerRadius: unit(0.7), style: .continuous)
            .fill(color(0x0A0D14, opacity: 0.22))
            .frame(width: unit(width), height: max(1, unit(1.4)))
            .position(x: unit(x + width / 2), y: unit(y + 0.7))
    }

    private func unit(_ value: CGFloat) -> CGFloat {
        value / 64 * size
    }

    private func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
        CGPoint(x: unit(x), y: unit(y))
    }

    private func color(_ hex: UInt32, opacity: Double = 1) -> Color {
        Color(
            red: Double((hex >> 16) & 0xff) / 255,
            green: Double((hex >> 8) & 0xff) / 255,
            blue: Double(hex & 0xff) / 255
        )
        .opacity(opacity)
    }
}

struct SettingsSection<Content: View>: View {
    let title: String
    let footer: String?
    let content: Content

    init(title: String, footer: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.footer = footer
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.headline)
            VStack(spacing: 0) {
                content
            }
            .background(Color(NSColor.controlBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .stroke(Color.primary.opacity(0.07))
            }
            if let footer {
                Text(footer)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct SettingsRow<Trailing: View>: View {
    let systemImage: String
    let title: String
    let subtitle: String
    let trailing: Trailing

    init(systemImage: String, title: String, subtitle: String, @ViewBuilder trailing: () -> Trailing) {
        self.systemImage = systemImage
        self.title = title
        self.subtitle = subtitle
        self.trailing = trailing()
    }

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: systemImage)
                .font(.title3)
                .foregroundStyle(.secondary)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.body)
                Text(subtitle)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 16)
            trailing
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }
}

struct MetricCard: View {
    let title: String
    let value: String
    let subtitle: String

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.title2.monospacedDigit().weight(.semibold))
                .minimumScaleFactor(0.7)
            Text(subtitle)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, minHeight: 82, alignment: .leading)
        .padding(10)
        .background(Color(NSColor.textBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

struct ActivityRollupCompactList: View {
    let title: String
    let rows: [ActivitySessionRollup]
    let emptyText: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            if rows.isEmpty {
                Text(emptyText)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(rows.prefix(6)) { row in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(row.label)
                                .font(.callout.weight(.medium))
                                .lineLimit(1)
                                .minimumScaleFactor(0.75)
                            Spacer(minLength: 8)
                            Text(row.shareLabel)
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                        ProgressView(value: min(max(row.share, 0), 1))
                            .progressViewStyle(.linear)
                        Text(row.durationLabel)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .padding(10)
        .background(Color(NSColor.textBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

struct MemoryCandidateRow: View {
    let item: MemoryApprovalCandidate
    let approve: () -> Void
    let promote: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    StatusPill(text: item.eligibilityLabel, kind: item.eligibilityKind)
                    StatusPill(text: item.candidateType, kind: .neutral)
                    StatusPill(text: item.subjectScopeLabel, kind: item.subjectKind)
                    if item.evidenceCount > 1 {
                        StatusPill(text: "\(item.evidenceCount) 证据", kind: .neutral)
                    }
                }
                Text(item.safeSummary)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                if !item.whyKeep.isEmpty {
                    Text(item.whyKeep)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !item.disabledReason.isEmpty {
                    Text(item.disabledReason)
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 12)
            VStack(spacing: 6) {
                if item.writeEligibility == "eligible" {
                    Button {
                        approve()
                    } label: {
                        Label("审批写入", systemImage: "checkmark.circle")
                    }
                    .disabled(!item.canApprove)
                } else if item.writeEligibility == "review_only" {
                    Button {
                        promote()
                    } label: {
                        Label("改写提升", systemImage: "square.and.pencil")
                    }
                    .disabled(!item.canPromote)
                } else {
                    Button {} label: {
                        Label("不写入", systemImage: "nosign")
                    }
                    .disabled(true)
                }
            }
            .buttonStyle(.bordered)
        }
        .padding(10)
        .background(Color(NSColor.textBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

struct ConfirmationDisclosurePanel<Content: View>: View {
    let title: String
    let systemImage: String
    let badge: String?
    @Binding var isExpanded: Bool
    let content: Content

    init(
        title: String,
        systemImage: String,
        badge: String? = nil,
        isExpanded: Binding<Bool>,
        @ViewBuilder content: () -> Content
    ) {
        self.title = title
        self.systemImage = systemImage
        self.badge = badge
        _isExpanded = isExpanded
        self.content = content()
    }

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            content
        } label: {
            HStack(spacing: 9) {
                Image(systemName: systemImage)
                    .foregroundStyle(.secondary)
                    .frame(width: 20)
                Text(title)
                    .font(.callout.weight(.semibold))
                Spacer(minLength: 8)
                if let badge {
                    Text(badge)
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .background(Color(NSColor.controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Color.primary.opacity(0.06))
        }
        .accessibilityHint(isExpanded ? "收起这一部分" : "展开这一部分")
    }
}

struct ActivityConfirmationCardDraft {
    var pendingNegativeAnswer: ActivityConfirmationAnswer?
    var selectedReason: ActivityConfirmationReason?
    var note = ""
    var workstream: String
    var outcome: String
    var noteExpanded = true
    var safeContextExpanded = false
    var businessDetailsExpanded = false
    var businessValidationMessage = ""

    init(group: ActivityConfirmationQuestionGroup) {
        workstream = group.effectiveWorkstream
        outcome = group.effectiveOutcome
    }
}

struct ActivityConfirmationQuestionCard: View {
    let group: ActivityConfirmationQuestionGroup
    @Binding var draft: ActivityConfirmationCardDraft
    let isLoading: Bool
    let isClarifying: Bool
    let clarification: ActivityConfirmationClarification?
    let answer: (ActivityConfirmationAnswer, ActivityConfirmationReason?, String, String, String) -> Void
    let clarify: (String) -> Void

    init(
        group: ActivityConfirmationQuestionGroup,
        draft: Binding<ActivityConfirmationCardDraft>,
        isLoading: Bool,
        isClarifying: Bool,
        clarification: ActivityConfirmationClarification?,
        answer: @escaping (ActivityConfirmationAnswer, ActivityConfirmationReason?, String, String, String) -> Void,
        clarify: @escaping (String) -> Void
    ) {
        self.group = group
        _draft = draft
        self.isLoading = isLoading
        self.isClarifying = isClarifying
        self.clarification = clarification
        self.answer = answer
        self.clarify = clarify
    }

    private var interactionBlocked: Bool {
        isLoading
            || isClarifying
            || group.submissionTarget == nil
            || !ActivityConfirmationCardInteractionPolicy.showsAnswerActions(
                isInformationInsufficient: group.isInformationInsufficient
            )
    }

    private var clarificationSubmissionEnabled: Bool {
        ActivityConfirmationClarificationSubmissionPolicy.isEnabled(
            isLoading: isLoading,
            isClarifying: isClarifying,
            canClarify: group.canClarify
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(group.subjectLabel)
                        .font(.title3.weight(.semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    VStack(alignment: .trailing, spacing: 5) {
                        if group.contractVersion >= 4 {
                            StatusPill(
                                text: group.priority.label,
                                kind: group.priority == .high ? .warning : .neutral
                            )
                        }
                        let stateLabel = ActivityConfirmationVisibilityPolicy.statusLabel(state: group.state)
                        if !stateLabel.isEmpty {
                            StatusPill(text: stateLabel, kind: .neutral)
                        }
                        StatusPill(
                            text: group.sourceCount == 1 ? "1 条线索" : "\(group.sourceCount) 条线索",
                            kind: group.sourceCount > 1 ? .ok : .neutral
                        )
                    }
                }
                if !group.contextLine.isEmpty {
                    Label(group.contextLine, systemImage: "clock")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .accessibilityElement(children: .combine)

            if !group.isInformationInsufficient {
                Text(group.questionDisplay)
                    .font(.headline)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
            }

            HStack(alignment: .top, spacing: 10) {
                RoundedRectangle(cornerRadius: 2, style: .continuous)
                    .fill(Color.accentColor)
                    .frame(width: 3)
                VStack(alignment: .leading, spacing: 3) {
                    Text("判断依据")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    Text(group.evidenceDisplay)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                Spacer(minLength: 4)
            }
            .padding(10)
            .background(Color.accentColor.opacity(0.065))
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .accessibilityElement(children: .combine)
            .accessibilityLabel("判断依据：\(group.evidenceDisplay)")

            if group.hasEvidenceStatus {
                Label(group.evidenceSummary.statusLabel, systemImage: group.evidenceSummary.status.systemImage)
                    .font(.caption)
                    .foregroundStyle(group.evidenceSummary.status.color)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel("截图证据状态：\(group.evidenceSummary.statusLabel)")
            }

            if !ActivityConfirmationCardInteractionPolicy.showsAnswerActions(
                isInformationInsufficient: group.isInformationInsufficient
            ) {
                Label("无需回答；系统只保留这条低打扰线索，等待更具体的对象或结构化证据。", systemImage: "info.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                if !group.cleanedContents.isEmpty {
                    cleanedContentsDisclosure
                }

                if let clarification, !clarification.isEmpty {
                    ActivityConfirmationClarificationView(clarification: clarification)
                }

                if let pendingNegativeAnswer = draft.pendingNegativeAnswer {
                    reasonSelection(for: pendingNegativeAnswer)
                }

                if group.contractVersion >= 4 {
                    businessDetailsSection
                }

                DisclosureGroup(isExpanded: $draft.noteExpanded) {
                    clarificationControls
                        .padding(.top, 6)
                    Text("说明会在本机保留最新一条清洗结果供后续重新梳理；问题处理完后删除，也不算作“发生了”的证据。")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .fixedSize(horizontal: false, vertical: true)
                } label: {
                    Text("补充说明（可选）")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                // Four fixed buttons never need lazy layout, and a lazy grid
                // nested in the confirmation list is exactly the shape that
                // traps SwiftUI in repeated placement updates.
                Grid(horizontalSpacing: 8, verticalSpacing: 8) {
                    GridRow {
                        answerButton(.occurred, systemImage: "checkmark.circle.fill", prominent: true)
                        answerButton(.notOccurred, systemImage: "xmark.circle", prominent: false)
                    }
                    if group.supports(.ignored) {
                        GridRow {
                            answerButton(.ignored, systemImage: "eye.slash.circle", prominent: false)
                        }
                    }
                }
                .buttonBorderShape(.roundedRectangle(radius: 8))

                if !draft.businessValidationMessage.isEmpty {
                    Label(draft.businessValidationMessage, systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if !group.supports(.ignored) {
                    Label(
                        "当前 Core 不支持“忽略该问题”；请同步升级 App 与 Core。系统不会把“忽略”映射成旧版误识别动作。",
                        systemImage: "exclamationmark.triangle"
                    )
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                }

                HStack(spacing: 8) {
                    if group.state == "deferred" {
                        Label("已暂时跳过，可随时在上方确认", systemImage: "hourglass")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        Button {
                            submit(.deferred, reason: nil)
                        } label: {
                            Label(ActivityConfirmationAnswer.deferred.label, systemImage: "hourglass")
                        }
                        .buttonStyle(.plain)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .disabled(interactionBlocked || !group.supports(.deferred))
                        .accessibilityHint("保留这组问题，稍后再决定；不会放行事实或累计校准")
                    }
                    Spacer()
                    if group.isLegacy {
                        Text("旧版 Core：暂按单条确认；升级后可使用截图回看与忽略")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
        }
        .padding(15)
        .background(Color(NSColor.controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 2, style: .continuous)
                .fill(Color.orange.opacity(0.85))
                .frame(width: 3)
                .padding(.vertical, 14)
        }
        .overlay {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(Color.primary.opacity(0.07))
        }
        .accessibilityElement(children: .contain)
    }

    private var clarificationControls: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) {
                clarificationTextField
                    .frame(minWidth: 280)
                clarifyButton
            }

            VStack(alignment: .leading, spacing: 8) {
                clarificationTextField
                HStack {
                    Spacer(minLength: 0)
                    clarifyButton
                }
            }
        }
    }

    private var cleanedContentsDisclosure: some View {
        DisclosureGroup(isExpanded: $draft.safeContextExpanded) {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(group.cleanedContents) { content in
                    cleanedContentRow(content)
                    if content.id != group.cleanedContents.last?.id {
                        Divider()
                    }
                }
            }
            .padding(10)
            .background(Color(NSColor.textBackgroundColor).opacity(0.55))
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .padding(.top, 7)
        } label: {
            Label("查看完整识别内容（已清洗）", systemImage: "text.alignleft")
                .font(.caption.weight(.medium))
        }
        .accessibilityHint(draft.safeContextExpanded ? "收起已清洗识别内容" : "展开已清洗识别内容，不包含原始截图或过滤前 OCR")
    }

    private func cleanedContentRow(_ content: ActivityConfirmationCleanedContent) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(content.text)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Spacer(minLength: 6)
            if content.count > 1 {
                Text("×\(content.count)")
                    .font(.caption2.monospacedDigit().weight(.semibold))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.primary.opacity(0.06))
                    .clipShape(Capsule())
            }
        }
    }

    private var businessDetailsSection: some View {
        DisclosureGroup(isExpanded: $draft.businessDetailsExpanded) {
            VStack(alignment: .leading, spacing: 9) {
                TextField("工作流 / 业务线", text: $draft.workstream)
                    .textFieldStyle(.roundedBorder)
                    .disabled(interactionBlocked)
                    .onChange(of: draft.workstream) { _, value in
                        draft.workstream = String(value.prefix(120))
                        draft.businessValidationMessage = ""
                    }
                    .accessibilityLabel("业务归因")
                    .accessibilityHint("回答发生或没有发生时必填；与业务结果在同一次提交中保存")

                Picker("业务结果", selection: $draft.outcome) {
                    Text("请选择结果").tag("")
                    ForEach(ActivityConfirmationBusinessOutcome.allCases) { value in
                        Text(value.label).tag(value.rawValue)
                    }
                }
                    .disabled(interactionBlocked)
                    .onChange(of: draft.outcome) { _, _ in draft.businessValidationMessage = "" }
                    .accessibilityLabel("业务结果")
                    .accessibilityHint("回答发生或没有发生时必填；没有发生时只允许仅准备或结果未知")

                Text("回答“发生了”或“没有发生”时都要同时确认业务归因和结果；“仅准备”只能和“没有发生”一起提交。系统建议只作为可编辑起点。")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.top, 7)
        } label: {
            HStack(spacing: 7) {
                Label("业务归因与结果", systemImage: "arrow.triangle.branch")
                    .font(.caption.weight(.medium))
                StatusPill(text: group.questionKind.label, kind: .neutral)
            }
        }
        .accessibilityHint(draft.businessDetailsExpanded ? "收起业务归因与结果" : "展开并核对业务归因与结果")
    }

    private var clarificationTextField: some View {
        TextField("补充实际情况，供重新梳理使用", text: $draft.note)
            .textFieldStyle(.roundedBorder)
            .disabled(!clarificationSubmissionEnabled)
            .onSubmit(submitClarification)
            .accessibilityLabel("补充实际情况，可选")
            .accessibilityHint("输入后按回车或确认并重新梳理；本机保存最新一条清洗后的说明，问题处理完后删除，不作为发生证据")
    }

    @ViewBuilder
    private func answerButton(
        _ response: ActivityConfirmationAnswer,
        systemImage: String,
        prominent: Bool
    ) -> some View {
        let button = Button {
            if response == .notOccurred {
                draft.pendingNegativeAnswer = response
                draft.selectedReason = nil
            } else {
                submit(response, reason: nil)
            }
        } label: {
            Label(response.label, systemImage: systemImage)
                .frame(maxWidth: .infinity, minHeight: 20)
        }
        .disabled(interactionBlocked || !group.supports(response))
        .accessibilityHint(accessibilityHint(for: response))

        if prominent {
            button.buttonStyle(.borderedProminent)
        } else {
            button.buttonStyle(.bordered)
        }
    }

    private var clarifyButton: some View {
        Button(action: submitClarification) {
            HStack(spacing: 7) {
                if isClarifying {
                    ProgressView()
                        .controlSize(.small)
                } else {
                    Image(systemName: "arrow.triangle.2.circlepath")
                }
                Text(ActivityConfirmationClarificationSubmissionPolicy.actionLabel(isClarifying: isClarifying))
            }
            .frame(minHeight: 20)
        }
        .buttonStyle(.bordered)
        .disabled(!clarificationSubmissionEnabled)
        .accessibilityHint(group.canClarify
            ? group.clarificationAccessibilityHint
            : "当前 Core 不支持对这张兼容卡重新梳理")
    }

    private func submitClarification() {
        guard clarificationSubmissionEnabled else { return }
        clarify(ActivityConfirmationCommandBuilder.boundedNote(draft.note))
    }

    private func reasonSelection(for response: ActivityConfirmationAnswer) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            VStack(alignment: .leading, spacing: 2) {
                Text("为什么没有发生？")
                    .font(.callout.weight(.semibold))
                Text("选择最接近的一项；这会帮助系统以后少问同类问题。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            VStack(spacing: 6) {
                ForEach(reasonOptions(for: response)) { reason in
                    Button {
                        draft.selectedReason = reason
                        if reason == .preparationOnly {
                            draft.outcome = ActivityConfirmationBusinessOutcome.preparationOnly.rawValue
                        } else {
                            draft.outcome = ActivityConfirmationBusinessOutcome.unknown.rawValue
                        }
                    } label: {
                        HStack(spacing: 9) {
                            Image(systemName: draft.selectedReason == reason ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(draft.selectedReason == reason ? Color.accentColor : Color.secondary)
                            Text(reason.label)
                                .foregroundStyle(.primary)
                            Spacer(minLength: 8)
                        }
                        .contentShape(Rectangle())
                        .padding(.horizontal, 10)
                        .padding(.vertical, 7)
                        .background(draft.selectedReason == reason ? Color.accentColor.opacity(0.08) : Color.clear)
                        .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .disabled(isLoading || isClarifying)
                    .accessibilityValue(draft.selectedReason == reason ? "已选择" : "未选择")
                }
            }

            HStack {
                Button("取消") {
                    draft.pendingNegativeAnswer = nil
                    draft.selectedReason = nil
                }
                .buttonStyle(.borderless)
                Spacer()
                Button(response.label) {
                    submit(response, reason: draft.selectedReason)
                }
                .buttonStyle(.borderedProminent)
                .disabled(draft.selectedReason == nil || isLoading || isClarifying)
                .accessibilityHint("按所选原因提交这组问题")
            }
        }
        .padding(11)
        .background(Color.orange.opacity(0.075))
        .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .stroke(Color.orange.opacity(0.16))
        }
    }

    private func submit(_ response: ActivityConfirmationAnswer, reason: ActivityConfirmationReason?) {
        if ActivityConfirmationStructuredDetailsPolicy.requiresBusinessDetails(
            contractVersion: group.contractVersion
        ) && (response == .occurred || response == .notOccurred) {
            let safeOutcome = ActivityConfirmationCommandBuilder.boundedOutcome(draft.outcome)
            let validDetails = group.businessDetailsAreValid(workstream: draft.workstream, outcome: draft.outcome)
            let semanticOutcomeIsValid: Bool
            if response == .occurred {
                semanticOutcomeIsValid = safeOutcome != ActivityConfirmationBusinessOutcome.preparationOnly.rawValue
            } else {
                let preparationReason = reason == .preparationOnly
                semanticOutcomeIsValid = [
                    ActivityConfirmationBusinessOutcome.preparationOnly.rawValue,
                    ActivityConfirmationBusinessOutcome.unknown.rawValue,
                ].contains(safeOutcome)
                    && preparationReason == (
                        safeOutcome == ActivityConfirmationBusinessOutcome.preparationOnly.rawValue
                    )
            }
            guard validDetails && semanticOutcomeIsValid else {
                draft.businessDetailsExpanded = true
                draft.businessValidationMessage = response == .occurred
                    ? "请填写工作流并选择完成、推进中、受阻或结果未知；如果只是准备，请选择“没有发生”。"
                    : "请填写工作流；如果只是准备请选择“仅准备”，其它未发生原因请选择“结果未知”。"
                return
            }
        }
        draft.businessValidationMessage = ""
        answer(
            response,
            reason,
            ActivityConfirmationCommandBuilder.boundedAnswerNote(draft.note),
            ActivityConfirmationCommandBuilder.boundedWorkstream(draft.workstream),
            ActivityConfirmationCommandBuilder.boundedOutcome(draft.outcome)
        )
    }

    private func reasonOptions(for response: ActivityConfirmationAnswer) -> [ActivityConfirmationReason] {
        ActivityConfirmationReason.options(for: response, legacyCompatible: group.isLegacy)
    }

    private func accessibilityHint(for response: ActivityConfirmationAnswer) -> String {
        switch response {
        case .occurred:
            return "确认这项具体活动发生，一次处理卡片内全部线索"
        case .notOccurred:
            return "展开只适用于没有发生的原因"
        case .ignored:
            return "忽略并关闭这项问题；不表示发生或未发生，也不用于系统校准"
        case .deferred:
            return "保留问题稍后处理，不放行事实"
        }
    }
}

struct ManualActivityEntrySheet: View {
    @ObservedObject var runner: CoreRunner
    @Environment(\.dismiss) private var dismiss
    @State private var safeLabel = ""
    @State private var workstream = ""
    @State private var outcome = ""
    @State private var startedAt = Date().addingTimeInterval(-30 * 60)
    @State private var endedAt = Date()
    @State private var explicitlyConfirmed = false

    private var canSubmit: Bool {
        !runner.manualActivityEntryLoading
            && explicitlyConfirmed
            && !safeLabel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !workstream.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !outcome.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && endedAt >= startedAt
            && endedAt.timeIntervalSince(startedAt) <= 86_400
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("补记系统遗漏")
                    .font(.title2.bold())
                Text("只补记你能确定已经发生的本人活动。内容通过本机 stdin 发送，不写进命令参数。")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Form {
                TextField("活动摘要", text: $safeLabel, prompt: Text("例如：完成客户方案复盘"))
                    .onChange(of: safeLabel) { _, value in
                        safeLabel = String(value.prefix(180))
                    }
                    .accessibilityHint("必填；请写一句不含账号、联系方式或私聊原文的安全摘要")
                TextField("工作流 / 业务线", text: $workstream, prompt: Text("例如：海外渠道开发"))
                    .onChange(of: workstream) { _, value in
                        workstream = String(value.prefix(120))
                    }
                    .accessibilityHint("必填；用于业务归因")
                Picker("业务结果", selection: $outcome) {
                    Text("请选择").tag("")
                    ForEach(ActivityConfirmationBusinessOutcome.allCases) { value in
                        Text(value.label).tag(value.rawValue)
                    }
                }
                .accessibilityHint("必填；用于结果复盘")
                DatePicker("开始时间", selection: $startedAt)
                DatePicker("结束时间", selection: $endedAt)
            }
            .formStyle(.grouped)

            if endedAt < startedAt {
                Label("结束时间不能早于开始时间。", systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            } else if endedAt.timeIntervalSince(startedAt) > 86_400 {
                Label("单次补记最长 24 小时，请缩小时间范围。", systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            Toggle("我确认这是本人已经发生的活动，并理解它会直接成为本机已确认事实", isOn: $explicitlyConfirmed)
                .toggleStyle(.checkbox)
                .accessibilityHint("必须勾选后才能提交；系统不会根据这次补记泛化或读取原始证据")

            if !runner.manualActivityEntryMessage.isEmpty {
                Text(runner.manualActivityEntryMessage)
                    .font(.caption)
                    .foregroundStyle(
                        ActivityConfirmationMessagePolicy.isWarning(runner.manualActivityEntryMessage)
                            ? Color.orange
                            : Color.secondary
                    )
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack {
                Button("取消") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button {
                    Task {
                        let added = await runner.addManualActivity(
                            safeLabel: safeLabel,
                            workstream: workstream,
                            outcome: outcome,
                            startedAt: startedAt,
                            endedAt: endedAt
                        )
                        if added { dismiss() }
                    }
                } label: {
                    if runner.manualActivityEntryLoading {
                        Label("正在补记…", systemImage: "progress.indicator")
                    } else {
                        Label("确认并补记", systemImage: "checkmark.circle.fill")
                    }
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!canSubmit)
            }
        }
        .padding(20)
        .frame(minWidth: 560, idealWidth: 620, minHeight: 500)
        .onAppear { runner.manualActivityEntryMessage = "" }
    }
}

private extension ActivityConfirmationClarificationSeverity {
    var color: Color {
        switch self {
        case .success: return .green
        case .neutral: return .accentColor
        case .warning: return .orange
        }
    }

    var systemImage: String {
        switch self {
        case .success: return "checkmark.circle"
        case .neutral: return "info.circle"
        case .warning: return "exclamationmark.triangle"
        }
    }
}

private extension ActivityConfirmationEvidenceStatus {
    var color: Color {
        switch self {
        case .available: return .secondary
        case .partial: return .orange
        case .unavailable: return .orange
        }
    }

    var systemImage: String {
        switch self {
        case .available: return "photo.badge.checkmark"
        case .partial: return "photo.badge.exclamationmark"
        case .unavailable: return "photo.badge.exclamationmark"
        }
    }
}

struct ActivityConfirmationClarificationView: View {
    let clarification: ActivityConfirmationClarification

    private var severity: ActivityConfirmationClarificationSeverity {
        clarification.severity ?? .warning
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: clarification.systemImage)
                .foregroundStyle(severity.color)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 3) {
                Text(clarification.title)
                    .font(.callout.weight(.semibold))
                if !clarification.message.isEmpty {
                    Text(clarification.message)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !clarification.missingFields.isEmpty {
                    Text("还需要：\(clarification.missingFields.joined(separator: "、"))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if clarification.inputsFieldPresent {
                    Text(clarification.inputsUsed.isEmpty
                        ? "本次没有可重新读取的输入"
                        : "本次已读取：\(clarification.inputsUsed.map(\.label).joined(separator: "、"))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if clarification.hasEvidenceStatus {
                    Text(clarification.evidenceSummary.statusLabel)
                        .font(.caption2)
                        .foregroundStyle(clarification.evidenceSummary.status.color)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !clarification.generatedAtLabel.isEmpty {
                    Text("重新梳理于 \(clarification.generatedAtLabel)")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer(minLength: 4)
        }
        .padding(10)
        .background(severity.color.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .accessibilityElement(children: .combine)
    }
}

struct ActivityConfirmationCalibrationRow: View {
    let calibration: ActivityConfirmationCalibration
    let isLoading: Bool
    let revoke: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "scope")
                .font(.title3)
                .foregroundStyle(.blue)
                .frame(width: 26)
            VStack(alignment: .leading, spacing: 5) {
                Text(calibration.label)
                    .font(.callout.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                Text(calibration.safeSummary)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text(calibration.contextLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            VStack(alignment: .trailing, spacing: 7) {
                StatusPill(
                    text: calibration.statusLabel,
                    kind: calibration.autoSuppressed ? .warning : .neutral
                )
                if !calibration.revoked {
                    Button("撤销") {
                        revoke()
                    }
                    .buttonStyle(.bordered)
                    .disabled(isLoading || !calibration.canRevoke)
                    .accessibilityLabel("撤销校准：\(calibration.label)")
                    .accessibilityHint("停止这条校准继续影响未来的相似判断")
                }
            }
        }
        .padding(12)
        .background(Color(NSColor.textBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Color.primary.opacity(0.06))
        }
    }
}

struct DecisionQueueItemRow: View {
    let item: DecisionQueueItem
    let directWrite: () -> Void
    let rewriteWrite: () -> Void
    let contextOnly: () -> Void
    let neverWrite: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: item.questionSystemImage)
                    .font(.title3)
                    .foregroundStyle(item.questionKind.color)
                    .frame(width: 24)
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(item.decisionQuestionDisplay)
                            .font(.headline)
                            .fixedSize(horizontal: false, vertical: true)
                        if item.mergedItemCount > 1 {
                            StatusPill(text: "已合并 \(item.mergedItemCount) 条", kind: .neutral)
                        }
                        Spacer(minLength: 8)
                        StatusPill(text: item.recommendedActionLabel, kind: item.recommendedActionKind)
                    }
                    VStack(alignment: .leading, spacing: 3) {
                        Text("系统识别到：")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text(item.candidateDisplay)
                            .font(.callout)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    VStack(alignment: .leading, spacing: 3) {
                        Text("为什么问你：")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text(item.uncertaintyDisplay)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !item.writeBlockedExplanation.isEmpty {
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "lock.shield")
                                .foregroundStyle(.secondary)
                            Text(item.writeBlockedExplanation)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if item.hasUserDecision {
                        Text(item.userDecisionLine)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    DisclosureGroup("技术细节") {
                        VStack(alignment: .leading, spacing: 8) {
                            HStack(spacing: 6) {
                                StatusPill(text: item.sourceLabelDisplay, kind: .neutral)
                                StatusPill(text: item.eligibilityLabel, kind: item.eligibilityKind)
                                StatusPill(text: item.candidateType, kind: .neutral)
                                StatusPill(text: "conf \(item.confidenceLabel)", kind: item.confidence >= 0.72 ? .ok : .warning)
                                if item.evidenceCount > 0 {
                                    StatusPill(text: "\(item.evidenceCount) 证据", kind: .neutral)
                                }
                            }
                            HStack(spacing: 6) {
                                StatusPill(text: item.subjectScopeLabel, kind: item.subjectKind)
                                StatusPill(text: item.sourceCategoryLabel, kind: .neutral)
                                if !item.app.isEmpty {
                                    StatusPill(text: item.app, kind: .neutral)
                                }
                                StatusPill(text: item.queueBucketLabel, kind: .neutral)
                            }
                            if !item.qualityFlags.isEmpty {
                                Text(item.qualityFlags.prefix(5).joined(separator: " / "))
                                    .font(.caption2.monospaced())
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            if !item.directWriteDisabledReason.isEmpty {
                                Text("不能直接写入：\(item.directWriteDisabledReason)")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            if !item.rewriteDisabledReason.isEmpty {
                                Text("不能改写写入：\(item.rewriteDisabledReason)")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .padding(.top, 4)
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                Spacer(minLength: 12)
            }

            LazyVGrid(columns: [GridItem(.adaptive(minimum: 154), spacing: 8)], spacing: 8) {
                if item.showDirectWriteAction {
                    Button {
                        directWrite()
                    } label: {
                        Label("记住这句并预览", systemImage: "checkmark.circle")
                            .frame(maxWidth: .infinity)
                    }
                    .help("先生成 dry-run 预览，不会马上写入。")
                }

                if item.showRewriteWriteAction {
                    Button {
                        rewriteWrite()
                    } label: {
                        Label("我改写后再预览", systemImage: "square.and.pencil")
                            .frame(maxWidth: .infinity)
                    }
                    .help("你先改成一句长期稳定的事实，再生成 dry-run 预览。")
                }

                if item.showContextOnlyAction {
                    Button {
                        contextOnly()
                    } label: {
                        Label("仅本次参考", systemImage: "text.badge.checkmark")
                            .frame(maxWidth: .infinity)
                    }
                    .help("这次可以参考，但不写进长期记忆。")
                }

                if item.showNeverWriteAction {
                    Button {
                        neverWrite()
                    } label: {
                        Label("以后同类不要问", systemImage: "nosign")
                            .frame(maxWidth: .infinity)
                    }
                    .help("按同类规则记录你的选择，后续类似内容会被降级或隐藏。")
                }
            }
            .buttonStyle(.bordered)
            .buttonBorderShape(.roundedRectangle(radius: 8))
        }
        .padding(12)
        .background(Color(NSColor.textBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

struct DecisionQueueWriteSheet: View {
    let request: DecisionQueueWriteRequest
    @ObservedObject var runner: CoreRunner
    @Environment(\.dismiss) private var dismiss
    @State private var draftText: String
    @State private var preview = MemoryApprovalPreviewResult()
    @State private var previewedText = ""
    @State private var loading = false

    init(request: DecisionQueueWriteRequest, runner: CoreRunner) {
        self.request = request
        self.runner = runner
        _draftText = State(initialValue: request.initialText)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(request.title)
                        .font(.title3.weight(.semibold))
                    Text(request.subtitle)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                StatusPill(text: "先预览", kind: .ok)
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("原候选")
                    .font(.headline)
                VStack(alignment: .leading, spacing: 3) {
                    Text("系统识别到：")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(request.item.candidateDisplay)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("为什么需要你确认：")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(request.item.uncertaintyDisplay)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if request.requiresText {
                Text("改写成一句能长期保存的话")
                    .font(.headline)
                TextEditor(text: $draftText)
                    .font(.body)
                    .frame(minHeight: 130)
                    .scrollContentBackground(.hidden)
                    .background(Color(NSColor.textBackgroundColor))
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                Text("不要放路径、token、联系方式、原文片段或只在当下有效的状态。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Text("待预览的写入文字")
                    .font(.headline)
                Text(request.initialText)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color(NSColor.textBackgroundColor))
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            }

            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "info.circle")
                    .foregroundStyle(.secondary)
                Text("先生成预览，是为了让系统检查重复、硬阻断和安全边界。预览通过后，下方会显示确认后真正写入的最终文字。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !preview.status.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        StatusPill(text: previewStatusLabel, kind: previewStatusKind)
                        if previewTextChanged {
                            StatusPill(text: "文字已改动", kind: .warning)
                        }
                    }
                    if preview.ready {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("确认后将写入长期记忆的最终文字：")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(previewedText)
                                .font(.callout.weight(.medium))
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if previewTextChanged {
                            Text("你已经改动了上面的文字。为了避免写入旧版本，请重新生成预览。")
                                .font(.caption)
                                .foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        DisclosureGroup("预览细节") {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(preview.displayMessage)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                                if !preview.planID.isEmpty {
                                    Text("plan: \(preview.planID)")
                                        .font(.caption2.monospaced())
                                        .foregroundStyle(.secondary)
                                }
                            }
                            .padding(.top, 4)
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    } else {
                        Text(preview.displayMessage)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(previewStatusKind.color.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            }

            HStack {
                Button("取消") { dismiss() }
                Spacer()
                Button {
                    Task { await runPreview() }
                } label: {
                    Label(loading ? "预览中" : "生成预览", systemImage: "doc.text.magnifyingglass")
                }
                .disabled(loading || normalizedDraftText.isEmpty)
                Button {
                    Task { await applyPreview() }
                } label: {
                    Label("确认写入这句", systemImage: "checkmark.circle")
                }
                .disabled(loading || !preview.ready || previewTextChanged)
            }
            .buttonStyle(.bordered)
            .buttonBorderShape(.roundedRectangle(radius: 8))
        }
        .padding(20)
        .frame(width: 560)
    }

    private var previewStatusLabel: String {
        if preview.ready { return "预览可写" }
        if preview.status.isEmpty { return "待预览" }
        return preview.status
    }

    private var previewStatusKind: StatusKind {
        if preview.ready { return .ok }
        if ["error", "blocked", "unsafe_text", "hard_blocked", "manual_rewrite_required"].contains(preview.status) {
            return .warning
        }
        return .neutral
    }

    private var normalizedDraftText: String {
        draftText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var previewTextChanged: Bool {
        preview.ready && previewedText != normalizedDraftText
    }

    private func runPreview() async {
        loading = true
        let text = normalizedDraftText
        let result = await runner.previewDecisionQueueWrite(request: request, memoryText: text)
        preview = result
        previewedText = result.ready ? text : ""
        loading = false
    }

    private func applyPreview() async {
        guard preview.ready else { return }
        loading = true
        let result = await runner.applyDecisionQueuePlan(planID: preview.planID)
        loading = false
        if result.status == "applied" {
            dismiss()
        } else {
            preview = result
        }
    }
}

struct MemoryApprovalSheet: View {
    let request: MemoryApprovalRequest
    @ObservedObject var runner: CoreRunner
    @Environment(\.dismiss) private var dismiss
    @State private var draftText: String
    @State private var preview = MemoryApprovalPreviewResult()
    @State private var loading = false

    init(request: MemoryApprovalRequest, runner: CoreRunner) {
        self.request = request
        self.runner = runner
        _draftText = State(initialValue: request.initialText)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(request.title)
                        .font(.title3.weight(.semibold))
                    Text(request.subtitle)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                StatusPill(text: "dry-run first", kind: .ok)
            }

            if request.requiresText {
                Text("安全摘要")
                    .font(.headline)
                TextEditor(text: $draftText)
                    .font(.body)
                    .frame(minHeight: 120)
                    .scrollContentBackground(.hidden)
                    .background(Color(NSColor.textBackgroundColor))
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                Text(request.initialText)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color(NSColor.textBackgroundColor))
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            }

            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Image(systemName: preview.status == "preview_ready" ? "checkmark.circle" : "info.circle")
                        .foregroundStyle(preview.status == "preview_ready" ? .green : .secondary)
                    Text(preview.displayMessage)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if preview.ready {
                    HStack(spacing: 8) {
                        StatusPill(text: "processed \(preview.processedCount)", kind: .neutral)
                        StatusPill(text: "deleted \(preview.deletedCount)", kind: preview.deletedCount > 0 ? .warning : .neutral)
                        StatusPill(text: "rewritten \(preview.rewrittenCount)", kind: preview.rewrittenCount > 0 ? .warning : .neutral)
                    }
                }
            }
            .padding(10)
            .background(Color(NSColor.controlBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

            HStack {
                Button("取消") {
                    dismiss()
                }
                Spacer()
                Button {
                    Task { await generatePreview() }
                } label: {
                    Label(loading ? "处理中" : "生成预览", systemImage: "doc.text.magnifyingglass")
                }
                .disabled(loading || (request.requiresText && draftText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
                Button(role: request.isDestructive ? .destructive : nil) {
                    Task { await applyPreview() }
                } label: {
                    Label(request.confirmTitle, systemImage: request.isDestructive ? "trash" : "checkmark.circle")
                }
                .disabled(loading || !preview.ready)
            }
            .buttonStyle(.bordered)
            .buttonBorderShape(.roundedRectangle(radius: 8))
        }
        .padding(18)
        .frame(width: 560)
    }

    private func generatePreview() async {
        loading = true
        preview = await runner.previewMemoryApproval(request: request, memoryText: draftText)
        loading = false
    }

    private func applyPreview() async {
        guard preview.ready else { return }
        loading = true
        let result = await runner.applyMemoryApproval(planID: preview.planID)
        loading = false
        if result.status == "applied" {
            dismiss()
        } else {
            preview = result
        }
    }
}

struct ResultPanel: View {
    let title: String
    let text: String
    var emptyText = "暂无结果。"

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
            ScrollView {
                Text(text.isEmpty ? emptyText : text)
                    .font(.system(.body, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding(12)
            }
            .frame(minHeight: 120)
            .background(Color(NSColor.textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
    }
}

struct StatusNotice: View {
    let systemImage: String
    let title: String
    let message: String

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: systemImage)
                .foregroundStyle(.orange)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.headline)
                Text(message)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }
        .padding(12)
        .background(Color.orange.opacity(0.10))
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

enum StatusKind: Equatable {
    case ok
    case warning
    case danger
    case neutral

    init(coreKind: String) {
        switch coreKind {
        case "ok":
            self = .ok
        case "warning":
            self = .warning
        case "danger":
            self = .danger
        default:
            self = .neutral
        }
    }

    var color: Color {
        switch self {
        case .ok: return .green
        case .warning: return .orange
        case .danger: return .red
        case .neutral: return .secondary
        }
    }
}

enum HealthAction {
    case refresh
    case startRecording
    case pause15
    case resumeRecording
    case openScreenRecordingSettings
    case openPrivacy
    case exportQualityReport
    case runBackupHygiene
}

struct HealthSignal: Identifiable {
    let id = UUID()
    let systemImage: String
    let title: String
    let statusLabel: String
    let kind: StatusKind
    let message: String
    let actionTitle: String
    let actionImage: String
    let action: HealthAction
}

struct DiagnosticsStatus {
    var status = "idle"
    var reasonCode = ""
    var coreKind = "neutral"
    var mode = "quick"
    var summary = "诊断尚未运行。"
    var checkedAt = ""
    var observedAt = ""
    var lastSuccessfulReadAt = ""
    var refreshOnly = true
    var elapsedMs = 0
    var targetResponseMs = 0
    var rawAccess = false
    var destructiveActions = false
    var memoryWriteActions = false
    var ruleWriteActions = false
    var probes: [DiagnosticProbe] = []
    var recommendations: [String] = []
    var actions: [DiagnosticAction] = []
    var readOnlyActionCount = 0
    var previewableActionCount = 0
    var confirmRequiredActionCount = 0

    init() {}

    private init(dictionary object: [String: Any]) {
        status = object["status"] as? String ?? "unknown"
        reasonCode = object["reason_code"] as? String ?? ""
        coreKind = object["kind"] as? String ?? "neutral"
        mode = object["mode"] as? String ?? mode
        summary = object["summary"] as? String ?? summary
        checkedAt = object["checked_at"] as? String ?? ""
        observedAt = checkedAt
        lastSuccessfulReadAt = checkedAt
        refreshOnly = false
        elapsedMs = object["elapsed_ms"] as? Int ?? 0
        targetResponseMs = object["target_response_ms"] as? Int ?? 0
        rawAccess = object["raw_access"] as? Bool ?? false
        destructiveActions = object["destructive_actions"] as? Bool ?? false
        memoryWriteActions = object["memory_write_actions"] as? Bool ?? false
        ruleWriteActions = object["rule_write_actions"] as? Bool ?? false
        probes = (object["probes"] as? [[String: Any]] ?? []).map(DiagnosticProbe.init(dictionary:))
        recommendations = object["recommendations"] as? [String] ?? []
        actions = (object["actions"] as? [[String: Any]] ?? []).map(DiagnosticAction.init(dictionary:))
        let actionCounts = object["action_counts"] as? [String: Any] ?? [:]
        readOnlyActionCount = actionCounts["read_only"] as? Int ?? 0
        previewableActionCount = actionCounts["previewable"] as? Int ?? 0
        confirmRequiredActionCount = actionCounts["confirm_required"] as? Int ?? 0
        if rawAccess || destructiveActions || memoryWriteActions || ruleWriteActions {
            coreKind = "danger"
            summary = "诊断返回了异常安全边界。"
        }
    }

    init(productionState state: ProductionDiagnosticsClientState) {
        if state.hasCurrentTrustedState, let dictionary = state.trustedDictionary {
            self.init(dictionary: dictionary)
            return
        }
        self.init()
        status = "unknown"
        reasonCode = state.reasonCode
        coreKind = "warning"
        mode = state.mode
        summary = state.unknownStatusTextCN
        checkedAt = state.observedAt
        observedAt = state.observedAt
        lastSuccessfulReadAt = state.lastSuccessfulReadAt ?? ""
        refreshOnly = true
        if state.reasonCode == "database_busy" {
            probes = [
                DiagnosticProbe(dictionary: [
                    "name": "database_busy",
                    "label": "数据库读窗口",
                    "status": "unknown",
                    "kind": "warning",
                    "summary": "数据库暂时不可读；未沿用旧诊断、服务或健康快照。",
                ])
            ]
        }
        actions = []
        recommendations = []
        readOnlyActionCount = 0
        previewableActionCount = 0
        confirmRequiredActionCount = 0
    }

    var kind: StatusKind {
        StatusKind(coreKind: coreKind)
    }

    var statusLabel: String {
        if status == "unknown" { return "未知" }
        switch kind {
        case .danger: return "异常"
        case .warning: return "观察"
        case .ok: return "正常"
        case .neutral: return status == "idle" ? "未运行" : "未知"
        }
    }

    var modeLabel: String {
        mode == "full" ? "完整" : "快速"
    }

    var elapsedLabel: String {
        if elapsedMs <= 0 { return "0ms" }
        if elapsedMs < 1000 { return "\(elapsedMs)ms" }
        return String(format: "%.1fs", Double(elapsedMs) / 1000.0)
    }

    var targetLabel: String {
        guard targetResponseMs > 0 else { return modeLabel }
        if targetResponseMs < 1000 { return "目标 \(targetResponseMs)ms" }
        return String(format: "目标 %.0fs", Double(targetResponseMs) / 1000.0)
    }

    var dangerCount: Int {
        probes.filter { $0.kind == .danger }.count
    }

    var warningCount: Int {
        probes.filter { $0.kind == .warning }.count
    }

    var visibleProbes: [DiagnosticProbe] {
        Array(probes.prefix(7))
    }

    var updatedLabel: String {
        if checkedAt.isEmpty { return "等待读取" }
        if status == "unknown" { return "观察 \(checkedAt)" }
        return "已读取"
    }
}

struct DiagnosticProbe: Identifiable {
    let id: String
    let name: String
    let label: String
    let status: String
    let coreKind: String
    let summary: String
    let elapsedMs: Int

    init(dictionary: [String: Any]) {
        name = dictionary["name"] as? String ?? UUID().uuidString
        id = name
        label = dictionary["label"] as? String ?? name
        status = dictionary["status"] as? String ?? ""
        coreKind = dictionary["kind"] as? String ?? "neutral"
        summary = dictionary["summary"] as? String ?? ""
        elapsedMs = dictionary["elapsed_ms"] as? Int ?? 0
    }

    var kind: StatusKind {
        StatusKind(coreKind: coreKind)
    }

    var statusLabel: String {
        switch kind {
        case .danger: return "异常"
        case .warning: return "观察"
        case .ok: return "正常"
        case .neutral: return status.isEmpty ? "未知" : status
        }
    }

    var systemImage: String {
        switch name {
        case "core_status": return "record.circle"
        case "ai_handoff": return "sparkles.rectangle.stack"
        case "privacy_policy": return "hand.raised"
        case "hygiene": return "externaldrive.badge.checkmark"
        case "ha_automation": return "clock.arrow.circlepath"
        case "quality_advice": return "wand.and.stars"
        default: return "info.circle"
        }
    }

    var timedSummary: String {
        if elapsedMs <= 0 { return summary }
        let elapsed = elapsedMs < 1000 ? "\(elapsedMs)ms" : String(format: "%.1fs", Double(elapsedMs) / 1000.0)
        return "\(summary)（\(elapsed)）"
    }
}

struct DiagnosticAction: Identifiable {
    let id: String
    let probe: String
    let kind: String
    let label: String
    let summary: String

    init(dictionary: [String: Any]) {
        id = dictionary["id"] as? String ?? UUID().uuidString
        probe = dictionary["probe"] as? String ?? ""
        kind = dictionary["kind"] as? String ?? "read_only"
        label = dictionary["label"] as? String ?? "查看诊断"
        summary = dictionary["summary"] as? String ?? ""
    }

    var kindLabel: String {
        switch kind {
        case "previewable": return "可预览"
        case "confirm_required": return "需确认"
        default: return "只读"
        }
    }

    var statusKind: StatusKind {
        switch kind {
        case "confirm_required": return .warning
        case "previewable": return .neutral
        default: return .ok
        }
    }

    var systemImage: String {
        switch kind {
        case "previewable": return "eye"
        case "confirm_required": return "hand.tap"
        default: return "doc.text.magnifyingglass"
        }
    }
}

struct AIConnectorStatus {
    var status = "idle"
    var mode = ""
    var baseURL = ""
    var startCommand = ""
    var stopCommand = ""
    var serverRunning = false
    var managedByApp = false
    var portConflict = false
    var stalePID = false
    var listenerStatus = "unknown"
    var pid: Int?
    var actionStatus = ""
    var message = ""
    var rawAccess = false
    var destructiveActions = false
    var memoryWriteActions = false
    var ruleWriteActions = false
    var endpointCount = 0
    var summary = "本机 AI 连接器尚未读取。"

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            status = "error"
            summary = "连接器状态无法解析。"
            return
        }
        status = object["status"] as? String ?? "unknown"
        mode = object["mode"] as? String ?? ""
        baseURL = object["base_url"] as? String ?? ""
        startCommand = object["start_command"] as? String ?? ""
        stopCommand = object["stop_command"] as? String ?? ""
        serverRunning = object["server_running"] as? Bool ?? false
        managedByApp = object["managed_by_app"] as? Bool ?? false
        portConflict = object["port_conflict"] as? Bool ?? false
        stalePID = object["stale_pid"] as? Bool ?? false
        listenerStatus = object["listener_status"] as? String ?? "unknown"
        pid = object["pid"] as? Int
        actionStatus = object["action_status"] as? String ?? ""
        message = object["message"] as? String ?? ""
        rawAccess = object["raw_access"] as? Bool ?? false
        destructiveActions = object["destructive_actions"] as? Bool ?? false
        memoryWriteActions = object["memory_write_actions"] as? Bool ?? false
        ruleWriteActions = object["rule_write_actions"] as? Bool ?? false
        endpointCount = (object["endpoints"] as? [[String: Any]] ?? []).count
        if rawAccess || destructiveActions || memoryWriteActions || ruleWriteActions {
            status = "error"
            summary = "连接器返回了异常安全边界。"
        } else {
            let runText = serverRunning ? "正在运行" : (portConflict ? "端口被占用" : "默认停止")
            let suffix = message.isEmpty ? "只读 GET 接口。" : message
            summary = "\(runText)；\(baseURL.isEmpty ? "loopback 地址待读取" : baseURL)；\(suffix)"
        }
    }

    var kind: StatusKind {
        if rawAccess || destructiveActions || memoryWriteActions || ruleWriteActions || status == "error" {
            return .danger
        }
        if status == "blocked" || portConflict || stalePID { return .warning }
        return status == "ok" ? .ok : .neutral
    }

    var statusLabel: String {
        if status == "ok" { return serverRunning ? "运行中" : "默认停止" }
        if status == "blocked" { return "需处理" }
        if status == "idle" { return "待读取" }
        if status == "error" { return "异常" }
        return status
    }

    var baseURLLabel: String {
        baseURL.replacingOccurrences(of: "http://", with: "")
    }

    var writeLabel: String {
        destructiveActions || memoryWriteActions || ruleWriteActions ? "异常" : "false"
    }

    var processLabel: String {
        if let pid, serverRunning { return "\(pid)" }
        return serverRunning ? "运行中" : "停止"
    }

    var listenerStatusLabel: String {
        switch listenerStatus {
        case "managed": return "受控运行"
        case "port_in_use": return "端口已占用"
        case "stale_pid": return "旧 PID"
        case "starting": return "启动中"
        case "stopped": return "默认停止"
        default: return listenerStatus.isEmpty ? "未知" : listenerStatus
        }
    }

    var controlSummary: String {
        if portConflict { return "端口已被其它进程占用；不会启动第二个 Connector。" }
        if stalePID { return "发现旧 PID，重新启动时会先清理过期记录。" }
        if serverRunning { return "受控 Connector 正在运行；只监听本机地址。" }
        return "默认不常驻；需要给本机 AI 读取安全摘要时手动启动。"
    }
}

struct AIReviewSnapshot {
    var status = "idle"
    var message = "选择时间范围后刷新，查看智能回顾。"
    var rangeLabel = ""
    var activityCount = 0
    var readableActivityCount = 0
    var partialRedactedCount = 0
    var communicationRedactedCount = 0
    var privacyEventCount = 0
    var gapEventCount = 0
    var duplicateEventCount = 0
    var dataTrustLevel = "unknown"
    var privacyScanStatus = "unknown"
    var rawAccess = false
    var destructiveActions = false
    var memoryWriteActions = false
    var warnings: [AIReviewWarning] = []
    var projects: [AIReviewProject] = []
    var decisionSignals: [AIReviewSignal] = []
    var followUps: [AIReviewSignal] = []
    var blockers: [AIReviewSignal] = []
    var samples: [AIReviewSample] = []
    var rules: [String] = []

    init() {}

    init(jsonText: String, fallbackLabel: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            status = "error"
            message = "智能回顾返回无法解析。"
            rangeLabel = fallbackLabel
            return
        }
        status = object["status"] as? String ?? "unknown"
        rangeLabel = (object["time_range"] as? [String: Any])?["label"] as? String ?? fallbackLabel
        rawAccess = object["raw_access"] as? Bool ?? false
        destructiveActions = object["destructive_actions"] as? Bool ?? false
        memoryWriteActions = object["memory_write_actions"] as? Bool ?? false
        let summary = object["summary"] as? [String: Any] ?? [:]
        activityCount = summary["activity_count"] as? Int ?? 0
        readableActivityCount = summary["readable_activity_count"] as? Int ?? 0
        partialRedactedCount = summary["partial_redacted_count"] as? Int ?? 0
        communicationRedactedCount = summary["communication_redacted_count"] as? Int ?? 0
        privacyEventCount = summary["privacy_event_count"] as? Int ?? 0
        gapEventCount = summary["gap_event_count"] as? Int ?? 0
        duplicateEventCount = summary["duplicate_event_count"] as? Int ?? 0
        let trust = object["data_trust"] as? [String: Any] ?? [:]
        dataTrustLevel = trust["level"] as? String ?? "unknown"
        privacyScanStatus = (object["privacy_scan"] as? [String: Any])?["status"] as? String ?? "unknown"
        warnings = (trust["warnings"] as? [[String: Any]] ?? []).map(AIReviewWarning.init(dictionary:))
        projects = (object["project_summaries"] as? [[String: Any]] ?? []).map(AIReviewProject.init(dictionary:))
        decisionSignals = (object["decision_signals"] as? [[String: Any]] ?? []).map(AIReviewSignal.init(dictionary:))
        followUps = (object["follow_ups"] as? [[String: Any]] ?? []).map(AIReviewSignal.init(dictionary:))
        blockers = (object["blockers"] as? [[String: Any]] ?? []).map(AIReviewSignal.init(dictionary:))
        samples = (object["safe_activity_samples"] as? [[String: Any]] ?? []).map(AIReviewSample.init(dictionary:))
        rules = object["downstream_ai_rules"] as? [String] ?? []
        if status != "ok" {
            message = object["message"] as? String ?? "智能回顾被隐私扫描阻断。"
        } else {
            message = "活动 \(activityCount) 条，可读 \(readableActivityCount) 条，项目 \(projects.count) 个。"
        }
    }

    var statusKind: StatusKind {
        if rawAccess || destructiveActions || memoryWriteActions { return .danger }
        if status == "ok" && privacyScanStatus == "ok" {
            return dataTrustLevel == "low" ? .warning : .ok
        }
        if status == "idle" { return .neutral }
        return .warning
    }

    var trustKind: StatusKind {
        switch dataTrustLevel {
        case "high": return .ok
        case "medium": return .warning
        case "low": return .warning
        default: return .neutral
        }
    }

    var statusLabel: String {
        if status == "ok" { return "已生成" }
        if status == "idle" { return "待刷新" }
        return status.isEmpty ? "未知" : status
    }
}

struct AIReviewWarning: Identifiable {
    let id = UUID()
    let type: String
    let message: String

    init(dictionary: [String: Any]) {
        type = dictionary["type"] as? String ?? "warning"
        message = dictionary["message"] as? String ?? type
    }
}

struct AIReviewProject: Identifiable {
    let id = UUID()
    let label: String
    let eventCount: Int
    let confidence: Double
    let topics: [String]
    let apps: [String]
    let representatives: [AIReviewSample]

    init(dictionary: [String: Any]) {
        label = dictionary["label"] as? String ?? dictionary["workflow"] as? String ?? "未分类"
        eventCount = dictionary["event_count"] as? Int ?? 0
        confidence = dictionary["confidence"] as? Double ?? 0
        topics = dictionary["top_topics"] as? [String] ?? []
        apps = dictionary["top_apps"] as? [String] ?? []
        representatives = (dictionary["representative_items"] as? [[String: Any]] ?? []).map(AIReviewSample.init(dictionary:))
    }
}

struct AIReviewSignal: Identifiable {
    let id = UUID()
    let timeLabel: String
    let topic: String
    let app: String
    let safeSummary: String
    let confidence: Double
    let recommendation: String

    init(dictionary: [String: Any]) {
        timeLabel = dictionary["time_label"] as? String ?? ""
        topic = dictionary["topic"] as? String ?? "未分类"
        app = dictionary["app"] as? String ?? ""
        safeSummary = dictionary["safe_summary"] as? String ?? ""
        confidence = dictionary["confidence"] as? Double ?? 0
        recommendation = dictionary["recommendation"] as? String ?? ""
    }
}

struct AIReviewSample: Identifiable {
    let id = UUID()
    let timeLabel: String
    let app: String
    let topic: String
    let workflowLabel: String
    let safeSummary: String
    let confidence: Double

    init(dictionary: [String: Any]) {
        timeLabel = dictionary["time_label"] as? String ?? ""
        app = dictionary["app"] as? String ?? ""
        topic = dictionary["topic"] as? String ?? "未分类"
        workflowLabel = dictionary["workflow_label"] as? String ?? dictionary["workflow"] as? String ?? "未分类"
        safeSummary = dictionary["safe_summary"] as? String ?? ""
        confidence = dictionary["confidence"] as? Double ?? 0
    }
}

struct MemoryReviewStatus {
    var ok = false
    var errorMessage = ""
    var queuePresent = false
    var ledgerPresent = false
    var storePresent = false
    var retrievalBlocked = false
    var supersedeSupported = false
    var generatedAt = ""
    var total = 0
    var pending = 0
    var byDecision: [String: Int] = [:]

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["interface"] as? String == "memory-review" else {
            errorMessage = "无法读取记忆审核状态。"
            return
        }
        ok = object["ok"] as? Bool ?? false
        errorMessage = object["error"] as? String ?? ""
        queuePresent = object["queue_path_present"] as? Bool ?? false
        ledgerPresent = object["ledger_path_present"] as? Bool ?? false
        storePresent = object["memory_store_present"] as? Bool ?? false
        retrievalBlocked = object["retrieval_blocked"] as? Bool ?? false
        supersedeSupported = object["supersede_supported"] as? Bool ?? false
        generatedAt = object["generated_at"] as? String ?? ""
        total = object["total"] as? Int ?? 0
        pending = object["pending"] as? Int ?? 0
        byDecision = object["by_decision"] as? [String: Int] ?? [:]
    }
}

struct MemoryReviewCandidate: Identifiable {
    var id: String { memoryID }
    var memoryID = ""
    var contentSHA256 = ""
    var fingerprint = ""
    var categories: [String] = []
    var reasonCodes: [String] = []
    var decision = ""
    var decisionTitle = ""
    var decidedAt = ""
    var reviewState = "pending"
    var retrievable = false

    init?(dictionary: [String: Any]) {
        guard let identifier = dictionary["memory_id"] as? String, !identifier.isEmpty else {
            return nil
        }
        memoryID = identifier
        contentSHA256 = dictionary["content_sha256"] as? String ?? ""
        fingerprint = dictionary["fingerprint"] as? String ?? ""
        categories = dictionary["categories"] as? [String] ?? []
        reasonCodes = dictionary["reason_codes"] as? [String] ?? []
        decision = dictionary["decision"] as? String ?? ""
        decisionTitle = dictionary["decision_title"] as? String ?? ""
        decidedAt = dictionary["decided_at"] as? String ?? ""
        reviewState = dictionary["review_state"] as? String ?? "pending"
        retrievable = dictionary["retrievable"] as? Bool ?? false
    }
}

struct MemoryReviewList {
    var items: [MemoryReviewCandidate] = []
    var total = 0
    var returned = 0
    var errorMessage = ""

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["interface"] as? String == "memory-review" else {
            errorMessage = "无法读取候选列表。"
            return
        }
        if let failure = object["error"] as? String {
            errorMessage = failure
            return
        }
        total = object["total"] as? Int ?? 0
        returned = object["returned"] as? Int ?? 0
        items = (object["items"] as? [[String: Any]] ?? []).compactMap(MemoryReviewCandidate.init)
    }
}

struct MemoryReviewPreview {
    var digest = ""
    var itemCount = 0
    var byAction: [String: Int] = [:]
    var errorMessage = ""

    var isReady: Bool { digest.count == 64 && itemCount > 0 }

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["interface"] as? String == "memory-review" else {
            errorMessage = "无法生成预览。"
            return
        }
        if let failure = object["error"] as? String {
            errorMessage = failure
            return
        }
        digest = object["preview_digest"] as? String ?? ""
        itemCount = object["item_count"] as? Int ?? 0
        byAction = object["by_action"] as? [String: Int] ?? [:]
    }
}

struct PrivacyPolicyStatus {
    var status = "unknown"
    var mode = "read_only_preview"
    var profile = "personal_high_precision"
    var rawAccess = false
    var destructiveActions = false
    var ruleWriteActions = false
    var firewallVersion = ""
    var redactionPolicyVersion = ""
    var hardBlockAppCount = 0
    var communicationAppCount = 0
    var highRiskDomainHintCount = 0
    var hardBlockExamples = "等待读取。"
    var communicationExamples = "等待读取。"
    var highRiskDomainExamples = "等待读取。"

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            status = "error"
            return
        }
        status = object["status"] as? String ?? status
        mode = object["mode"] as? String ?? mode
        profile = object["profile"] as? String ?? profile
        rawAccess = object["raw_access"] as? Bool ?? false
        destructiveActions = object["destructive_actions"] as? Bool ?? false
        ruleWriteActions = object["rule_write_actions"] as? Bool ?? false
        firewallVersion = object["firewall_version"] as? String ?? ""
        redactionPolicyVersion = object["redaction_policy_version"] as? String ?? ""
        let counts = object["rule_counts"] as? [String: Any] ?? [:]
        hardBlockAppCount = counts["hard_block_apps"] as? Int ?? 0
        communicationAppCount = counts["communication_apps"] as? Int ?? 0
        highRiskDomainHintCount = counts["high_risk_domain_hints"] as? Int ?? 0
        let policies = object["policies"] as? [[String: Any]] ?? []
        hardBlockExamples = Self.examples(for: "hard_block_apps", policies: policies)
        communicationExamples = Self.examples(for: "communication_apps", policies: policies)
        highRiskDomainExamples = Self.examples(for: "high_risk_domain_hints", policies: policies)
    }

    var summary: String {
        if status == "error" { return "隐私规则状态无法解析；未修改任何规则。" }
        return "\(profile)，\(firewallVersion)，\(redactionPolicyVersion)；本页只读预览，不写规则。"
    }

    var statusLabel: String {
        if status == "ok" { return "只读" }
        if status == "error" { return "异常" }
        return status.isEmpty ? "未知" : status
    }

    var kind: StatusKind {
        if rawAccess || destructiveActions || ruleWriteActions { return .danger }
        if status == "ok" { return .ok }
        if status == "error" { return .danger }
        return .neutral
    }

    private static func examples(for name: String, policies: [[String: Any]]) -> String {
        guard let item = policies.first(where: { $0["name"] as? String == name }) else { return "无样例" }
        let examples = item["examples"] as? [String] ?? []
        return examples.prefix(4).joined(separator: "、")
    }
}

struct PrivacyPolicyPreview {
    var status = "idle"
    var firewallStatus = ""
    var reason = ""
    var privacyLevel = ""
    var readableDetail = false
    var countOnly = false
    var summary = "输入 App 或域名后运行只读预览。"
    var rawAccess = false
    var ruleWriteActions = false

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            status = "error"
            summary = "隐私预览返回无法解析；未修改任何规则。"
            return
        }
        status = object["status"] as? String ?? "unknown"
        rawAccess = object["raw_access"] as? Bool ?? false
        ruleWriteActions = object["rule_write_actions"] as? Bool ?? false
        let classification = object["classification"] as? [String: Any] ?? [:]
        firewallStatus = classification["firewall_status"] as? String ?? ""
        reason = classification["reason"] as? String ?? ""
        privacyLevel = classification["privacy_level"] as? String ?? ""
        let effect = object["ai_handoff_effect"] as? [String: Any] ?? [:]
        readableDetail = effect["readable_detail"] as? Bool ?? false
        countOnly = effect["count_only"] as? Bool ?? false
        summary = effect["summary"] as? String ?? "预览完成。"
    }

    var title: String {
        if status == "idle" { return "等待预览" }
        if status == "error" { return "预览异常" }
        return "预览结果"
    }

    var effectLabel: String {
        if readableDetail { return "可读摘要" }
        if countOnly { return "只计数" }
        return status == "idle" ? "未运行" : "受保护"
    }

    var reasonLabel: String {
        if !reason.isEmpty { return reason }
        if !privacyLevel.isEmpty { return privacyLevel }
        return firewallStatus.isEmpty ? "无" : firewallStatus
    }

    var kind: StatusKind {
        if rawAccess || ruleWriteActions || status == "error" { return .danger }
        if readableDetail { return .ok }
        if countOnly { return .warning }
        return .neutral
    }
}

struct AIReviewPanel: View {
    let snapshot: AIReviewSnapshot
    let loading: Bool
    let rangeLabel: String
    let onRefresh: () -> Void
    let onExport: () -> Void

    var body: some View {
        SettingsSection(title: "智能 Review", footer: "直接读取只读 AI 接手包；所有片段都来自 normal clear 安全摘要，局部清洗和私密内容只进入计数。") {
            VStack(alignment: .leading, spacing: 12) {
                SettingsRow(
                    systemImage: "sparkles.rectangle.stack",
                    title: "项目回顾 - \(rangeLabel)",
                    subtitle: loading ? "正在生成智能回顾..." : snapshot.message
                ) {
                    HStack(spacing: 6) {
                        StatusPill(text: snapshot.statusLabel, kind: snapshot.statusKind)
                        StatusPill(text: "trust \(snapshot.dataTrustLevel)", kind: snapshot.trustKind)
                        StatusPill(text: "raw=false", kind: snapshot.rawAccess ? .danger : .ok)
                        Button {
                            onRefresh()
                        } label: {
                            Label("刷新", systemImage: "arrow.clockwise")
                        }
                        Button {
                            onExport()
                        } label: {
                            Label("导出", systemImage: "square.and.arrow.down")
                        }
                    }
                }
                Divider()
                HStack(spacing: 10) {
                    MetricCard(title: "活动", value: "\(snapshot.activityCount)", subtitle: "可读 \(snapshot.readableActivityCount)")
                    MetricCard(title: "保护", value: "\(snapshot.privacyEventCount)", subtitle: "局部 \(snapshot.partialRedactedCount) / 沟通 \(snapshot.communicationRedactedCount)")
                    MetricCard(title: "缺口", value: "\(snapshot.gapEventCount)", subtitle: "重复 \(snapshot.duplicateEventCount)")
                    MetricCard(title: "项目", value: "\(snapshot.projects.count)", subtitle: "决策 \(snapshot.decisionSignals.count)，待办 \(snapshot.followUps.count)")
                }
                if !snapshot.warnings.isEmpty {
                    Divider()
                    AIReviewWarningList(warnings: snapshot.warnings)
                }
                Divider()
                AIReviewProjectsView(projects: snapshot.projects)
                Divider()
                HStack(alignment: .top, spacing: 12) {
                    AIReviewSignalList(title: "疑似决策", signals: snapshot.decisionSignals, emptyText: "暂无疑似决策。")
                    AIReviewSignalList(title: "待办/跟进", signals: snapshot.followUps, emptyText: "暂无待办线索。")
                    AIReviewSignalList(title: "阻塞/异常", signals: snapshot.blockers, emptyText: "暂无阻塞线索。")
                }
                if !snapshot.samples.isEmpty {
                    Divider()
                    AIReviewSampleList(samples: Array(snapshot.samples.prefix(4)))
                }
            }
            .padding(14)
        }
    }
}

struct AIReviewWarningList: View {
    let warnings: [AIReviewWarning]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("低置信 / 风险提示")
                .font(.headline)
            ForEach(warnings.prefix(4)) { warning in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                    Text(warning.message)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

struct AIReviewProjectsView: View {
    let projects: [AIReviewProject]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("项目推进")
                .font(.headline)
            if projects.isEmpty {
                Text("暂无可读项目明细。")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(projects.prefix(5)) { project in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 8) {
                            Text(project.label)
                                .font(.callout.weight(.semibold))
                            StatusPill(text: "\(project.eventCount) 条", kind: .neutral)
                            StatusPill(text: "conf \(String(format: "%.2f", project.confidence))", kind: project.confidence >= 0.7 ? .ok : .warning)
                        }
                        Text("主题：\(project.topics.isEmpty ? "无" : project.topics.joined(separator: "、"))")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                        Text("App：\(project.apps.isEmpty ? "无" : project.apps.joined(separator: "、"))")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
}

struct AIReviewSignalList: View {
    let title: String
    let signals: [AIReviewSignal]
    let emptyText: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.headline)
            if signals.isEmpty {
                Text(emptyText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(signals.prefix(3)) { signal in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(signal.topic)
                            .font(.caption.weight(.semibold))
                            .lineLimit(1)
                        Text(signal.safeSummary)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(3)
                        Text("\(signal.app.isEmpty ? "无 App" : signal.app) · conf \(String(format: "%.2f", signal.confidence))")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }
}

struct AIReviewSampleList: View {
    let samples: [AIReviewSample]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("安全片段样例")
                .font(.headline)
            ForEach(samples) { sample in
                Text("\(sample.timeLabel) · \(sample.workflowLabel) · \(sample.app.isEmpty ? "无 App" : sample.app)：\(sample.safeSummary)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        }
    }
}

struct HealthCenterView: View {
    let overallLabel: String
    let overallKind: StatusKind
    let overallMessage: String
    let updatedAt: String
    let signals: [HealthSignal]
    let actionHandler: (HealthAction) -> Void

    var body: some View {
        SettingsSection(title: "今日健康中心") {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Image(systemName: overallIcon)
                        .foregroundStyle(overallColor)
                    Text(overallLabel)
                        .font(.title3.weight(.semibold))
                    StatusPill(text: statusText, kind: overallKind)
                    Spacer()
                    Text("完整证据 \(updatedAt)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 14)
                .padding(.top, 14)
                .padding(.bottom, 6)
                Text(overallMessage)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 14)
                    .padding(.bottom, 8)
                Divider()
                ForEach(Array(signals.enumerated()), id: \.element.id) { index, signal in
                    HealthSignalRow(signal: signal, actionHandler: actionHandler)
                    if index < signals.count - 1 {
                        Divider().padding(.leading, 52)
                    }
                }
            }
        }
    }

    private var statusText: String {
        switch overallKind {
        case .danger: return "红"
        case .warning: return "黄"
        case .ok: return "绿"
        case .neutral: return "灰"
        }
    }

    private var overallIcon: String {
        switch overallKind {
        case .danger: return "exclamationmark.triangle"
        case .warning: return "exclamationmark.circle"
        case .ok: return "checkmark.circle"
        case .neutral: return "info.circle"
        }
    }

    private var overallColor: Color {
        switch overallKind {
        case .danger: return .red
        case .warning: return .orange
        case .ok: return .green
        case .neutral: return .secondary
        }
    }
}

struct HealthSignalRow: View {
    let signal: HealthSignal
    let actionHandler: (HealthAction) -> Void

    var body: some View {
        SettingsRow(
            systemImage: signal.systemImage,
            title: signal.title,
            subtitle: signal.message
        ) {
            HStack(spacing: 8) {
                StatusPill(text: signal.statusLabel, kind: signal.kind)
                Button {
                    actionHandler(signal.action)
                } label: {
                    Label(signal.actionTitle, systemImage: signal.actionImage)
                        .labelStyle(.titleAndIcon)
                }
            }
        }
    }
}

struct StatusPill: View {
    let text: String
    let kind: StatusKind

    var body: some View {
        Text(text)
            .font(.caption.monospaced().weight(.semibold))
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .foregroundStyle(foreground)
            .background(background)
            .clipShape(Capsule())
    }

    private var foreground: Color {
        switch kind {
        case .ok: return .green
        case .warning: return .orange
        case .danger: return .red
        case .neutral: return .secondary
        }
    }

    private var background: Color {
        switch kind {
        case .ok: return Color.green.opacity(0.12)
        case .warning: return Color.orange.opacity(0.14)
        case .danger: return Color.red.opacity(0.12)
        case .neutral: return Color.secondary.opacity(0.12)
        }
    }
}

enum MemoryApprovalAction: String {
    case delete
    case rewrite
    case approveCandidate = "approve-candidate"
    case promoteReviewOnly = "promote-review-only"
}

enum DecisionQueueWriteMode {
    case direct
    case rewrite
}

struct DecisionQueueWriteRequest: Identifiable {
    let id = UUID()
    let item: DecisionQueueItem
    let mode: DecisionQueueWriteMode

    static func directWrite(item: DecisionQueueItem) -> DecisionQueueWriteRequest {
        DecisionQueueWriteRequest(item: item, mode: .direct)
    }

    static func rewriteWrite(item: DecisionQueueItem) -> DecisionQueueWriteRequest {
        DecisionQueueWriteRequest(item: item, mode: .rewrite)
    }

    var itemKey: String {
        item.itemKey
    }

    var packageID: String {
        item.packageID
    }

    var initialText: String {
        item.candidateDisplay
    }

    var requiresText: Bool {
        mode == .rewrite
    }

    var title: String {
        mode == .direct ? "预览写入长期记忆" : "改写后写入长期记忆"
    }

    var subtitle: String {
        switch mode {
        case .direct:
            return "这一步只生成 dry-run 预览，不会马上写入长期记忆。"
        case .rewrite:
            return "先把它改成一句明确、安全、可长期保存的话，再生成 dry-run 预览。"
        }
    }
}

struct ActivityConfirmationItem: Identifiable {
    var id: String { itemKey }
    let itemKey: String
    let eventType: String
    let safeObservation: String
    let decisionQuestion: String
    let whyUncertain: String
    let app: String
    let timeLabel: String
    let observedStart: String
    let observedEnd: String
    let confidence: Double
    let reasonCodes: [String]
    let state: String
    let answerOptions: [String]
    let userAnswer: String

    init(dictionary: [String: Any]) {
        itemKey = dictionary["item_key"] as? String ?? UUID().uuidString
        eventType = dictionary["event_type"] as? String ?? "activity_claim"
        safeObservation = MemoryApprovalMemory.safeText(
            dictionary["safe_observation"],
            fallback: "系统检测到一项活动，但安全摘要为空。",
            maxLength: 520
        )
        decisionQuestion = MemoryApprovalMemory.safeText(
            dictionary["decision_question"],
            fallback: "这项活动今天真的发生了吗？",
            maxLength: 180
        )
        whyUncertain = MemoryApprovalMemory.safeText(
            dictionary["why_uncertain"],
            fallback: "现有证据不足，系统不会替你下结论。",
            maxLength: 320
        )
        app = MemoryApprovalMemory.safeText(dictionary["app"], fallback: "", maxLength: 80)
        timeLabel = MemoryApprovalMemory.safeText(dictionary["time_label"], fallback: "", maxLength: 80)
        observedStart = dictionary["observed_start"] as? String ?? ""
        observedEnd = dictionary["observed_end"] as? String ?? ""
        confidence = max(0, min(1, dictionary["confidence"] as? Double ?? (dictionary["confidence"] as? NSNumber)?.doubleValue ?? 0))
        reasonCodes = (dictionary["reason_codes"] as? [String] ?? []).prefix(12).map {
            MemoryApprovalMemory.safeText($0, fallback: "", maxLength: 80)
        }.filter { !$0.isEmpty }
        state = dictionary["state"] as? String ?? "pending"
        answerOptions = dictionary["answer_options"] as? [String] ?? []
        if let answer = dictionary["user_answer"] as? String {
            userAnswer = answer
        } else if let answerObject = dictionary["user_answer"] as? [String: Any] {
            userAnswer = answerObject["answer"] as? String ?? ""
        } else {
            userAnswer = ""
        }
    }

    var questionDisplay: String {
        decisionQuestion.isEmpty ? "这项活动今天真的发生了吗？" : decisionQuestion
    }

    var observationDisplay: String {
        safeObservation
    }

    var uncertaintyDisplay: String {
        whyUncertain
    }

    var confidenceLabel: String {
        guard confidence > 0 else { return "需要确认" }
        return "把握 \(Int((confidence * 100).rounded()))%"
    }

    var contextLine: String {
        let dateLabel = observedStart.count >= 10 ? String(observedStart.prefix(10)) : ""
        let timeAlreadyIncludesDate = timeLabel.contains(dateLabel)
            || (timeLabel.contains("月") && timeLabel.contains("日"))
        let explicitDate = timeAlreadyIncludesDate ? "" : dateLabel
        return [explicitDate, timeLabel, app].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    func supports(_ answer: ActivityConfirmationAnswer) -> Bool {
        ActivityConfirmationAnswerAvailability.supports(
            answer,
            answerOptions: answerOptions,
            isLegacy: true
        )
    }
}

struct ActivityConfirmationSubject {
    let eventLabel: String
    let company: String
    let round: String
    let object: String
    let label: String
    let companyIdentified: Bool
    let roundIdentified: Bool
    let objectIdentified: Bool

    init(dictionary: [String: Any]) {
        eventLabel = MemoryApprovalMemory.safeText(dictionary["event_label"], fallback: "", maxLength: 80)
        company = MemoryApprovalMemory.safeText(dictionary["company"], fallback: "", maxLength: 120)
        round = MemoryApprovalMemory.safeText(dictionary["round"], fallback: "", maxLength: 80)
        object = MemoryApprovalMemory.safeText(dictionary["object"], fallback: "", maxLength: 120)
        label = MemoryApprovalMemory.safeText(dictionary["label"], fallback: "", maxLength: 180)
        companyIdentified = dictionary["company_identified"] as? Bool ?? !company.isEmpty
        roundIdentified = dictionary["round_identified"] as? Bool ?? !round.isEmpty
        objectIdentified = dictionary["object_identified"] as? Bool ?? !object.isEmpty
    }
}

struct ActivityConfirmationCleanedContent: Identifiable {
    var id: String { "\(text)|\(count)" }
    let text: String
    let count: Int

    init?(dictionary: [String: Any]) {
        let value = MemoryApprovalMemory.safeText(dictionary["text"], fallback: "", maxLength: 2400)
        guard !value.isEmpty else { return nil }
        text = value
        count = max(1, dictionary["count"] as? Int ?? (dictionary["count"] as? NSNumber)?.intValue ?? 1)
    }

    init?(text value: String, count: Int = 1) {
        let safeValue = MemoryApprovalMemory.safeText(value, fallback: "", maxLength: 2400)
        guard !safeValue.isEmpty else { return nil }
        text = safeValue
        self.count = max(1, count)
    }
}

struct ActivityConfirmationClarification {
    let outcome: ActivityConfirmationClarificationOutcome?
    let severity: ActivityConfirmationClarificationSeverity?
    let inputsUsed: [ActivityConfirmationClarificationInput]
    let inputsFieldPresent: Bool
    let inputsValid: Bool
    let evidenceSummary: ActivityConfirmationEvidenceSummary
    let hasEvidenceStatus: Bool
    let message: String
    let missingFields: [String]
    let generatedAt: String

    init(dictionary: [String: Any]) {
        let body = dictionary["result"] as? [String: Any] ?? dictionary
        outcome = ActivityConfirmationClarificationPolicy.normalizedOutcome(
            body["outcome"] as? String ?? ""
        )
        severity = ActivityConfirmationClarificationPolicy.normalizedSeverity(
            body["severity"] as? String ?? ""
        )
        let parsedInputs = body["inputs_used"] as? [String]
        let rawInputs = parsedInputs ?? []
        inputsUsed = ActivityConfirmationClarificationPolicy.normalizedInputs(rawInputs)
        inputsFieldPresent = parsedInputs != nil
        inputsValid = ActivityConfirmationClarificationPolicy.inputsAreValid(rawInputs)
        let rawEvidenceStatus = body["evidence_status"] as? String ?? ""
        hasEvidenceStatus = ActivityConfirmationEvidenceStatus(rawValue: rawEvidenceStatus) != nil
        evidenceSummary = ActivityConfirmationEvidenceSummary(
            status: rawEvidenceStatus,
            evidenceCount: body["evidence_count"] as? Int
                ?? (body["evidence_count"] as? NSNumber)?.intValue
                ?? 0,
            availableSourceCount: body["available_source_count"] as? Int
                ?? (body["available_source_count"] as? NSNumber)?.intValue
                ?? 0
        )
        message = MemoryApprovalMemory.safeText(
            body["message"] ?? dictionary["message"],
            fallback: "",
            maxLength: 520
        )
        generatedAt = MemoryApprovalMemory.safeText(
            body["generated_at"] ?? dictionary["generated_at"],
            fallback: "",
            maxLength: 80
        )
        var labels: [String] = []
        for field in (body["missing_fields"] as? [String] ?? []).prefix(8) {
            let safeField = MemoryApprovalMemory.safeText(field, fallback: "", maxLength: 80)
            guard !safeField.isEmpty else { continue }
            let label = ActivityConfirmationFieldLabel.displayName(for: safeField)
            if !labels.contains(label) {
                labels.append(label)
            }
        }
        missingFields = labels
    }

    var isEmpty: Bool {
        outcome == nil && message.isEmpty && missingFields.isEmpty && generatedAt.isEmpty
    }

    var generatedAtLabel: String {
        ActivityConfirmationClarificationPolicy.generatedAtTimeLabel(generatedAt)
    }

    var title: String {
        switch outcome {
        case .systemFiltered: return "已核对并移出待确认"
        case .rewritten: return "已重新描述"
        case .split: return "已拆分为更具体的问题"
        case .unchanged: return "已重新读取，问题保持不变"
        case .insufficientInformation: return "仍缺少直接证据"
        case .evidenceUnavailable: return "原截图不可用"
        case nil: return "重新梳理结果"
        }
    }

    var systemImage: String {
        switch outcome {
        case .systemFiltered: return "line.3.horizontal.decrease.circle"
        case .rewritten: return "text.badge.checkmark"
        case .split: return "square.split.2x1"
        case .unchanged: return "equal.circle"
        case .insufficientInformation: return "questionmark.circle"
        case .evidenceUnavailable: return "photo.badge.exclamationmark"
        case nil: return "sparkles"
        }
    }
}

enum ActivityConfirmationCardSection: String {
    case pendingConfirmation = "pending_confirmation"
    case informationInsufficient = "information_insufficient"
}

struct ActivityConfirmationQuestionGroup: Identifiable {
    var id: String { groupKey }
    let groupKey: String
    let memberRevision: String
    let isLegacy: Bool
    let contractVersion: Int
    let v4Fields: ActivityConfirmationGroupV4Fields?
    let cardSection: ActivityConfirmationCardSection?
    let cardPriority: ActivityConfirmationPriority?
    let subjectStatus: String
    let semanticKey: String
    let verificationRoute: String
    let eventType: String
    let subject: ActivityConfirmationSubject
    let subjectLabel: String
    let triggerSentence: String
    let decisionQuestion: String
    let whyUncertain: String
    let app: String
    let timeLabel: String
    let observedStart: String
    let observedEnd: String
    let confidence: Double
    let reasonCodes: [String]
    let state: String
    let answerOptions: [String]
    let userAnswer: String
    let sourceCount: Int
    let evidenceSummary: ActivityConfirmationEvidenceSummary
    let hasEvidenceStatus: Bool
    let cleanedContents: [ActivityConfirmationCleanedContent]
    let clarification: ActivityConfirmationClarification?

    init(dictionary: [String: Any], contractVersion: Int = 2) {
        let suppliedKey = dictionary["group_key"] as? String ?? ""
        groupKey = suppliedKey.isEmpty ? "unavailable-\(UUID().uuidString)" : suppliedKey
        if let revision = dictionary["member_revision"] as? String {
            memberRevision = revision
        } else if let revision = dictionary["member_revision"] as? NSNumber {
            memberRevision = revision.stringValue
        } else {
            memberRevision = ""
        }
        isLegacy = false
        self.contractVersion = contractVersion
        v4Fields = contractVersion == 4
            ? ActivityConfirmationGroupV4Fields(dictionary: dictionary)
            : nil
        cardSection = (dictionary["section"] as? String).flatMap(ActivityConfirmationCardSection.init(rawValue:))
        cardPriority = (dictionary["priority"] as? String).flatMap(ActivityConfirmationPriority.init(rawValue:))
        subjectStatus = MemoryApprovalMemory.safeText(
            dictionary["subject_status"],
            fallback: "identified",
            maxLength: 40
        )
        semanticKey = MemoryApprovalMemory.safeText(
            dictionary["semantic_key"],
            fallback: "",
            maxLength: 120
        )
        verificationRoute = MemoryApprovalMemory.safeText(
            dictionary["verification_route"],
            fallback: "user_confirmation",
            maxLength: 80
        )
        eventType = dictionary["event_type"] as? String ?? "activity_claim"
        let subjectDictionary = dictionary["canonical_subject"] as? [String: Any]
            ?? dictionary["subject"] as? [String: Any]
            ?? [:]
        subject = ActivityConfirmationSubject(dictionary: subjectDictionary)
        subjectLabel = MemoryApprovalMemory.safeText(
            dictionary["display_title"] ?? dictionary["subject_label"] ?? subjectDictionary["label"],
            fallback: Self.unknownSubjectLabel(for: eventType),
            maxLength: 180
        )
        triggerSentence = MemoryApprovalMemory.safeText(
            dictionary["display_evidence"] ?? dictionary["trigger_sentence"],
            fallback: "",
            maxLength: 720
        )
        decisionQuestion = MemoryApprovalMemory.safeText(
            dictionary["display_question"] ?? dictionary["decision_question"],
            fallback: cardSection == .informationInsufficient ? "" : "这项活动真的发生了吗？",
            maxLength: 240
        )
        whyUncertain = MemoryApprovalMemory.safeText(
            dictionary["why_uncertain"],
            fallback: "现有安全证据不足，系统不会替你下结论。",
            maxLength: 360
        )
        app = MemoryApprovalMemory.safeText(dictionary["app"], fallback: "", maxLength: 80)
        timeLabel = MemoryApprovalMemory.safeText(dictionary["time_label"], fallback: "", maxLength: 100)
        observedStart = dictionary["observed_start"] as? String ?? ""
        observedEnd = dictionary["observed_end"] as? String ?? ""
        confidence = max(0, min(1, dictionary["confidence"] as? Double ?? (dictionary["confidence"] as? NSNumber)?.doubleValue ?? 0))
        reasonCodes = (dictionary["reason_codes"] as? [String] ?? []).prefix(12).map {
            MemoryApprovalMemory.safeText($0, fallback: "", maxLength: 80)
        }.filter { !$0.isEmpty }
        state = dictionary["state"] as? String ?? "pending"
        answerOptions = dictionary["answer_options"] as? [String] ?? []
        if let answer = dictionary["user_answer"] as? String {
            userAnswer = answer
        } else if let answerObject = dictionary["user_answer"] as? [String: Any] {
            userAnswer = answerObject["answer"] as? String ?? ""
        } else {
            userAnswer = ""
        }
        let contentObjects = dictionary["cleaned_contents"] as? [[String: Any]] ?? []
        var parsedContents = contentObjects.compactMap(ActivityConfirmationCleanedContent.init(dictionary:))
        if parsedContents.isEmpty, let strings = dictionary["cleaned_contents"] as? [String] {
            parsedContents = strings.compactMap { ActivityConfirmationCleanedContent(text: $0) }
        }
        var contentOrder: [String] = []
        var contentCounts: [String: Int] = [:]
        for content in parsedContents {
            if contentCounts[content.text] == nil {
                contentOrder.append(content.text)
            }
            contentCounts[content.text, default: 0] += content.count
        }
        cleanedContents = contentOrder.prefix(24).compactMap {
            ActivityConfirmationCleanedContent(text: $0, count: contentCounts[$0] ?? 1)
        }
        sourceCount = max(
            1,
            dictionary["source_count"] as? Int
                ?? (dictionary["source_count"] as? NSNumber)?.intValue
                ?? parsedContents.reduce(0) { $0 + $1.count }
        )
        let rawEvidenceStatus = dictionary["evidence_status"] as? String ?? ""
        hasEvidenceStatus = ActivityConfirmationEvidenceStatus(rawValue: rawEvidenceStatus) != nil
        evidenceSummary = ActivityConfirmationEvidenceSummary(
            status: rawEvidenceStatus,
            evidenceCount: dictionary["evidence_count"] as? Int
                ?? (dictionary["evidence_count"] as? NSNumber)?.intValue
                ?? 0,
            availableSourceCount: dictionary["available_source_count"] as? Int
                ?? (dictionary["available_source_count"] as? NSNumber)?.intValue
                ?? 0
        )
        if let clarificationObject = dictionary["clarification"] as? [String: Any] {
            let parsed = ActivityConfirmationClarification(dictionary: clarificationObject)
            clarification = parsed.isEmpty ? nil : parsed
        } else {
            clarification = nil
        }
    }

    init(legacy item: ActivityConfirmationItem) {
        groupKey = item.itemKey
        memberRevision = ""
        isLegacy = true
        contractVersion = 1
        v4Fields = nil
        cardSection = nil
        cardPriority = nil
        subjectStatus = "identified"
        semanticKey = ""
        verificationRoute = "user_confirmation"
        eventType = item.eventType
        subject = ActivityConfirmationSubject(dictionary: [:])
        subjectLabel = Self.unknownSubjectLabel(for: item.eventType)
        triggerSentence = item.safeObservation
        decisionQuestion = item.decisionQuestion
        whyUncertain = item.whyUncertain
        app = item.app
        timeLabel = item.timeLabel
        observedStart = item.observedStart
        observedEnd = item.observedEnd
        confidence = item.confidence
        reasonCodes = item.reasonCodes
        state = item.state
        answerOptions = item.answerOptions
        userAnswer = item.userAnswer
        sourceCount = 1
        evidenceSummary = ActivityConfirmationEvidenceSummary(
            status: "unavailable",
            evidenceCount: 0,
            availableSourceCount: 0
        )
        hasEvidenceStatus = false
        cleanedContents = ActivityConfirmationCleanedContent(text: item.safeObservation).map { [$0] } ?? []
        clarification = nil
    }

    var questionDisplay: String {
        decisionQuestion.isEmpty ? "这项活动真的发生了吗？" : decisionQuestion
    }

    var evidenceDisplay: String {
        triggerSentence.isEmpty ? "系统没有返回可核对的触发句，请重新梳理。" : triggerSentence
    }

    var confidenceLabel: String {
        guard confidence > 0 else { return "需要确认" }
        return "把握 \(Int((confidence * 100).rounded()))%"
    }

    var contextLine: String {
        let dateLabel = observedStart.count >= 10 ? String(observedStart.prefix(10)) : ""
        let timeAlreadyIncludesDate = timeLabel.contains(dateLabel)
            || (timeLabel.contains("月") && timeLabel.contains("日"))
        let explicitDate = timeAlreadyIncludesDate ? "" : dateLabel
        return [explicitDate, timeLabel, app].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    var submissionTarget: ActivityConfirmationSubmissionTarget? {
        if isLegacy {
            return groupKey.hasPrefix("unavailable-") ? nil : .legacyItem(itemKey: groupKey)
        }
        guard !groupKey.hasPrefix("unavailable-"), !memberRevision.isEmpty else { return nil }
        return .questionGroup(groupKey: groupKey, memberRevision: memberRevision)
    }

    var canClarify: Bool {
        !isInformationInsufficient && !isLegacy && submissionTarget != nil
    }

    var questionKind: ActivityConfirmationQuestionKind {
        v4Fields?.questionKind ?? .occurrence
    }

    var priority: ActivityConfirmationPriority {
        cardPriority ?? v4Fields?.priority ?? .normal
    }

    var episodeID: String {
        v4Fields?.episodeID ?? ""
    }

    var notificationEligible: Bool {
        v4Fields?.notificationEligible ?? false
    }

    var isInformationInsufficient: Bool {
        cardSection == .informationInsufficient || subjectStatus == "information_insufficient"
    }

    var effectiveWorkstream: String {
        if let v4Fields {
            return v4Fields.userWorkstream.isEmpty
                ? v4Fields.suggestedWorkstream
                : v4Fields.userWorkstream
        }
        guard contractVersion == 5, subject.objectIdentified else { return "" }
        return ActivityConfirmationV5BusinessDefaults.workstream(
            object: subject.object,
            label: subject.label
        )
    }

    var effectiveOutcome: String {
        if let v4Fields {
            return (v4Fields.userOutcome ?? v4Fields.suggestedOutcome)?.rawValue ?? ""
        }
        guard contractVersion == 5, subject.objectIdentified else { return "" }
        return ActivityConfirmationV5BusinessDefaults.outcome(eventType: eventType)
    }

    func businessDetailsAreValid(workstream: String, outcome: String) -> Bool {
        guard contractVersion >= 4 else { return true }
        let safeWorkstream = ActivityConfirmationCommandBuilder.boundedWorkstream(workstream)
        let safeOutcome = ActivityConfirmationCommandBuilder.boundedOutcome(outcome)
        return !safeWorkstream.isEmpty && !safeOutcome.isEmpty
    }

    var businessValidationMessage: String {
        "确认发生前，请同时填写工作流并选择结果（可选“结果未知”）。"
    }

    var clarificationAccessibilityHint: String {
        if !hasEvidenceStatus {
            return "参考本机已清洗信息和上方补充说明重新梳理；当前 Core 未提供暂存截图状态，不记录回答，也不放行事实"
        }
        if evidenceSummary.canReinspectRetainedFrames {
            return "重新读取仍暂存在本机的截图 OCR，并参考已清洗信息和上方补充说明；不记录回答，也不放行事实"
        }
        return "没有可回看的暂存截图；将参考本机已清洗信息和上方补充说明重新梳理，不记录回答，也不放行事实"
    }

    func supports(_ answer: ActivityConfirmationAnswer) -> Bool {
        ActivityConfirmationAnswerAvailability.supports(
            answer,
            answerOptions: answerOptions,
            isLegacy: isLegacy
        )
    }

    private static func unknownSubjectLabel(for eventType: String) -> String {
        switch eventType {
        case "interview": return "具体公司和轮次未识别的面试"
        case "meeting": return "具体对象未识别的会议"
        case "send": return "具体对象未识别的发送活动"
        case "submit": return "具体内容未识别的提交活动"
        case "purchase": return "具体项目未识别的购买活动"
        case "publish": return "具体内容未识别的发布活动"
        default: return "具体主体未识别的活动"
        }
    }
}

struct ActivityConfirmationClarifyResponse {
    let status: String
    let groupKey: String
    let clarification: ActivityConfirmationClarification?
    let questionGroups: [ActivityConfirmationQuestionGroup]
    let displayMessage: String
    let severity: ActivityConfirmationClarificationSeverity

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              ActivityConfirmationSafetyBoundary.permits(
                  rawAccess: object["raw_access"],
                  destructiveActions: object["destructive_actions"],
                  memoryWriteActions: object["memory_write_actions"]
              )
        else {
            status = "unsafe_contract"
            groupKey = ""
            clarification = nil
            questionGroups = []
            displayMessage = "重新梳理结果无法解析或未满足本机安全契约。"
            severity = .warning
            return
        }
        let parsedStatus = object["status"] as? String ?? "unknown"
        let parsedGroupKey = object["group_key"] as? String ?? ""
        let parsedContractVersion = object["contract_version"] as? Int
            ?? (object["contract_version"] as? NSNumber)?.intValue
            ?? 3
        let resultObject = object["result"] as? [String: Any] ?? [:]
        let parsedClarification = ActivityConfirmationClarification(dictionary: resultObject)
        let parsedQuestionGroups = (resultObject["question_groups"] as? [[String: Any]] ?? [])
            .map { ActivityConfirmationQuestionGroup(dictionary: $0, contractVersion: parsedContractVersion) }
        let v4GroupsAreValid = parsedContractVersion < 4
            || parsedQuestionGroups.allSatisfy { $0.v4Fields != nil }
        guard ActivityConfirmationClarificationPolicy.completedPayloadIsValid(
            status: parsedStatus,
            groupKey: parsedGroupKey,
            outcome: parsedClarification.outcome,
            severity: parsedClarification.severity,
            generatedAt: parsedClarification.generatedAt,
            questionGroupCount: parsedQuestionGroups.count,
            containsRootGroup: parsedQuestionGroups.contains { $0.groupKey == parsedGroupKey },
            memberRevisionsComplete: parsedQuestionGroups.allSatisfy {
                !$0.groupKey.hasPrefix("unavailable-") && !$0.memberRevision.isEmpty
            },
            inputsFieldPresent: parsedClarification.inputsFieldPresent,
            inputsValid: parsedClarification.inputsValid,
            evidenceStatusValid: parsedClarification.hasEvidenceStatus
        ) else {
            status = "unsafe_contract"
            groupKey = ""
            clarification = nil
            questionGroups = []
            displayMessage = "重新梳理结果无法解析或未满足本机安全契约。"
            severity = .warning
            return
        }
        guard v4GroupsAreValid else {
            status = "unsafe_contract"
            groupKey = ""
            clarification = nil
            questionGroups = []
            displayMessage = "重新梳理结果缺少 activity-confirmation v4 的安全字段。"
            severity = .warning
            return
        }
        status = parsedStatus
        groupKey = parsedGroupKey
        clarification = parsedClarification.isEmpty ? nil : parsedClarification
        questionGroups = parsedQuestionGroups
        let message = MemoryApprovalMemory.safeText(
            resultObject["message"] ?? object["message"],
            fallback: "",
            maxLength: 520
        )
        displayMessage = ActivityConfirmationClarificationPolicy.feedbackMessage(
            status: status,
            outcome: parsedClarification.outcome,
            message: message
        )
        severity = parsedStatus == "clarified"
            ? (parsedClarification.severity ?? .warning)
            : .warning
    }

    var isSafe: Bool {
        status != "unsafe_contract"
    }
}

struct ActivityConfirmationCalibration: Identifiable {
    var id: String { calibrationKey }
    let calibrationKey: String
    let label: String
    let safeSummary: String
    let negativeCount: Int
    let positiveCount: Int
    let autoSuppressed: Bool
    let updatedAt: String
    let revoked: Bool
    let canRevoke: Bool

    init(dictionary: [String: Any]) {
        let suppliedKey = dictionary["calibration_key"] as? String
            ?? dictionary["key"] as? String
            ?? ""
        calibrationKey = suppliedKey.isEmpty ? "unavailable-\(UUID().uuidString)" : suppliedKey
        label = MemoryApprovalMemory.safeText(
            dictionary["label"],
            fallback: "活动信号",
            maxLength: 120
        )
        safeSummary = MemoryApprovalMemory.safeText(
            dictionary["safe_summary"]
                ?? dictionary["safe_rule_summary"]
                ?? dictionary["safe_observation"]
                ?? dictionary["summary"],
            fallback: "这条校准没有可展示的安全摘要。",
            maxLength: 360
        )
        negativeCount = dictionary["negative_count"] as? Int
            ?? (dictionary["negative_count"] as? NSNumber)?.intValue
            ?? 0
        positiveCount = dictionary["positive_count"] as? Int
            ?? (dictionary["positive_count"] as? NSNumber)?.intValue
            ?? 0
        autoSuppressed = dictionary["auto_suppressed"] as? Bool ?? false
        updatedAt = dictionary["updated_at"] as? String
            ?? dictionary["created_at"] as? String
            ?? ""
        let state = dictionary["state"] as? String ?? "active"
        let explicitlyRevoked = dictionary["revoked"] as? Bool ?? false
        revoked = explicitlyRevoked || state == "revoked"
        canRevoke = !suppliedKey.isEmpty && (
            dictionary["can_revoke"] as? Bool
                ?? !revoked
        )
    }

    var statusLabel: String {
        if revoked { return "已撤销" }
        if autoSuppressed { return "自动降级中" }
        return "仅用于校准"
    }

    var contextLine: String {
        var parts = ["负反馈 \(negativeCount) 次", "正反馈 \(positiveCount) 次"]
        if !updatedAt.isEmpty {
            let compact = updatedAt
                .replacingOccurrences(of: "T", with: " ")
                .replacingOccurrences(of: "Z", with: "")
            parts.append("更新 \(String(compact.prefix(16)))")
        }
        return parts.joined(separator: " · ")
    }
}

struct ActivityConfirmationSnapshot {
    var status = "unknown"
    var interface = ""
    var contractVersion = 1
    var rawAccess = false
    var destructiveActions = false
    var memoryWriteActions = false
    var pendingCount = 0
    var pendingQuestionCount = 0
    var pendingSourceCount = 0
    var totalCardCount = 0
    var informationInsufficientCount = 0
    var resolvedCount = 0
    var resolvedQuestionCount = 0
    var summary = ""
    var items: [ActivityConfirmationItem] = []
    var questionGroups: [ActivityConfirmationQuestionGroup] = []
    var contractMode: ActivityConfirmationContractMode = .legacyItems
    var calibrations: [ActivityConfirmationCalibration] = []
    var v4Summary: ActivityConfirmationV4Summary?
    var navigationState = ActivityConfirmationNavigationState.unavailable

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["status"] is String,
              object["interface"] as? String == "activity-confirmation",
              ActivityConfirmationSafetyBoundary.permits(
                  rawAccess: object["raw_access"],
                  destructiveActions: object["destructive_actions"],
                  memoryWriteActions: object["memory_write_actions"]
              )
        else {
            status = "error"
            summary = "待确认接口无法解析或未满足安全契约；没有把任何内容当成已确认事实。"
            return
        }
        status = object["status"] as? String ?? "error"
        interface = object["interface"] as? String ?? ""
        let parsedContractVersion = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
            object["contract_version"]
        )
            ?? Int(object["contract_version"] as? String ?? "")
        let contractVersionMalformed = object["contract_version"] != nil
            && (parsedContractVersion == nil || parsedContractVersion == 0)
        contractVersion = parsedContractVersion ?? 1
        rawAccess = object["raw_access"] as? Bool ?? false
        destructiveActions = object["destructive_actions"] as? Bool ?? false
        memoryWriteActions = object["memory_write_actions"] as? Bool ?? false
        pendingCount = object["pending_count"] as? Int ?? (object["pending_count"] as? NSNumber)?.intValue ?? 0
        resolvedCount = object["resolved_count"] as? Int ?? (object["resolved_count"] as? NSNumber)?.intValue ?? 0
        summary = MemoryApprovalMemory.safeText(object["summary"], fallback: "", maxLength: 320)
        items = (object["items"] as? [[String: Any]] ?? []).map(ActivityConfirmationItem.init(dictionary:))
        let groupObjects = object["question_groups"] as? [[String: Any]]
        contractMode = contractVersionMalformed
            ? .invalidGroupedContract
            : ActivityConfirmationContractMode.resolve(
                contractVersion: contractVersion,
                hasQuestionGroups: groupObjects != nil
            )
        switch contractMode {
        case .grouped:
            questionGroups = (groupObjects ?? []).map {
                ActivityConfirmationQuestionGroup(dictionary: $0, contractVersion: contractVersion)
            }
            pendingQuestionCount = object["pending_question_count"] as? Int
                ?? (object["pending_question_count"] as? NSNumber)?.intValue
                ?? pendingGroups.count
            pendingSourceCount = object["pending_source_count"] as? Int
                ?? (object["pending_source_count"] as? NSNumber)?.intValue
                ?? pendingGroups.reduce(0) { $0 + $1.sourceCount }
            resolvedQuestionCount = object["resolved_question_count"] as? Int
                ?? (object["resolved_question_count"] as? NSNumber)?.intValue
                ?? resolvedCount
            if contractVersion >= 4 {
                let decodedV4 = ActivityConfirmationV4PayloadState(dictionary: object)
                navigationState = decodedV4.navigationState
                pendingQuestionCount = decodedV4.navigationState.verifiedPendingCount ?? 0
                pendingSourceCount = decodedV4.pendingSourceCount
                resolvedQuestionCount = decodedV4.resolvedQuestionCount
                guard decodedV4.disposition == .readable,
                      let parsedV4Summary = decodedV4.summary
                else {
                    status = "unsafe_contract"
                    self.summary = decodedV4.failureReason
                    pendingCount = pendingQuestionCount
                    resolvedCount = resolvedQuestionCount
                    items = []
                    questionGroups = []
                    calibrations = []
                    v4Summary = nil
                    return
                }
                v4Summary = parsedV4Summary
            }
        case .legacyItems:
            questionGroups = pendingItems.map(ActivityConfirmationQuestionGroup.init(legacy:))
            pendingQuestionCount = pendingCount == 0 ? questionGroups.count : pendingCount
            pendingSourceCount = pendingQuestionCount
            resolvedQuestionCount = resolvedCount
            navigationState = .compatibleResponse(
                status: status,
                pendingCount: pendingQuestionCount
            )
        case .invalidGroupedContract:
            status = "unsafe_contract"
            summary = "待确认分组契约缺少有效的 question_groups；已停止展示和操作，不会回读兼容 items。"
            pendingCount = 0
            pendingQuestionCount = 0
            pendingSourceCount = 0
            resolvedCount = 0
            resolvedQuestionCount = 0
            items = []
            questionGroups = []
            calibrations = []
            v4Summary = nil
            navigationState = .unavailable
            return
        }
        if contractVersion < 4, contractMode == .grouped {
            navigationState = .compatibleResponse(
                status: status,
                pendingCount: pendingQuestionCount
            )
        }
        pendingCount = pendingQuestionCount
        totalCardCount = pendingQuestionCount
        resolvedCount = resolvedQuestionCount
        let calibrationObjects = object["calibrations"] as? [[String: Any]]
            ?? object["recent_calibrations"] as? [[String: Any]]
            ?? []
        calibrations = calibrationObjects.map(ActivityConfirmationCalibration.init(dictionary:))
        if pendingCount == 0 && !items.isEmpty {
            pendingCount = pendingQuestionCount
        }
        if rawAccess || destructiveActions || memoryWriteActions {
            status = "unsafe_contract"
            summary = "待确认接口没有满足本机安全边界，已停止展示和操作。"
            pendingCount = 0
            pendingQuestionCount = 0
            pendingSourceCount = 0
            items = []
            questionGroups = []
            calibrations = []
            v4Summary = nil
            navigationState = .unavailable
        }
    }

    var pendingItems: [ActivityConfirmationItem] {
        items.filter {
            ActivityConfirmationVisibilityPolicy.isVisibleQuestion(state: $0.state)
        }.sorted {
            let leftPriority = ActivityConfirmationVisibilityPolicy.sortPriority(state: $0.state)
            let rightPriority = ActivityConfirmationVisibilityPolicy.sortPriority(state: $1.state)
            if leftPriority != rightPriority { return leftPriority < rightPriority }
            return $0.observedStart > $1.observedStart
        }
    }

    var pendingGroups: [ActivityConfirmationQuestionGroup] {
        questionGroups.filter {
            ActivityConfirmationVisibilityPolicy.isVisibleQuestion(state: $0.state)
        }.sorted {
            if $0.priority != $1.priority {
                let priorityOrder: [ActivityConfirmationPriority: Int] = [.high: 0, .normal: 1, .low: 2]
                return priorityOrder[$0.priority, default: 1] < priorityOrder[$1.priority, default: 1]
            }
            let leftPriority = ActivityConfirmationVisibilityPolicy.sortPriority(state: $0.state)
            let rightPriority = ActivityConfirmationVisibilityPolicy.sortPriority(state: $1.state)
            if leftPriority != rightPriority { return leftPriority < rightPriority }
            return $0.observedStart > $1.observedStart
        }
    }

    var highPriorityPendingGroups: [ActivityConfirmationQuestionGroup] {
        pendingGroups.filter { $0.priority == .high }
    }

    var otherPendingGroups: [ActivityConfirmationQuestionGroup] {
        pendingGroups.filter { $0.priority != .high }
    }

    var isBlocked: Bool {
        status == "error" || status == "unsafe_contract" || status == "blocked"
    }

    var summaryLine: String {
        if !summary.isEmpty { return summary }
        if status == "ok" {
            return pendingQuestionCount == 0
                ? "当前没有待确认项目。"
                : "有 \(pendingQuestionCount) 个问题、\(pendingSourceCount) 条安全线索需要你确认。"
        }
        return "待确认状态：\(status)。"
    }
}

struct ActivityConfirmationCardsPage {
    let isValid: Bool
    let status: String
    let revision: String
    let nextCursor: String
    let hasMore: Bool
    let cursorReset: Bool
    let pendingQuestionCount: Int
    let pendingSourceCount: Int
    let totalCardCount: Int
    let informationInsufficientCount: Int
    let groups: [ActivityConfirmationQuestionGroup]

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["status"] as? String == "ok",
              object["interface"] as? String == "activity-confirmation",
              ActivityConfirmationV5Contract.excludesForbiddenFields(object),
              ActivityConfirmationSafetyBoundary.nonnegativeInteger(object["contract_version"]) == 5,
              object["raw_access"] as? Bool == false,
              object["destructive_actions"] as? Bool == false,
              object["memory_write_actions"] as? Bool == false,
              let revision = object["revision"] as? String,
              !revision.isEmpty,
              revision.count <= 160,
              let cards = object["cards"] as? [[String: Any]],
              let pageSize = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
                  object["page_size"]
              ),
              (1...12).contains(pageSize),
              cards.count <= pageSize,
              let pendingQuestionCount = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
                  object["pending_question_count"]
              ),
              let pendingSourceCount = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
                  object["pending_source_count"]
              )
        else {
            isValid = false
            status = "error"
            self.revision = ""
            nextCursor = ""
            hasMore = false
            cursorReset = false
            self.pendingQuestionCount = 0
            self.pendingSourceCount = 0
            totalCardCount = 0
            informationInsufficientCount = 0
            groups = []
            return
        }

        let totalCardCount = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
            object["total_card_count"]
        ) ?? pendingQuestionCount
        let informationInsufficientCount = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
            object["information_insufficient_count"]
        ) ?? max(0, totalCardCount - pendingQuestionCount)

        let parsedGroups = cards.compactMap { dictionary -> ActivityConfirmationQuestionGroup? in
            guard Self.cardIsSafe(dictionary) else { return nil }
            return ActivityConfirmationQuestionGroup(dictionary: dictionary, contractVersion: 5)
        }
        let parsedHasMore = object["has_more"] as? Bool ?? false
        let parsedNextCursor = MemoryApprovalMemory.safeText(
            object["next_cursor"],
            fallback: "",
            maxLength: 220
        )
        guard parsedGroups.count == cards.count,
              totalCardCount >= parsedGroups.count,
              totalCardCount >= pendingQuestionCount + informationInsufficientCount,
              parsedHasMore == !parsedNextCursor.isEmpty
        else {
            isValid = false
            status = "error"
            self.revision = ""
            nextCursor = ""
            hasMore = false
            cursorReset = false
            self.pendingQuestionCount = 0
            self.pendingSourceCount = 0
            self.totalCardCount = 0
            self.informationInsufficientCount = 0
            groups = []
            return
        }

        isValid = true
        status = "ok"
        self.revision = revision
        nextCursor = parsedNextCursor
        hasMore = parsedHasMore
        cursorReset = object["cursor_reset"] as? Bool ?? false
        self.pendingQuestionCount = pendingQuestionCount
        self.pendingSourceCount = pendingSourceCount
        self.totalCardCount = totalCardCount
        self.informationInsufficientCount = informationInsufficientCount
        groups = parsedGroups
    }

    var snapshot: ActivityConfirmationSnapshot {
        var snapshot = ActivityConfirmationSnapshot()
        snapshot.status = status
        snapshot.interface = "activity-confirmation"
        snapshot.contractVersion = 5
        snapshot.rawAccess = false
        snapshot.destructiveActions = false
        snapshot.memoryWriteActions = false
        snapshot.pendingCount = pendingQuestionCount
        snapshot.pendingQuestionCount = pendingQuestionCount
        snapshot.pendingSourceCount = pendingSourceCount
        snapshot.totalCardCount = totalCardCount
        snapshot.informationInsufficientCount = informationInsufficientCount
        snapshot.resolvedCount = 0
        snapshot.resolvedQuestionCount = 0
        snapshot.questionGroups = groups
        snapshot.contractMode = .grouped
        snapshot.navigationState = .compatibleResponse(
            status: status,
            pendingCount: pendingQuestionCount
        )
        snapshot.summary = pendingQuestionCount == 0
            ? "当前没有待确认项目。"
            : "有 \(pendingQuestionCount) 个问题、\(pendingSourceCount) 条安全线索需要你确认。"
        return snapshot
    }

    private static func cardIsSafe(_ dictionary: [String: Any]) -> Bool {
        guard ActivityConfirmationV5Contract.excludesForbiddenFields(dictionary),
              dictionary["raw_access"] as? Bool == false,
              let groupKey = dictionary["group_key"] as? String,
              !groupKey.isEmpty,
              groupKey.count <= 200,
              let memberRevision = dictionary["member_revision"] as? String,
              !memberRevision.isEmpty,
              memberRevision.count <= 200,
              let sectionValue = dictionary["section"] as? String,
              let section = ActivityConfirmationCardSection(rawValue: sectionValue),
              let subjectStatus = dictionary["subject_status"] as? String,
              ["identified", "information_insufficient"].contains(subjectStatus),
              let verificationRoute = dictionary["verification_route"] as? String,
              ["user_confirmation", "information_insufficient"].contains(verificationRoute),
              dictionary["canonical_subject"] is [String: Any],
              let priorityValue = dictionary["priority"] as? String,
              ActivityConfirmationPriority(rawValue: priorityValue) != nil,
              let state = dictionary["state"] as? String,
              ActivityConfirmationVisibilityPolicy.isVisibleQuestion(state: state),
              let semanticKey = dictionary["semantic_key"] as? String,
              !semanticKey.isEmpty,
              semanticKey.count <= 120,
              let title = dictionary["display_title"] as? String,
              !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              title.count <= 180,
              let evidence = dictionary["display_evidence"] as? String,
              !evidence.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              evidence.count <= 720,
              let question = dictionary["display_question"] as? String,
              question.count <= 240,
              let answerOptions = dictionary["answer_options"] as? [String],
              answerOptions.allSatisfy({ ActivityConfirmationAnswer(rawValue: $0) != nil }),
              let sourceCount = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
                  dictionary["source_count"]
              ),
              sourceCount > 0
        else { return false }

        if section == .informationInsufficient {
            return subjectStatus == "information_insufficient"
                && verificationRoute == "information_insufficient"
                && question.isEmpty
                && answerOptions.isEmpty
        }
        return subjectStatus == "identified"
            && verificationRoute == "user_confirmation"
            && !question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !answerOptions.isEmpty
    }
}

struct ActivityConfirmationRevisionSnapshot {
    let isValid: Bool
    let revision: String
    let pendingQuestionCount: Int
    let pendingSourceCount: Int
    let totalCardCount: Int
    let informationInsufficientCount: Int

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["status"] as? String == "ok",
              object["interface"] as? String == "activity-confirmation",
              ActivityConfirmationSafetyBoundary.nonnegativeInteger(object["contract_version"]) == 5,
              object["raw_access"] as? Bool == false,
              let revision = object["revision"] as? String,
              !revision.isEmpty,
              revision.count <= 160,
              let pendingQuestionCount = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
                  object["pending_question_count"]
              ),
              let pendingSourceCount = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
                  object["pending_source_count"]
              )
        else {
            isValid = false
            self.revision = ""
            self.pendingQuestionCount = 0
            self.pendingSourceCount = 0
            totalCardCount = 0
            informationInsufficientCount = 0
            return
        }
        isValid = true
        self.revision = revision
        self.pendingQuestionCount = pendingQuestionCount
        self.pendingSourceCount = pendingSourceCount
        totalCardCount = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
            object["total_card_count"]
        ) ?? pendingQuestionCount
        informationInsufficientCount = ActivityConfirmationSafetyBoundary.nonnegativeInteger(
            object["information_insufficient_count"]
        ) ?? max(0, totalCardCount - pendingQuestionCount)
    }
}

struct DecisionQueueDecision: Identifiable {
    var id: String { itemKey + action + updatedAt }
    let itemKey: String
    let sourceKind: String
    let action: String
    let safeSummary: String
    let userNote: String
    let generalizeScope: String
    let matchType: String
    let updatedAt: String

    init(dictionary: [String: Any]) {
        itemKey = dictionary["item_key"] as? String ?? ""
        sourceKind = dictionary["source_kind"] as? String ?? ""
        action = dictionary["action"] as? String ?? ""
        safeSummary = MemoryApprovalMemory.safeText(dictionary["safe_summary"], fallback: "安全摘要为空。", maxLength: 280)
        userNote = MemoryApprovalMemory.safeText(dictionary["user_note"], fallback: "", maxLength: 160)
        generalizeScope = dictionary["generalize_scope"] as? String ?? ""
        matchType = dictionary["match_type"] as? String ?? ""
        updatedAt = dictionary["updated_at"] as? String ?? dictionary["created_at"] as? String ?? ""
    }

    var titleLine: String {
        let scope = generalizeScope.isEmpty ? "exact" : generalizeScope
        return "\(actionLabel) · \(scope)"
    }

    var actionLabel: String {
        switch action {
        case "never-write": return "永不写入"
        case "context-only": return "仅本次参考"
        case "approved-write": return "已确认写入"
        case "rewritten-write": return "已改写写入"
        default: return action.isEmpty ? "已处理" : action
        }
    }

    var systemImage: String {
        switch action {
        case "never-write": return "nosign"
        case "context-only": return "text.badge.checkmark"
        case "approved-write", "rewritten-write": return "checkmark.circle"
        default: return "info.circle"
        }
    }

    var kind: StatusKind {
        switch action {
        case "never-write": return .warning
        case "context-only": return .neutral
        case "approved-write", "rewritten-write": return .ok
        default: return .neutral
        }
    }
}

struct DecisionQueueItem: Identifiable {
    var id: String { itemKey }
    let itemKey: String
    let itemType: String
    let sourceKind: String
    let sourceLabel: String
    let packageID: String
    let candidateKey: String
    let safeSummary: String
    let topic: String
    let app: String
    let timeLabel: String
    let candidateType: String
    let confidence: Double
    let whyUncertain: String
    let decisionQuestion: String
    let candidateSentence: String
    let uncertaintyExplanation: String
    let recommendedAction: String
    let availableActions: [String]
    let queueBucket: String
    let evidenceCount: Int
    let sourceCategory: String
    let subjectScope: String
    let writeEligibility: String
    let qualityFlags: [String]
    let canDirectWrite: Bool
    let canRewriteWrite: Bool
    let canContextOnly: Bool
    let canNeverWrite: Bool
    let directWriteDisabledReason: String
    let rewriteDisabledReason: String
    let decisionState: String
    let userDecision: DecisionQueueDecision?
    let mergedItemCount: Int

    init(dictionary: [String: Any]) {
        itemKey = dictionary["item_key"] as? String ?? dictionary["candidate_key"] as? String ?? UUID().uuidString
        itemType = dictionary["item_type"] as? String ?? ""
        sourceKind = dictionary["source_kind"] as? String ?? ""
        sourceLabel = dictionary["source_label"] as? String ?? ""
        packageID = dictionary["package_id"] as? String ?? ""
        candidateKey = dictionary["candidate_key"] as? String ?? ""
        safeSummary = MemoryApprovalMemory.safeText(dictionary["safe_summary"], fallback: "安全摘要为空。", maxLength: 520)
        topic = MemoryApprovalMemory.safeText(dictionary["topic"], fallback: "", maxLength: 140)
        app = MemoryApprovalMemory.safeText(dictionary["app"], fallback: "", maxLength: 80)
        timeLabel = MemoryApprovalMemory.safeText(dictionary["time_label"], fallback: "", maxLength: 80)
        candidateType = dictionary["candidate_type"] as? String ?? "activity_fact"
        confidence = dictionary["confidence"] as? Double ?? (dictionary["confidence"] as? NSNumber)?.doubleValue ?? 0
        whyUncertain = MemoryApprovalMemory.safeText(dictionary["why_uncertain"], fallback: "", maxLength: 240)
        decisionQuestion = MemoryApprovalMemory.safeText(dictionary["decision_question"], fallback: "", maxLength: 120)
        candidateSentence = MemoryApprovalMemory.safeText(dictionary["candidate_sentence"], fallback: "", maxLength: 520)
        uncertaintyExplanation = MemoryApprovalMemory.safeText(dictionary["uncertainty_explanation"], fallback: "", maxLength: 260)
        recommendedAction = dictionary["recommended_action"] as? String ?? ""
        availableActions = dictionary["available_actions"] as? [String] ?? []
        queueBucket = dictionary["queue_bucket"] as? String ?? ""
        evidenceCount = dictionary["evidence_count"] as? Int ?? (dictionary["evidence_count"] as? NSNumber)?.intValue ?? 0
        sourceCategory = dictionary["source_category"] as? String ?? ""
        subjectScope = dictionary["subject_scope"] as? String ?? ""
        writeEligibility = dictionary["write_eligibility"] as? String ?? "ephemeral"
        qualityFlags = dictionary["quality_flags"] as? [String] ?? []
        canDirectWrite = dictionary["can_direct_write"] as? Bool ?? false
        canRewriteWrite = dictionary["can_rewrite_write"] as? Bool ?? false
        canContextOnly = dictionary["can_context_only"] as? Bool ?? false
        canNeverWrite = dictionary["can_never_write"] as? Bool ?? false
        directWriteDisabledReason = MemoryApprovalMemory.safeText(dictionary["direct_write_disabled_reason"], fallback: "", maxLength: 180)
        rewriteDisabledReason = MemoryApprovalMemory.safeText(dictionary["rewrite_disabled_reason"], fallback: "", maxLength: 180)
        decisionState = dictionary["decision_state"] as? String ?? "needs_decision"
        mergedItemCount = dictionary["merged_item_count"] as? Int ?? (dictionary["merged_item_count"] as? NSNumber)?.intValue ?? 1
        if let decisionObject = dictionary["user_decision"] as? [String: Any],
           !(decisionObject["action"] as? String ?? "").isEmpty {
            userDecision = DecisionQueueDecision(dictionary: decisionObject)
        } else {
            userDecision = nil
        }
    }

    var sourceLabelDisplay: String {
        if !sourceLabel.isEmpty { return sourceLabel }
        if sourceKind == "memory_candidate" { return "长期记忆候选" }
        return "Review 线索"
    }

    var candidateDisplay: String {
        candidateSentence.isEmpty ? safeSummary : candidateSentence
    }

    var decisionQuestionDisplay: String {
        if !decisionQuestion.isEmpty { return decisionQuestion }
        return sourceKind == "memory_candidate" ? "要不要把这句话记进长期记忆？" : "这条线索要不要整理成长期记忆？"
    }

    var uncertaintyDisplay: String {
        if !uncertaintyExplanation.isEmpty { return uncertaintyExplanation }
        if !whyUncertain.isEmpty { return whyUncertain }
        switch writeEligibility {
        case "eligible":
            return "系统认为它可能是稳定事实，但写入前仍需要你确认。"
        case "review_only":
            return "系统觉得它可能有用，但现在不像一句能直接长期保存的事实，需要你改写或否决。"
        default:
            return "系统认为这类内容只适合临时参考，通常不该写进长期记忆。"
        }
    }

    var questionSystemImage: String {
        if showDirectWriteAction { return "checkmark.seal" }
        if showRewriteWriteAction { return "square.and.pencil" }
        return "questionmark.bubble"
    }

    var questionKind: StatusKind {
        if showDirectWriteAction { return .ok }
        if showRewriteWriteAction { return .warning }
        return .neutral
    }

    var eligibilityLabel: String {
        switch writeEligibility {
        case "eligible": return "可写池"
        case "review_only": return "复核池"
        default: return "临时降级"
        }
    }

    var eligibilityKind: StatusKind {
        switch writeEligibility {
        case "eligible": return canDirectWrite ? .ok : .warning
        case "review_only": return canRewriteWrite ? .warning : .neutral
        default: return .neutral
        }
    }

    var confidenceLabel: String {
        String(format: "%.2f", confidence)
    }

    var subjectScopeLabel: String {
        subjectScope.isEmpty ? "subject unknown" : subjectScope
    }

    var subjectKind: StatusKind {
        subjectScope == "self_or_user_owned" ? .ok : .warning
    }

    var sourceCategoryLabel: String {
        sourceCategory.isEmpty ? "source unknown" : sourceCategory
    }

    var directWriteHelp: String {
        if canDirectWrite { return "生成 dry-run 预览，不直接写入。" }
        return directWriteDisabledReason.isEmpty ? "这条不能直接写入。" : directWriteDisabledReason
    }

    var rewriteWriteHelp: String {
        if canRewriteWrite { return "先由你改写，再生成 dry-run 预览。" }
        return rewriteDisabledReason.isEmpty ? "这条不能改写写入。" : rewriteDisabledReason
    }

    var showDirectWriteAction: Bool {
        availableActions.isEmpty ? canDirectWrite : availableActions.contains("direct-write")
    }

    var showRewriteWriteAction: Bool {
        availableActions.isEmpty ? canRewriteWrite : availableActions.contains("rewrite-write")
    }

    var showContextOnlyAction: Bool {
        availableActions.isEmpty ? canContextOnly : availableActions.contains("context-only")
    }

    var showNeverWriteAction: Bool {
        availableActions.isEmpty ? canNeverWrite : availableActions.contains("never-write")
    }

    var recommendedActionLabel: String {
        switch recommendedAction {
        case "direct-write":
            return "建议：记住并预览"
        case "rewrite-write":
            return "建议：改写后预览"
        case "context-only":
            return "建议：仅本次参考"
        case "never-write":
            return "建议：以后别问"
        default:
            return sourceKind == "memory_candidate" ? "等待你判断" : "建议：先改写"
        }
    }

    var recommendedActionKind: StatusKind {
        switch recommendedAction {
        case "direct-write":
            return .ok
        case "rewrite-write":
            return .warning
        default:
            return .neutral
        }
    }

    var writeBlockedExplanation: String {
        if showDirectWriteAction || showRewriteWriteAction { return "" }
        if !rewriteDisabledReason.isEmpty { return rewriteDisabledReason }
        if !directWriteDisabledReason.isEmpty { return directWriteDisabledReason }
        return "这类内容不适合长期记忆；你只需要决定是否本次参考，或以后同类不再问。"
    }

    var queueBucketLabel: String {
        switch queueBucket {
        case "can_preview":
            return "可预览"
        case "needs_rewrite":
            return "需改写"
        case "context_only":
            return "只作参考"
        default:
            return queueBucket.isEmpty ? "未分组" : queueBucket
        }
    }

    var hasUserDecision: Bool {
        userDecision != nil || decisionState != "needs_decision"
    }

    var userDecisionLine: String {
        if let userDecision {
            return "已按你的规则处理：\(userDecision.actionLabel)"
        }
        return decisionState
    }
}

struct DecisionQueueAutoHandled: Identifiable {
    let reason: String
    let label: String
    let count: Int

    var id: String { reason }

    init(dictionary: [String: Any]) {
        reason = dictionary["reason"] as? String ?? ""
        label = MemoryApprovalMemory.safeText(dictionary["label"], fallback: "自动处理", maxLength: 80)
        count = dictionary["count"] as? Int ?? (dictionary["count"] as? NSNumber)?.intValue ?? 0
    }

    var systemImage: String {
        switch reason {
        case "self_ui_noise":
            return "display"
        case "hard_blocked":
            return "lock.shield"
        case "already_written", "near_duplicate", "merged_duplicate":
            return "rectangle.on.rectangle"
        default:
            return "tray.and.arrow.down"
        }
    }
}

struct DecisionQueueSnapshot {
    var status = "unknown"
    var rawAccess = false
    var destructiveActions = false
    var memoryWriteActions = false
    var itemCount = 0
    var hiddenByDecisionCount = 0
    var autoHandledCount = 0
    var memoryCandidateCount = 0
    var reviewSignalCount = 0
    var summary = ""
    var items: [DecisionQueueItem] = []
    var autoHandledItems: [DecisionQueueAutoHandled] = []
    var recentDecisions: [DecisionQueueDecision] = []

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            status = "error"
            summary = "待决策队列无法解析。"
            return
        }
        status = object["status"] as? String ?? status
        rawAccess = object["raw_access"] as? Bool ?? false
        destructiveActions = object["destructive_actions"] as? Bool ?? false
        memoryWriteActions = object["memory_write_actions"] as? Bool ?? false
        itemCount = object["item_count"] as? Int ?? (object["item_count"] as? NSNumber)?.intValue ?? 0
        hiddenByDecisionCount = object["hidden_by_user_decision_count"] as? Int ?? (object["hidden_by_user_decision_count"] as? NSNumber)?.intValue ?? 0
        autoHandledCount = object["auto_handled_count"] as? Int ?? (object["auto_handled_count"] as? NSNumber)?.intValue ?? 0
        memoryCandidateCount = object["memory_candidate_count"] as? Int ?? (object["memory_candidate_count"] as? NSNumber)?.intValue ?? 0
        reviewSignalCount = object["review_signal_count"] as? Int ?? (object["review_signal_count"] as? NSNumber)?.intValue ?? 0
        summary = MemoryApprovalMemory.safeText(object["summary"], fallback: "", maxLength: 260)
        items = (object["items"] as? [[String: Any]] ?? []).map(DecisionQueueItem.init(dictionary:))
        autoHandledItems = (object["auto_handled"] as? [[String: Any]] ?? []).map(DecisionQueueAutoHandled.init(dictionary:))
        recentDecisions = (object["recent_decisions"] as? [[String: Any]] ?? []).map(DecisionQueueDecision.init(dictionary:))
    }

    var memoryItems: [DecisionQueueItem] {
        items.filter { $0.sourceKind == "memory_candidate" }
    }

    var reviewItems: [DecisionQueueItem] {
        items.filter { $0.sourceKind.hasPrefix("ai_review_") }
    }

    var actionableItems: [DecisionQueueItem] {
        let visibleKeys = Set(
            DecisionSurfacePolicy.actionableItems(
                memoryWriteActions: memoryWriteActions,
                items: items.map {
                    DecisionSurfaceItem(
                        key: $0.itemKey,
                        decisionState: $0.decisionState,
                        queueBucket: $0.queueBucket,
                        canDirectWrite: $0.canDirectWrite,
                        canRewriteWrite: $0.canRewriteWrite
                    )
                }
            ).map(\.key)
        )
        return items.filter { visibleKeys.contains($0.itemKey) }
    }

    var shouldSurfaceAttention: Bool {
        !actionableItems.isEmpty
    }

    var summaryLine: String {
        if !summary.isEmpty { return summary }
        if status == "ok" { return "当前没有需要处理的项目。" }
        return "队列状态：\(status)。"
    }

    var statusLabel: String {
        if status == "ok" { return "待决策队列" }
        if status == "error" { return "队列异常" }
        return status.isEmpty ? "队列未知" : status
    }

    var kind: StatusKind {
        if rawAccess || destructiveActions || memoryWriteActions || status == "error" {
            return .danger
        }
        if status != "ok" {
            return .warning
        }
        return itemCount > 0 ? .ok : .neutral
    }
}

struct MemoryApprovalRequest: Identifiable {
    let id = UUID()
    let action: MemoryApprovalAction
    let ledgerID: String
    let candidateKey: String
    let packageID: String
    let initialText: String
    let title: String
    let subtitle: String
    let confirmTitle: String
    let requiresText: Bool
    let isDestructive: Bool

    static func delete(memory: MemoryApprovalMemory) -> MemoryApprovalRequest {
        MemoryApprovalRequest(
            action: .delete,
            ledgerID: memory.ledgerID,
            candidateKey: "",
            packageID: "",
            initialText: memory.safeSummary,
            title: "预览删除记忆",
            subtitle: "删除会先 dry-run，再由 core 校验 hash、备份并同步台账。",
            confirmTitle: "确认删除",
            requiresText: false,
            isDestructive: true
        )
    }

    static func rewrite(memory: MemoryApprovalMemory) -> MemoryApprovalRequest {
        MemoryApprovalRequest(
            action: .rewrite,
            ledgerID: memory.ledgerID,
            candidateKey: "",
            packageID: "",
            initialText: memory.safeSummary,
            title: "预览改写记忆",
            subtitle: "改写后的内容只应是安全摘要，不应包含原文、路径、token 或私聊细节。",
            confirmTitle: "确认改写",
            requiresText: true,
            isDestructive: false
        )
    }

    static func approve(candidate: MemoryApprovalCandidate, packageID: String) -> MemoryApprovalRequest {
        MemoryApprovalRequest(
            action: .approveCandidate,
            ledgerID: "",
            candidateKey: candidate.candidateKey,
            packageID: packageID,
            initialText: candidate.safeSummary,
            title: "预览写入候选",
            subtitle: "eligible 候选会在确认后写入长期记忆，并刷新 Obsidian 台账。",
            confirmTitle: "确认写入",
            requiresText: false,
            isDestructive: false
        )
    }

    static func promote(candidate: MemoryApprovalCandidate, packageID: String) -> MemoryApprovalRequest {
        MemoryApprovalRequest(
            action: .promoteReviewOnly,
            ledgerID: "",
            candidateKey: candidate.candidateKey,
            packageID: packageID,
            initialText: candidate.safeSummary,
            title: "改写并提升候选",
            subtitle: "review_only 必须先人工改写成明确、安全、可长期保存的事实。",
            confirmTitle: "确认写入",
            requiresText: true,
            isDestructive: false
        )
    }
}

struct MemoryApprovalMemory: Identifiable {
    var id: String { ledgerID }
    let ledgerID: String
    let safeSummary: String
    let subjectScope: String
    let sourceCategory: String
    let syncStatus: String
    let canDelete: Bool
    let canRewrite: Bool

    init(dictionary: [String: Any]) {
        ledgerID = dictionary["ledger_id"] as? String ?? ""
        safeSummary = Self.safeText(dictionary["safe_summary"], fallback: "安全摘要为空。", maxLength: 360)
        subjectScope = dictionary["subject_scope"] as? String ?? ""
        sourceCategory = dictionary["source_category"] as? String ?? ""
        syncStatus = dictionary["sync_status"] as? String ?? ""
        canDelete = dictionary["can_delete"] as? Bool ?? false
        canRewrite = dictionary["can_rewrite"] as? Bool ?? false
    }

    var subjectScopeLabel: String {
        subjectScope.isEmpty ? "主体未标注" : subjectScope
    }

    var sourceCategoryLabel: String {
        sourceCategory.isEmpty ? "来源未标注" : sourceCategory
    }

    var syncStatusLabel: String {
        syncStatus.isEmpty ? "unknown" : syncStatus
    }

    static func safeText(_ value: Any?, fallback: String, maxLength: Int) -> String {
        let text = (value as? String ?? fallback).trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.count > maxLength else { return text.isEmpty ? fallback : text }
        let end = text.index(text.startIndex, offsetBy: maxLength)
        return String(text[..<end]) + "..."
    }
}

struct MemoryApprovalCandidate: Identifiable {
    var id: String { candidateKey }
    let candidateKey: String
    let safeSummary: String
    let candidateType: String
    let whyKeep: String
    let evidenceCount: Int
    let sourceCategory: String
    let subjectScope: String
    let writeEligibility: String
    let qualityFlags: [String]
    let canApprove: Bool
    let canPromote: Bool
    let disabledReason: String

    init(dictionary: [String: Any]) {
        candidateKey = dictionary["candidate_key"] as? String ?? UUID().uuidString
        safeSummary = MemoryApprovalMemory.safeText(dictionary["safe_summary"], fallback: "安全摘要为空。", maxLength: 420)
        candidateType = dictionary["candidate_type"] as? String ?? "activity_fact"
        whyKeep = MemoryApprovalMemory.safeText(dictionary["why_keep"], fallback: "", maxLength: 180)
        evidenceCount = dictionary["evidence_count"] as? Int ?? 0
        sourceCategory = dictionary["source_category"] as? String ?? ""
        subjectScope = dictionary["subject_scope"] as? String ?? ""
        writeEligibility = dictionary["write_eligibility"] as? String ?? "ephemeral"
        qualityFlags = dictionary["quality_flags"] as? [String] ?? []
        canApprove = dictionary["can_approve"] as? Bool ?? false
        canPromote = dictionary["can_promote"] as? Bool ?? false
        disabledReason = MemoryApprovalMemory.safeText(dictionary["disabled_reason"], fallback: "", maxLength: 180)
    }

    var eligibilityLabel: String {
        switch writeEligibility {
        case "eligible": return "eligible"
        case "review_only": return "review only"
        default: return "ephemeral"
        }
    }

    var eligibilityKind: StatusKind {
        switch writeEligibility {
        case "eligible": return canApprove ? .ok : .warning
        case "review_only": return canPromote ? .warning : .neutral
        default: return .neutral
        }
    }

    var subjectScopeLabel: String {
        subjectScope.isEmpty ? "subject unknown" : subjectScope
    }

    var subjectKind: StatusKind {
        subjectScope == "self_or_user_owned" ? .ok : .warning
    }
}

struct MemoryApprovalRecord: Identifiable {
    let id = UUID()
    let status: String
    let action: String
    let message: String
    let recordedAt: String

    init(dictionary: [String: Any]) {
        status = dictionary["status"] as? String ?? "unknown"
        action = dictionary["action"] as? String ?? ""
        message = MemoryApprovalMemory.safeText(dictionary["message"], fallback: "", maxLength: 180)
        recordedAt = dictionary["recorded_at_utc"] as? String ?? ""
    }

    var titleLine: String {
        let actionText = action.isEmpty ? "memory approval" : action
        return "\(actionText)：\(status)"
    }
}

struct MemoryApprovalStatus {
    var currentMemories: [MemoryApprovalMemory] = []
    var records: [MemoryApprovalRecord] = []
    var rawAccess = false
    var destructiveActions = false

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        rawAccess = object["raw_access"] as? Bool ?? false
        destructiveActions = object["destructive_actions"] as? Bool ?? false
        currentMemories = (object["current_memories"] as? [[String: Any]] ?? []).map(MemoryApprovalMemory.init(dictionary:))
        records = (object["approval_records"] as? [[String: Any]] ?? []).map(MemoryApprovalRecord.init(dictionary:))
    }

    var currentMemorySummary: String {
        currentMemories.isEmpty ? "没有历史记忆摘要。" : "历史记忆仅供安全摘要级审计；本页不再提供改写或删除入口。"
    }

    var recordSummary: String {
        records.isEmpty ? "还没有本机审批记录。" : "只显示动作、状态和安全摘要化结果。"
    }
}

struct MemoryApprovalCandidates {
    var status = "unknown"
    var packageID = ""
    var rawAccess = false
    var destructiveActions = false
    var eligibleCandidateCount = 0
    var reviewOnlyCandidateCount = 0
    var ephemeralCandidateCount = 0
    var totalCandidateCount = 0
    var memoryCandidates: [MemoryApprovalCandidate] = []
    var reviewOnlyCandidates: [MemoryApprovalCandidate] = []
    var ephemeralCandidates: [MemoryApprovalCandidate] = []

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            status = "error"
            return
        }
        status = object["status"] as? String ?? status
        packageID = object["package_id"] as? String ?? ""
        rawAccess = object["raw_access"] as? Bool ?? false
        destructiveActions = object["destructive_actions"] as? Bool ?? false
        eligibleCandidateCount = object["eligible_candidate_count"] as? Int ?? 0
        reviewOnlyCandidateCount = object["review_only_candidate_count"] as? Int ?? 0
        ephemeralCandidateCount = object["ephemeral_candidate_count"] as? Int ?? 0
        totalCandidateCount = object["total_candidate_count"] as? Int ?? 0
        memoryCandidates = (object["memory_candidates"] as? [[String: Any]] ?? []).map(MemoryApprovalCandidate.init(dictionary:))
        reviewOnlyCandidates = (object["review_only_candidates"] as? [[String: Any]] ?? []).map(MemoryApprovalCandidate.init(dictionary:))
        ephemeralCandidates = (object["ephemeral_candidates"] as? [[String: Any]] ?? []).map(MemoryApprovalCandidate.init(dictionary:))
    }

    var allDisplayItems: [MemoryApprovalCandidate] {
        memoryCandidates + reviewOnlyCandidates + ephemeralCandidates
    }

    var subjectRiskCount: Int {
        allDisplayItems.filter { $0.subjectScope != "self_or_user_owned" }.count
    }

    var unknownEligibleCount: Int {
        memoryCandidates.filter { $0.qualityFlags.contains("unknown_app") }.count
    }

    var singleEvidenceDemotedCount: Int {
        allDisplayItems.filter { $0.qualityFlags.contains("single_evidence_demoted") }.count
    }

    var mediaOrFileEphemeralCount: Int {
        ephemeralCandidates.filter {
            $0.qualityFlags.contains("media_player_context") || $0.qualityFlags.contains("file_listing_context")
        }.count
    }

    var summaryLine: String {
        if status == "error" { return "候选状态无法解析。" }
        if status != "ok" { return "候选包不可用：\(status)。" }
        return "eligible \(eligibleCandidateCount)，review_only \(reviewOnlyCandidateCount)，ephemeral \(ephemeralCandidateCount)。"
    }

    var statusLabel: String {
        if status == "ok" { return "正常" }
        if status == "error" { return "异常" }
        return status.isEmpty ? "未知" : status
    }

    var kind: StatusKind {
        if rawAccess || destructiveActions || status == "error" { return .danger }
        if status != "ok" { return .warning }
        return eligibleCandidateCount > 0 || reviewOnlyCandidateCount > 0 ? .ok : .neutral
    }
}

struct MemoryApprovalPreviewResult {
    var status = "idle"
    var planID = ""
    var message = "先生成 dry-run 预览。"
    var processedCount = 0
    var deletedCount = 0
    var rewrittenCount = 0

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            status = "error"
            message = "审批结果无法解析。"
            return
        }
        status = object["status"] as? String ?? status
        planID = object["plan_id"] as? String ?? ""
        message = object["summary"] as? String ?? object["message"] as? String ?? message
        if let preview = object["preview"] as? [String: Any] {
            processedCount = preview["processed_count"] as? Int ?? 0
            deletedCount = preview["deleted_count"] as? Int ?? 0
            rewrittenCount = preview["rewritten_count"] as? Int ?? 0
        }
    }

    var ready: Bool {
        status == "preview_ready" && !planID.isEmpty
    }

    var displayMessage: String {
        message.isEmpty ? status : message
    }
}

struct AttentionItem: Identifiable {
    let id = UUID()
    let code: String
    let kind: StatusKind
    let title: String
    let message: String
    let severity: String
    let scope: String
    let remediation: String
    let recheck: String

    var systemImage: String {
        switch kind {
        case .danger: return "exclamationmark.triangle"
        case .warning: return "exclamationmark.circle"
        case .ok: return "checkmark.circle"
        case .neutral: return "info.circle"
        }
    }

    init(dictionary: [String: Any]) {
        code = dictionary["code"] as? String ?? ""
        title = dictionary["title"] as? String ?? "需要注意"
        message = dictionary["message"] as? String ?? ""
        severity = dictionary["severity"] as? String ?? "info"
        scope = dictionary["scope"] as? String ?? "capture"
        let remediationObject = dictionary["remediation"] as? [String: Any] ?? [:]
        remediation = remediationObject["summary"] as? String ?? ""
        let recheckObject = dictionary["recheck_result"] as? [String: Any] ?? [:]
        recheck = recheckObject["summary"] as? String ?? ""
        switch dictionary["kind"] as? String {
        case "danger":
            kind = .danger
        case "warning":
            kind = .warning
        case "ok":
            kind = .ok
        default:
            kind = .neutral
        }
    }

    var displayMessage: String {
        var lines = [message]
        if !remediation.isEmpty { lines.append("建议：\(remediation)") }
        if !recheck.isEmpty, severity != "info" { lines.append("复验：\(recheck)") }
        return lines.filter { !$0.isEmpty }.joined(separator: "\n")
            .replacingOccurrences(of: "PAM 开发调试面板", with: "AGI 开发调试面板")
            .replacingOccurrences(of: "PAM 调试面板", with: "AGI 调试面板")
            .replacingOccurrences(of: "PAM 健康", with: "AGI 健康")
    }
}

struct SoakGate: Identifiable {
    let id = UUID()
    var code = ""
    var label = ""
    var status = "pending"
    var summary = ""

    init(dictionary: [String: Any]) {
        code = dictionary["code"] as? String ?? ""
        label = dictionary["label"] as? String ?? code
        status = dictionary["status"] as? String ?? "pending"
        summary = dictionary["summary"] as? String ?? ""
    }

    var statusLabel: String {
        switch status {
        case "passed": return "通过"
        case "failed": return "未通过"
        default: return "待观察"
        }
    }

    var kind: StatusKind {
        switch status {
        case "passed": return .ok
        case "failed": return .danger
        default: return .warning
        }
    }

    var systemImage: String {
        switch status {
        case "passed": return "checkmark.circle"
        case "failed": return "xmark.circle"
        default: return "clock.badge.questionmark"
        }
    }
}

struct SoakScorecard {
    var status = "in_progress"
    var summary = "等待稳定性证据。"
    var observedDays = 0
    var passedCount = 0
    var failedCount = 0
    var pendingCount = 0
    var totalCount = 0
    var gates: [SoakGate] = []

    init() {}

    init(dictionary: [String: Any]) {
        status = dictionary["status"] as? String ?? status
        summary = dictionary["summary"] as? String ?? summary
        let window = dictionary["window"] as? [String: Any] ?? [:]
        observedDays = window["observed_days"] as? Int ?? 0
        let counts = dictionary["gate_counts"] as? [String: Any] ?? [:]
        passedCount = counts["passed"] as? Int ?? 0
        failedCount = counts["failed"] as? Int ?? 0
        pendingCount = counts["pending"] as? Int ?? 0
        totalCount = counts["total"] as? Int ?? 0
        gates = (dictionary["gates"] as? [[String: Any]] ?? []).map(SoakGate.init(dictionary:))
    }

    var statusLabel: String {
        switch status {
        case "passed": return "已通过"
        case "failed": return "未通过"
        default: return "观察中"
        }
    }

    var kind: StatusKind {
        switch status {
        case "passed": return .ok
        case "failed": return .danger
        default: return .warning
        }
    }

    var visibleGates: [SoakGate] {
        let priority = gates.filter { $0.status != "passed" }
        return Array((priority.isEmpty ? gates : priority).prefix(5))
    }
}

struct SessionTransitionEvidenceStatus {
    var state = "unknown"
    var source = "no_evidence"
    var reason = "no_evidence"
    var observedAt = ""
    var lastValidState = ""
    var lastValidAt = ""
    var evidenceCount = 0

    init() {}

    init(dictionary: [String: Any]) {
        evidenceCount = dictionary["evidence_count"] as? Int ?? 0
        let latest = dictionary["latest"] as? [String: Any] ?? [:]
        state = latest["state"] as? String ?? state
        source = latest["source"] as? String ?? source
        reason = latest["reason"] as? String ?? reason
        observedAt = latest["observed_at"] as? String ?? ""
        lastValidState = latest["last_valid_state"] as? String ?? ""
        lastValidAt = latest["last_valid_at"] as? String ?? ""
    }

    var stateLabel: String {
        switch state {
        case "unlocked": return "已解锁"
        case "locked": return "已锁屏"
        default: return "未知"
        }
    }

    var lastValidStateLabel: String {
        switch lastValidState {
        case "unlocked": return "已解锁"
        case "locked": return "已锁屏"
        default: return "暂无"
        }
    }

    var evidenceLabel: String {
        evidenceCount == 0 ? "暂无状态证据" : "\(source) · \(reason)"
    }

    var lastValidLabel: String {
        lastValidAt.isEmpty ? "尚无有效时间" : lastValidAt
    }
}

struct RuntimeIdentity {
    var coreRoot = ""
    var databasePath = ""
    var pythonExecutable = ""
    var processPID = 0
    var coreVersion = ""
    var coreCodeHash = "unknown"
    var pathKind = "other"
    var pathLabel = "custom local core"
    var isRepoLocal = false
    var isApplicationSupportRuntime = false
    var isWrapper = false

    init() {}

    init(dictionary: [String: Any]) {
        coreRoot = dictionary["core_root"] as? String ?? ""
        databasePath = dictionary["database_path"] as? String ?? ""
        pythonExecutable = dictionary["python_executable"] as? String ?? ""
        processPID = dictionary["process_pid"] as? Int ?? 0
        coreVersion = dictionary["core_version"] as? String ?? ""
        coreCodeHash = dictionary["core_code_hash"] as? String ?? "unknown"
        pathKind = dictionary["path_kind"] as? String ?? pathKind
        pathLabel = dictionary["path_label"] as? String ?? pathLabel
        isRepoLocal = dictionary["is_repo_local"] as? Bool ?? false
        isApplicationSupportRuntime = dictionary["is_application_support_runtime"] as? Bool ?? false
        isWrapper = dictionary["is_personal_ai_monitor_wrapper"] as? Bool ?? false
    }

    var kindLabel: String {
        if integrityMismatch { return "版本不一致" }
        if integrityUnverified { return "未验证" }
        switch pathKind {
        case "application_support": return "live runtime"
        case "wrapper": return "wrapper"
        case "repo_local": return "开发目录"
        default: return "自定义"
        }
    }

    var kind: StatusKind {
        if integrityMismatch { return .danger }
        if integrityUnverified { return .warning }
        switch pathKind {
        case "application_support", "wrapper": return .ok
        case "repo_local": return .warning
        default: return .neutral
        }
    }

    var displayPath: String {
        let path = coreRoot.isEmpty ? databasePath : coreRoot
        guard !path.isEmpty else { return "等待 core 状态。" }
        return (path as NSString).abbreviatingWithTildeInPath
    }

    var versionMismatch: Bool {
        let appVersion = AppBuildInfo.current.version
        return !coreVersion.isEmpty && appVersion != "unknown" && coreVersion != appVersion
    }

    var codeHashMismatch: Bool {
        let expected = AppBuildInfo.current.coreCodeHash
        return expected != "unknown" && coreCodeHash != "unknown" && expected != coreCodeHash
    }

    var integrityMismatch: Bool { versionMismatch || codeHashMismatch }

    var integrityUnverified: Bool {
        AppBuildInfo.current.version == "unknown"
            || coreVersion.isEmpty
            || AppBuildInfo.current.coreCodeHash == "unknown"
            || coreCodeHash == "unknown"
    }

    var integritySummary: String {
        if versionMismatch {
            return "App 为 v\(AppBuildInfo.current.version)，运行核心为 v\(coreVersion)；请重新安装同一构建。"
        }
        if codeHashMismatch {
            return "App 与 Application Support 中的核心代码指纹不同；请重新同步并重启记录服务。"
        }
        if integrityUnverified {
            return "App 或运行核心缺少版本/代码指纹，当前不能证明两者来自同一构建。"
        }
        return "App 与运行核心版本及代码指纹一致。"
    }

    var displaySummary: String {
        let versionLabel = coreVersion.isEmpty ? "版本未知" : "v\(coreVersion)"
        return "\(displayPath) · \(versionLabel)"
    }
}

struct DashboardHealth {
    var enabled = false
    var listening = false
    var pid = ""
    var managedByDaemon = false
    var orphanListener = false
    var status = "off"
    var summary = "开发调试面板未运行。"

    init() {}

    init(dictionary: [String: Any]) {
        enabled = dictionary["enabled"] as? Bool ?? false
        listening = dictionary["listening"] as? Bool ?? false
        pid = dictionary["pid"] as? String ?? ""
        managedByDaemon = dictionary["managed_by_daemon"] as? Bool ?? false
        orphanListener = dictionary["orphan_listener"] as? Bool ?? false
        status = dictionary["status"] as? String ?? status
        summary = dictionary["summary"] as? String ?? summary
    }

    var label: String {
        if managedByDaemon { return "已托管" }
        if orphanListener { return "仍在运行" }
        if listening { return "监听中" }
        if enabled { return "已启用" }
        return "关闭"
    }

    var kind: StatusKind {
        if orphanListener { return .warning }
        if managedByDaemon || (enabled && listening) { return .ok }
        if enabled && !listening { return .warning }
        return .neutral
    }
}

struct CaptureFreshness {
    var lastCaptureAgeSeconds: Int?
    var staleThresholdSeconds = 90
    var isStale = false
    var reason = ""
    var summary = "等待采集状态。"

    init() {}

    init(dictionary: [String: Any]) {
        lastCaptureAgeSeconds = dictionary["last_capture_age_seconds"] as? Int
        staleThresholdSeconds = dictionary["stale_threshold_seconds"] as? Int ?? staleThresholdSeconds
        isStale = dictionary["is_stale"] as? Bool ?? false
        reason = dictionary["reason"] as? String ?? ""
        summary = dictionary["summary"] as? String ?? summary
    }

    var label: String {
        isStale ? "停滞" : "正常"
    }

    var kind: StatusKind {
        isStale ? .danger : .ok
    }
}

struct ScreenRecordingPermissionStatus {
    var state = "unknown"
    var source = "unknown"
    var backendAuthoritative = false
    var summary = "记录核心尚无足够证据判断屏幕录制权限。"

    init() {}

    init(dictionary: [String: Any]) {
        state = dictionary["state"] as? String ?? state
        source = dictionary["source"] as? String ?? source
        backendAuthoritative = dictionary["backend_authoritative"] as? Bool ?? false
        summary = dictionary["summary"] as? String ?? summary
    }

    var observedGranted: Bool { backendAuthoritative && state == "granted_observed" }
    var denied: Bool { backendAuthoritative && state == "denied" }
}

struct CoverageHealth {
    var summary = "今天还没有覆盖健康数据。"
    var framesSampled = 0
    var framesDropped = 0
    var protectedCount = 0
    var gapCount = 0
    var reasonLines: [String] = []

    init() {}

    init(dictionary: [String: Any]) {
        summary = dictionary["summary"] as? String ?? summary
        framesSampled = dictionary["frames_sampled"] as? Int ?? 0
        framesDropped = dictionary["frames_dropped"] as? Int ?? 0
        protectedCount = dictionary["protected_count"] as? Int ?? 0
        gapCount = dictionary["gap_count"] as? Int ?? 0
        if let reasons = dictionary["main_reasons"] as? [[String: Any]] {
            reasonLines = reasons.map { item in
                let label = item["label"] as? String ?? item["reason"] as? String ?? "未知原因"
                let count = item["count"] as? Int ?? 0
                return "\(label)：\(count) 次"
            }
        }
    }
}

struct PrivacyHealth {
    var summary = "今天还没有隐私清洗统计。"
    var clearCount = 0
    var normalClearCount = 0
    var partialRedactedCount = 0
    var blockedCount = 0
    var reviewRequiredCount = 0
    var gapCount = 0
    var explanations: [String] = []

    init() {}

    init(dictionary: [String: Any]) {
        summary = dictionary["summary"] as? String ?? summary
        clearCount = dictionary["clear_count"] as? Int ?? 0
        normalClearCount = dictionary["normal_clear_count"] as? Int ?? 0
        partialRedactedCount = dictionary["partial_redacted_count"] as? Int ?? 0
        blockedCount = dictionary["blocked_count"] as? Int ?? 0
        reviewRequiredCount = dictionary["review_required_count"] as? Int ?? 0
        gapCount = dictionary["gap_count"] as? Int ?? 0
        explanations = dictionary["explanations"] as? [String] ?? []
    }
}





struct HygieneCleanup {
    var enabled = false
    var backupRotationEnabled = false
    var keepBackups = 2
    var minAgeDays = 14
    var quarantineDays = 7
    var status = "disabled"
    var label = "未启用"
    var kindName = "neutral"
    var summary = "旧数据自动清理状态未知。"
    var lastRunAt = ""
    var lastError = ""
    var quarantineCount = 0
    var quarantineSizeBytes = 0
    var nextDeleteAfter = ""

    init() {}

    init(dictionary: [String: Any]) {
        enabled = dictionary["enabled"] as? Bool ?? false
        backupRotationEnabled = dictionary["backup_rotation_enabled"] as? Bool ?? false
        keepBackups = dictionary["keep_backups"] as? Int ?? keepBackups
        minAgeDays = dictionary["min_age_days"] as? Int ?? minAgeDays
        quarantineDays = dictionary["quarantine_days"] as? Int ?? quarantineDays
        status = dictionary["status"] as? String ?? status
        label = dictionary["label"] as? String ?? (enabled ? "已启用" : "未启用")
        kindName = dictionary["kind"] as? String ?? kindName
        summary = dictionary["summary"] as? String ?? summary
        lastRunAt = dictionary["last_run_at"] as? String ?? ""
        lastError = dictionary["last_error"] as? String ?? ""
        quarantineCount = dictionary["quarantine_count"] as? Int ?? 0
        quarantineSizeBytes = dictionary["quarantine_size_bytes"] as? Int ?? 0
        nextDeleteAfter = dictionary["next_delete_after"] as? String ?? ""
    }

    var kind: StatusKind {
        switch kindName {
        case "ok": return .ok
        case "warning": return .warning
        case "danger": return .danger
        default: return .neutral
        }
    }
}

struct HAAutomation {
    var enabled = false
    var intervalMinutes = 60
    var triggerMode = "direct_hermes"
    var hermesProfile = "activity-analysis"
    var status = "disabled"
    var label = "未启用"
    var kindName = "neutral"
    var summary = "HA 自动包状态未知。"
    var lastGeneratedAt = ""
    var lastTriggeredAt = ""
    var lastPackageId = ""
    var lastError = ""
    var pendingCount = 0
    var failedCount = 0
    var deadLetterCount = 0
    var historicalDeadLetterCount = 0
    var unresolvedDeadLetterCount = 0
    var retryDueCount = 0
    var stalePendingCount = 0
    var memoryReportPendingCount = 0
    var memoryReportFailedCount = 0
    var memoryReportRetryDueCount = 0
    var activePackageCount = 0
    var activeAutoPackageCount = 0
    var latestCandidateCount = 0
    var latestEligibleCandidateCount = 0
    var latestReviewOnlyCandidateCount = 0
    var latestEphemeralCandidateCount = 0
    var latestMemoriesWrittenCount = 0
    var latestWrittenCandidateCount = 0
    var latestSkippedCandidateCount = 0
    var latestSingleEvidenceDemotedCount = 0
    var latestMediaOrFileListingEphemeralCount = 0
    var latestWrittenCandidateTypes: [String] = []
    var latestSkippedReasons: [String] = []
    var latestWriteAuditSummary = ""
    var latestMemoryReportStatus = ""
    var latestMemoryReportError = ""
    var latestMemoryReportedAt = ""
    var latestMemoryReportAttemptCount = 0
    var latestCandidateSkipReason = ""
    var latestResultSummary = ""

    init() {}

    init(dictionary: [String: Any]) {
        enabled = dictionary["enabled"] as? Bool ?? false
        intervalMinutes = dictionary["interval_minutes"] as? Int ?? intervalMinutes
        triggerMode = dictionary["trigger_mode"] as? String ?? triggerMode
        hermesProfile = dictionary["hermes_profile"] as? String ?? hermesProfile
        status = dictionary["status"] as? String ?? status
        label = dictionary["label"] as? String ?? (enabled ? "已启用" : "未启用")
        kindName = dictionary["kind"] as? String ?? kindName
        summary = dictionary["summary"] as? String ?? summary
        lastGeneratedAt = dictionary["last_generated_at"] as? String ?? ""
        lastTriggeredAt = dictionary["last_triggered_at"] as? String ?? ""
        lastPackageId = dictionary["last_package_id"] as? String ?? ""
        lastError = dictionary["last_error"] as? String ?? ""
        pendingCount = dictionary["pending_count"] as? Int ?? 0
        failedCount = dictionary["failed_count"] as? Int ?? 0
        deadLetterCount = dictionary["dead_letter_count"] as? Int ?? 0
        historicalDeadLetterCount = dictionary["historical_dead_letter_count"] as? Int ?? 0
        unresolvedDeadLetterCount = dictionary["unresolved_dead_letter_count"] as? Int ?? max(0, deadLetterCount - historicalDeadLetterCount)
        retryDueCount = dictionary["retry_due_count"] as? Int ?? 0
        stalePendingCount = dictionary["stale_pending_count"] as? Int ?? 0
        memoryReportPendingCount = dictionary["memory_report_pending_count"] as? Int ?? 0
        memoryReportFailedCount = dictionary["memory_report_failed_count"] as? Int ?? 0
        memoryReportRetryDueCount = dictionary["memory_report_retry_due_count"] as? Int ?? 0
        activePackageCount = dictionary["active_package_count"] as? Int ?? 0
        activeAutoPackageCount = dictionary["active_auto_package_count"] as? Int ?? 0
        latestCandidateCount = dictionary["latest_candidate_count"] as? Int ?? 0
        latestEligibleCandidateCount = dictionary["latest_eligible_candidate_count"] as? Int ?? 0
        latestReviewOnlyCandidateCount = dictionary["latest_review_only_candidate_count"] as? Int ?? 0
        latestEphemeralCandidateCount = dictionary["latest_ephemeral_candidate_count"] as? Int ?? 0
        latestMemoriesWrittenCount = dictionary["latest_memories_written_count"] as? Int ?? 0
        latestWrittenCandidateCount = dictionary["latest_written_candidate_count"] as? Int ?? latestMemoriesWrittenCount
        latestSkippedCandidateCount = dictionary["latest_skipped_candidate_count"] as? Int ?? 0
        latestSingleEvidenceDemotedCount = dictionary["latest_single_evidence_demoted_count"] as? Int ?? 0
        latestMediaOrFileListingEphemeralCount = dictionary["latest_media_or_file_listing_ephemeral_count"] as? Int ?? 0
        latestWrittenCandidateTypes = dictionary["latest_written_candidate_types"] as? [String] ?? []
        latestSkippedReasons = dictionary["latest_skipped_reasons"] as? [String] ?? []
        latestWriteAuditSummary = dictionary["latest_write_audit_summary"] as? String ?? ""
        latestMemoryReportStatus = dictionary["latest_memory_report_status"] as? String ?? ""
        latestMemoryReportError = dictionary["latest_memory_report_error"] as? String ?? ""
        latestMemoryReportedAt = dictionary["latest_memory_reported_at"] as? String ?? ""
        latestMemoryReportAttemptCount = dictionary["latest_memory_report_attempt_count"] as? Int ?? 0
        latestCandidateSkipReason = dictionary["latest_candidate_skip_reason"] as? String ?? ""
        latestResultSummary = dictionary["latest_result_summary"] as? String ?? ""
    }

    var kind: StatusKind {
        switch kindName {
        case "ok": return .ok
        case "warning": return .warning
        case "danger": return .danger
        default: return .neutral
        }
    }

    var memoryReportLine: String {
        if memoryReportFailedCount > 0 {
            let errorText = latestMemoryReportError.isEmpty ? "等待补投。" : latestMemoryReportError
            return "飞书记忆系统汇报失败 \(memoryReportFailedCount) 个，已尝试 \(latestMemoryReportAttemptCount) 次。\(errorText)"
        }
        if memoryReportPendingCount > 0 {
            return "飞书记忆系统汇报待补投 \(memoryReportPendingCount) 个，可重试 \(memoryReportRetryDueCount) 个。"
        }
        if latestMemoryReportStatus == "sent" || latestMemoryReportStatus == "dry_run" {
            let mode = latestMemoryReportStatus == "dry_run" ? "dry-run 已验证" : "已汇报"
            return latestMemoryReportedAt.isEmpty ? "Hermes 写入后的飞书汇报\(mode)。" : "Hermes 写入后的飞书汇报\(mode)：\(latestMemoryReportedAt)。"
        }
        if latestMemoriesWrittenCount > 0 {
            return "Hermes 有写入记录，等待飞书记忆系统汇报状态刷新。"
        }
        return "最近无 mem0 写入，无需飞书汇报。"
    }
}

struct QualityAdvice {
    var enabled = true
    var profile = "activity-quality-analysis"
    var status = "ready"
    var label = "等待首次分析"
    var kindName = "neutral"
    var summary = "Hermes 质量顾问状态未知。"
    var latestRecommendationCount = 0
    var latestIssueCount = 0
    var latestMemoryCandidateCount = 0
    var latestDocumentMatchCount = 0
    var latestSummary = ""

    init() {}

    init(dictionary: [String: Any]) {
        enabled = dictionary["enabled"] as? Bool ?? enabled
        profile = dictionary["profile"] as? String ?? profile
        status = dictionary["status"] as? String ?? status
        label = dictionary["label"] as? String ?? (enabled ? "已启用" : "未启用")
        kindName = dictionary["kind"] as? String ?? kindName
        summary = dictionary["summary"] as? String ?? summary
        latestRecommendationCount = dictionary["latest_recommendation_count"] as? Int ?? 0
        latestIssueCount = dictionary["latest_issue_count"] as? Int ?? 0
        latestMemoryCandidateCount = dictionary["latest_memory_candidate_count"] as? Int ?? 0
        latestDocumentMatchCount = dictionary["latest_document_match_count"] as? Int ?? 0
        latestSummary = dictionary["latest_summary"] as? String ?? ""
    }

    var kind: StatusKind {
        switch kindName {
        case "ok": return .ok
        case "warning": return .warning
        case "danger": return .danger
        default: return .neutral
        }
    }
}

struct StructuredSignalHealth {
    var documentCandidateCount = 0
    var documentMatchCount = 0
    var documentMetadataOnlyCount = 0
    var summary = "结构化信号状态未知。"

    init() {}

    init(dictionary: [String: Any]) {
        documentCandidateCount = dictionary["document_candidate_count"] as? Int ?? 0
        documentMatchCount = dictionary["document_match_count"] as? Int ?? 0
        documentMetadataOnlyCount = dictionary["document_metadata_only_count"] as? Int ?? 0
        summary = dictionary["summary"] as? String ?? summary
    }
}

struct QualityHealth {
    var status = "unknown"
    var summary = "今天还没有质量报告。"
    var memoryCandidateCount = 0
    var displayCandidateCount = 0
    var totalCandidateCount = 0
    var writeInputCount = 0
    var memoryCandidateSkipReason = ""
    var eligibleCandidateCount = 0
    var reviewOnlyCandidateCount = 0
    var ephemeralCandidateCount = 0
    var singleEvidenceEligibleCount = 0
    var subjectRiskCandidateCount = 0
    var latestMemoriesWrittenCount = 0
    var latestWrittenCandidateCount = 0
    var latestSkippedCandidateCount = 0
    var singleEvidenceDemotedCount = 0
    var mediaOrFileListingEphemeralCount = 0
    var latestSkippedReasons: [String] = []
    var latestWriteAuditSummary = ""
    var memoryWriteSummary = "长期记忆写入健康状态未知。"
    var feedbackCount = 0
    var documentCandidateCount = 0
    var documentMatchCount = 0
    var attributionHealth = AttributionHealth()
    var memoryGovernanceTrend = MemoryGovernanceTrend()
    var recommendations: [String] = []

    init() {}

    init(dictionary: [String: Any]) {
        status = dictionary["status"] as? String ?? status
        summary = dictionary["summary"] as? String ?? summary
        memoryCandidateCount = dictionary["memory_candidate_count"] as? Int ?? 0
        displayCandidateCount = dictionary["display_candidate_count"] as? Int ?? memoryCandidateCount
        totalCandidateCount = dictionary["total_candidate_count"] as? Int ?? displayCandidateCount
        writeInputCount = dictionary["write_input_count"] as? Int ?? 0
        memoryCandidateSkipReason = dictionary["memory_candidate_skip_reason"] as? String ?? ""
        eligibleCandidateCount = dictionary["eligible_candidate_count"] as? Int ?? 0
        reviewOnlyCandidateCount = dictionary["review_only_candidate_count"] as? Int ?? 0
        ephemeralCandidateCount = dictionary["ephemeral_candidate_count"] as? Int ?? 0
        singleEvidenceEligibleCount = dictionary["single_evidence_eligible_count"] as? Int ?? 0
        subjectRiskCandidateCount = dictionary["subject_risk_candidate_count"] as? Int ?? 0
        latestMemoriesWrittenCount = dictionary["latest_memories_written_count"] as? Int ?? 0
        latestWrittenCandidateCount = dictionary["latest_written_candidate_count"] as? Int ?? latestMemoriesWrittenCount
        latestSkippedCandidateCount = dictionary["latest_skipped_candidate_count"] as? Int ?? 0
        singleEvidenceDemotedCount = dictionary["single_evidence_demoted_count"] as? Int ?? 0
        mediaOrFileListingEphemeralCount = dictionary["media_or_file_listing_ephemeral_count"] as? Int ?? 0
        latestSkippedReasons = dictionary["latest_skipped_reasons"] as? [String] ?? []
        latestWriteAuditSummary = dictionary["latest_write_audit_summary"] as? String ?? ""
        if let memoryWriteHealth = dictionary["memory_write_health"] as? [String: Any] {
            totalCandidateCount = memoryWriteHealth["total_candidate_count"] as? Int ?? totalCandidateCount
            displayCandidateCount = memoryWriteHealth["display_candidate_count"] as? Int ?? displayCandidateCount
            writeInputCount = memoryWriteHealth["write_input_count"] as? Int ?? writeInputCount
            eligibleCandidateCount = memoryWriteHealth["eligible_candidate_count"] as? Int ?? eligibleCandidateCount
            reviewOnlyCandidateCount = memoryWriteHealth["review_only_candidate_count"] as? Int ?? reviewOnlyCandidateCount
            ephemeralCandidateCount = memoryWriteHealth["ephemeral_candidate_count"] as? Int ?? ephemeralCandidateCount
            singleEvidenceEligibleCount = memoryWriteHealth["single_evidence_eligible_count"] as? Int ?? singleEvidenceEligibleCount
            subjectRiskCandidateCount = memoryWriteHealth["subject_risk_candidate_count"] as? Int ?? subjectRiskCandidateCount
            latestMemoriesWrittenCount = memoryWriteHealth["latest_memories_written_count"] as? Int ?? latestMemoriesWrittenCount
            latestWrittenCandidateCount = memoryWriteHealth["latest_written_candidate_count"] as? Int ?? latestWrittenCandidateCount
            latestSkippedCandidateCount = memoryWriteHealth["latest_skipped_candidate_count"] as? Int ?? latestSkippedCandidateCount
            singleEvidenceDemotedCount = memoryWriteHealth["single_evidence_demoted_count"] as? Int ?? singleEvidenceDemotedCount
            mediaOrFileListingEphemeralCount = memoryWriteHealth["media_or_file_listing_ephemeral_count"] as? Int ?? mediaOrFileListingEphemeralCount
            latestSkippedReasons = memoryWriteHealth["latest_skipped_reasons"] as? [String] ?? latestSkippedReasons
            latestWriteAuditSummary = memoryWriteHealth["latest_write_audit_summary"] as? String ?? latestWriteAuditSummary
            memoryWriteSummary = memoryWriteHealth["summary"] as? String ?? memoryWriteSummary
        }
        if totalCandidateCount == 0 { totalCandidateCount = max(displayCandidateCount, memoryCandidateCount, eligibleCandidateCount) }
        if displayCandidateCount == 0 { displayCandidateCount = memoryCandidateCount }
        if writeInputCount == 0 { writeInputCount = eligibleCandidateCount }
        feedbackCount = dictionary["feedback_count"] as? Int ?? 0
        documentCandidateCount = dictionary["document_candidate_count"] as? Int ?? 0
        documentMatchCount = dictionary["document_match_count"] as? Int ?? 0
        attributionHealth = AttributionHealth(dictionary: dictionary["attribution_health"] as? [String: Any] ?? [:])
        memoryGovernanceTrend = MemoryGovernanceTrend(dictionary: dictionary["memory_governance_trend"] as? [String: Any] ?? [:])
        recommendations = dictionary["recommendations"] as? [String] ?? []
    }

    var label: String {
        switch status {
        case "ok": return "正常"
        case "watch": return "观察"
        case "needs_review": return "需复核"
        default: return status.isEmpty ? "未知" : status
        }
    }

    var kind: StatusKind {
        switch status {
        case "ok": return .ok
        case "watch": return .warning
        case "needs_review": return .danger
        default: return .neutral
        }
    }

    var documentLine: String {
        if documentCandidateCount == 0 { return "当前范围没有文档候选。" }
        return "候选 \(documentCandidateCount) 条，语义匹配 \(documentMatchCount) 条。"
    }

    var memoryLine: String {
        if totalCandidateCount > 0 { return "展示 \(displayCandidateCount) 条，全量候选池 \(totalCandidateCount) 条。" }
        if !memoryCandidateSkipReason.isEmpty { return memoryCandidateSkipReason }
        return "当前范围没有长期记忆候选。"
    }

    var memoryWriteLine: String {
        if totalCandidateCount == 0 && !memoryWriteSummary.isEmpty { return memoryWriteSummary }
        return "可写池 \(eligibleCandidateCount)，实际输入 \(writeInputCount)，单证据可写 \(singleEvidenceEligibleCount)，主体风险 \(subjectRiskCandidateCount)。"
    }

    var memoryAuditLine: String {
        if !latestWriteAuditSummary.isEmpty { return latestWriteAuditSummary }
        let reasons = latestSkippedReasons.prefix(2).joined(separator: "、")
        let reasonText = reasons.isEmpty ? "暂无跳过原因摘要" : "跳过原因：\(reasons)"
        return "Hermes 写入 \(latestMemoriesWrittenCount) 条，跳过 \(latestSkippedCandidateCount) 条；单证据降级 \(singleEvidenceDemotedCount) 条，媒体/文件列表降级 \(mediaOrFileListingEphemeralCount) 条。\(reasonText)。"
    }



    var feedbackLine: String {
        feedbackCount > 0 ? "已有人工质量反馈，优先处理。" : "暂无人工反馈。"
    }
}

struct AttributionHealth {
    var status = "unknown"
    var summary = "归因健康状态未知。"
    var confidenceLabel = "unknown"
    var frameSignalCount = 0
    var knownAppFrameCount = 0
    var unknownAppFrameCount = 0
    var lowConfidenceContextCount = 0
    var secondaryDisplayFrameCount = 0
    var extensionBrowserSignalCount = 0
    var applescriptBrowserSignalCount = 0
    var accessibilitySignalCount = 0
    var fallbackContextCount = 0
    var crossDisplayInheritance = "disabled"

    init() {}

    init(dictionary: [String: Any]) {
        status = dictionary["status"] as? String ?? status
        summary = dictionary["summary"] as? String ?? summary
        confidenceLabel = dictionary["confidence_label"] as? String ?? confidenceLabel
        frameSignalCount = dictionary["frame_signal_count"] as? Int ?? 0
        knownAppFrameCount = dictionary["known_app_frame_count"] as? Int ?? 0
        unknownAppFrameCount = dictionary["unknown_app_frame_count"] as? Int ?? 0
        lowConfidenceContextCount = dictionary["low_confidence_context_count"] as? Int ?? 0
        secondaryDisplayFrameCount = dictionary["secondary_display_frame_count"] as? Int ?? 0
        extensionBrowserSignalCount = dictionary["extension_browser_signal_count"] as? Int ?? 0
        applescriptBrowserSignalCount = dictionary["applescript_browser_signal_count"] as? Int ?? 0
        accessibilitySignalCount = dictionary["accessibility_signal_count"] as? Int ?? 0
        fallbackContextCount = dictionary["fallback_context_count"] as? Int ?? 0
        crossDisplayInheritance = dictionary["cross_display_inheritance"] as? String ?? crossDisplayInheritance
    }

    var kind: StatusKind {
        switch status {
        case "ok": return .ok
        case "watch": return .warning
        case "observe": return .neutral
        default: return .neutral
        }
    }

    var label: String {
        switch status {
        case "ok": return "正常"
        case "watch": return "观察"
        case "observe": return "少量"
        case "no_frames": return "无帧"
        default: return "未知"
        }
    }

    var unknownLine: String {
        "Unknown \(unknownAppFrameCount)，低可信 \(lowConfidenceContextCount)，副屏 \(secondaryDisplayFrameCount)。"
    }

    var structuredLine: String {
        "扩展 \(extensionBrowserSignalCount)，兜底 \(applescriptBrowserSignalCount)，AX \(accessibilitySignalCount)。"
    }

    var boundaryLine: String {
        crossDisplayInheritance == "disabled" ? "副屏不继承主屏上下文。" : "跨屏继承需复查。"
    }
}

struct MemoryGovernanceTrend {
    var windowJobCount = 0
    var summary = "还没有可用于趋势统计的 HA 记忆治理记录。"
    var candidateCount = 0
    var eligibleCandidateCount = 0
    var reviewOnlyCandidateCount = 0
    var ephemeralCandidateCount = 0
    var subjectRiskCandidateCount = 0
    var singleEvidenceDemotedCount = 0

    init() {}

    init(dictionary: [String: Any]) {
        windowJobCount = dictionary["window_job_count"] as? Int ?? 0
        summary = dictionary["summary"] as? String ?? summary
        let totals = dictionary["totals"] as? [String: Any] ?? [:]
        candidateCount = totals["candidate_count"] as? Int ?? 0
        eligibleCandidateCount = totals["eligible_candidate_count"] as? Int ?? 0
        reviewOnlyCandidateCount = totals["review_only_candidate_count"] as? Int ?? 0
        ephemeralCandidateCount = totals["ephemeral_candidate_count"] as? Int ?? 0
        subjectRiskCandidateCount = totals["subject_risk_candidate_count"] as? Int ?? 0
        singleEvidenceDemotedCount = totals["single_evidence_demoted_count"] as? Int ?? 0
    }
}

struct MemoryLedgerStatus {
    var status = "unknown"
    var ledgerPath = ""
    var detailPath = ""
    var rawAccess = false
    var destructiveActions = false
    var entryCount = 0
    var currentMemoryCount = 0
    var canSyncCount = 0
    var pendingActionCount = 0
    var needsManualLookupCount = 0
    var unmatchedNewLineCount = 0
    var complexChangeCount = 0
    var hasLoaded = false

    init() {}

    init(jsonText: String) {
        self.init()
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            status = "error"
            hasLoaded = true
            return
        }
        status = object["status"] as? String ?? status
        ledgerPath = object["ledger_path"] as? String ?? ""
        detailPath = object["detail_path"] as? String ?? ""
        rawAccess = object["raw_access"] as? Bool ?? false
        destructiveActions = object["destructive_actions"] as? Bool ?? false
        entryCount = object["entry_count"] as? Int ?? 0
        currentMemoryCount = object["current_memory_count"] as? Int ?? 0
        canSyncCount = object["can_sync_count"] as? Int ?? 0
        pendingActionCount = object["pending_action_count"] as? Int ?? 0
        needsManualLookupCount = object["needs_manual_lookup_count"] as? Int ?? 0
        unmatchedNewLineCount = object["unmatched_new_line_count"] as? Int ?? 0
        complexChangeCount = object["complex_change_count"] as? Int ?? 0
        hasLoaded = true
    }

    init(dictionary: [String: Any]) {
        status = dictionary["status"] as? String ?? status
        ledgerPath = dictionary["ledger_path"] as? String ?? ""
        detailPath = dictionary["detail_path"] as? String ?? ""
        rawAccess = dictionary["raw_access"] as? Bool ?? false
        destructiveActions = dictionary["destructive_actions"] as? Bool ?? false
        entryCount = dictionary["entry_count"] as? Int ?? 0
        currentMemoryCount = dictionary["current_memory_count"] as? Int ?? 0
        canSyncCount = dictionary["can_sync_count"] as? Int ?? 0
        pendingActionCount = dictionary["pending_action_count"] as? Int ?? 0
        needsManualLookupCount = dictionary["needs_manual_lookup_count"] as? Int ?? 0
        unmatchedNewLineCount = dictionary["unmatched_new_line_count"] as? Int ?? 0
        complexChangeCount = dictionary["complex_change_count"] as? Int ?? 0
        hasLoaded = true
    }

    var hasUsefulData: Bool {
        !ledgerPath.isEmpty || entryCount > 0 || currentMemoryCount > 0 || ["ok", "frozen_legacy"].contains(status)
    }

    var displayName: String {
        guard !ledgerPath.isEmpty else { return "台账路径未返回" }
        return URL(fileURLWithPath: ledgerPath).lastPathComponent
    }

    var summaryLine: String {
        if !hasLoaded {
            return "等待读取台账状态。"
        }
        if status == "error" {
            return "无法解析台账状态；未创建或修改任何文件。"
        }
        if status == "frozen_legacy" {
            return "\(displayName)：历史台账已冻结，只读盘点 \(entryCount) 条；不会同步、改写或删除。"
        }
        return "\(displayName)：当前 \(currentMemoryCount) 条，主清单 \(entryCount) 条，可同步 \(canSyncCount) 条，待处理 \(pendingActionCount) 条。"
    }

    var statusLabel: String {
        if status == "error" { return "异常" }
        if status == "missing" { return "缺失" }
        if status == "frozen_legacy" { return "历史只读" }
        if pendingActionCount > 0 { return "待处理" }
        if complexChangeCount > 0 || unmatchedNewLineCount > 0 { return "需人工" }
        if status == "ok" { return "正常" }
        return status.isEmpty ? "未知" : status
    }

    var kind: StatusKind {
        if rawAccess || destructiveActions { return .danger }
        if status == "error" { return .danger }
        if status == "missing" { return .warning }
        if status == "frozen_legacy" { return .ok }
        if pendingActionCount > 0 || unmatchedNewLineCount > 0 || complexChangeCount > 0 {
            return .warning
        }
        if status == "ok" { return .ok }
        return .neutral
    }
}

struct MemoryIssueCount: Identifiable {
    let id: String
    let label: String
    let count: Int
}

struct MemoryAuditItem: Identifiable {
    let id = UUID()
    let issueTypes: [String]
    let safeSummary: String
    let recommendation: String

    init(dictionary: [String: Any]) {
        issueTypes = dictionary["issue_types"] as? [String] ?? []
        safeSummary = Self.safeText(dictionary["safe_summary"], fallback: "安全摘要为空。", maxLength: 260)
        recommendation = Self.safeText(dictionary["recommendation"], fallback: "", maxLength: 220)
    }

    var issueLabel: String {
        issueTypes.isEmpty ? "unknown_issue" : issueTypes.joined(separator: " / ")
    }

    private static func safeText(_ value: Any?, fallback: String, maxLength: Int) -> String {
        let text = (value as? String ?? fallback).trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.count > maxLength else { return text.isEmpty ? fallback : text }
        let end = text.index(text.startIndex, offsetBy: maxLength)
        return String(text[..<end]) + "..."
    }
}

struct MemoryAuditStatus {
    var status = "unknown"
    var summary = "等待读取长期记忆审计结果。"
    var rawAccess = false
    var destructiveActions = false
    var itemsChecked = 0
    var itemCount = 0
    var issueCounts: [String: Int] = [:]
    var items: [MemoryAuditItem] = []
    var ledger = MemoryLedgerStatus()
    var hasLoaded = false

    init() {}

    init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            status = "error"
            summary = "无法解析长期记忆审计结果；未执行删除或改写。"
            hasLoaded = true
            return
        }
        status = object["status"] as? String ?? status
        summary = object["summary"] as? String ?? summary
        rawAccess = object["raw_access"] as? Bool ?? false
        destructiveActions = object["destructive_actions"] as? Bool ?? false
        itemsChecked = object["items_checked"] as? Int ?? 0
        itemCount = object["item_count"] as? Int ?? 0
        issueCounts = object["issue_counts"] as? [String: Int] ?? [:]
        items = (object["items"] as? [[String: Any]] ?? []).map(MemoryAuditItem.init(dictionary:))
        if itemCount == 0 {
            itemCount = items.count
        }
        if let ledgerObject = object["ledger"] as? [String: Any] {
            ledger = MemoryLedgerStatus(dictionary: ledgerObject)
        }
        hasLoaded = true
    }

    var issueRows: [MemoryIssueCount] {
        issueCounts
            .map { key, value in MemoryIssueCount(id: key, label: key, count: value) }
            .sorted { left, right in
                if left.count == right.count { return left.label < right.label }
                return left.count > right.count
            }
    }

    var issueSummaryLine: String {
        if issueRows.isEmpty { return "无 issue 分布" }
        return issueRows.prefix(3).map { "\($0.label) \($0.count)" }.joined(separator: "；")
    }

    var personalMedicalRiskCount: Int {
        issueCounts["personal_medical_content_risk"] ?? 0
    }

    var summaryLine: String {
        if !hasLoaded { return "等待读取长期记忆审计结果。" }
        if status == "error" { return summary }
        return "\(summary) 检查 \(itemsChecked) 条，可疑 \(itemCount) 条。"
    }

    var statusLabel: String {
        if status == "error" { return "异常" }
        if itemCount > 0 { return "需复核" }
        if status == "ok" { return "正常" }
        return status.isEmpty ? "未知" : status
    }

    var kind: StatusKind {
        if rawAccess || destructiveActions { return .danger }
        if status == "error" { return .danger }
        if itemCount > 0 { return .warning }
        if status == "ok" { return .ok }
        return .neutral
    }
}

struct ActivitySessionRollup: Identifiable {
    let id = UUID()
    let label: String
    let category: String
    let durationSeconds: Int
    let durationLabel: String
    let share: Double
    let shareLabel: String
    let confidence: Double
    let rank: Int

    init(dictionary: [String: Any]) {
        label = MemoryApprovalMemory.safeText(dictionary["label"], fallback: "未分类", maxLength: 90)
        category = dictionary["category"] as? String ?? ""
        durationSeconds = Self.int(dictionary["duration_seconds"])
        durationLabel = dictionary["duration_label"] as? String ?? Self.durationLabel(durationSeconds)
        share = Self.double(dictionary["share"])
        shareLabel = dictionary["share_label"] as? String ?? Self.percent(share)
        confidence = Self.double(dictionary["confidence"])
        rank = Self.int(dictionary["rank"])
    }

    static func int(_ value: Any?) -> Int {
        if let intValue = value as? Int { return intValue }
        if let number = value as? NSNumber { return number.intValue }
        if let string = value as? String, let intValue = Int(string) { return intValue }
        return 0
    }

    static func double(_ value: Any?) -> Double {
        if let doubleValue = value as? Double { return doubleValue }
        if let number = value as? NSNumber { return number.doubleValue }
        if let string = value as? String, let doubleValue = Double(string) { return doubleValue }
        return 0
    }

    static func percent(_ value: Double) -> String {
        String(format: "%.1f%%", max(0, value) * 100)
    }

    static func durationLabel(_ seconds: Int) -> String {
        let value = max(0, seconds)
        let hours = value / 3600
        let minutes = (value % 3600) / 60
        if hours > 0 {
            return minutes > 0 ? "\(hours)h \(minutes)m" : "\(hours)h"
        }
        if minutes > 0 {
            return "\(minutes)m"
        }
        return "\(value)s"
    }
}

struct ScreenTimeBoundary {
    var source = "pam_local_timeline"
    var systemScreenTimeUsed = false
    var reason = "系统 Screen Time 明细未作为当前主数据源。"

    init() {}

    init(dictionary: [String: Any]) {
        source = dictionary["source"] as? String ?? source
        systemScreenTimeUsed = dictionary["system_screen_time_used"] as? Bool ?? false
        reason = MemoryApprovalMemory.safeText(dictionary["reason"], fallback: reason, maxLength: 220)
    }

    var statusLabel: String {
        systemScreenTimeUsed ? "已接入" : "未使用"
    }

    var kind: StatusKind {
        systemScreenTimeUsed ? .warning : .ok
    }
}

struct ActivityAttributionQuality {
    var status = "ok"
    var label = "归因质量正常"
    var unknownSeconds = 0
    var unknownShare = 0.0
    var unknownShareLabel = "0.0%"
    var lowConfidenceSeconds = 0
    var lowConfidenceShare = 0.0
    var lowConfidenceShareLabel = "0.0%"
    var visibleMediaSeconds = 0
    var visibleMediaShare = 0.0
    var visibleMediaShareLabel = "0.0%"
    var reasonCounts: [ActivityAttributionReasonCount] = []
    var explanationLines: [String] = []
    var rawAccess = false

    init() {}

    init(dictionary: [String: Any]) {
        status = dictionary["status"] as? String ?? status
        label = MemoryApprovalMemory.safeText(dictionary["label"], fallback: label, maxLength: 80)
        unknownSeconds = ActivitySessionRollup.int(dictionary["unknown_seconds"])
        unknownShare = ActivitySessionRollup.double(dictionary["unknown_share"])
        unknownShareLabel = dictionary["unknown_share_label"] as? String ?? ActivitySessionRollup.percent(unknownShare)
        lowConfidenceSeconds = ActivitySessionRollup.int(dictionary["low_confidence_seconds"])
        lowConfidenceShare = ActivitySessionRollup.double(dictionary["low_confidence_share"])
        lowConfidenceShareLabel = dictionary["low_confidence_share_label"] as? String ?? ActivitySessionRollup.percent(lowConfidenceShare)
        visibleMediaSeconds = ActivitySessionRollup.int(dictionary["visible_media_seconds"])
        visibleMediaShare = ActivitySessionRollup.double(dictionary["visible_media_share"])
        visibleMediaShareLabel = dictionary["visible_media_share_label"] as? String ?? ActivitySessionRollup.percent(visibleMediaShare)
        reasonCounts = (dictionary["attribution_reason_counts"] as? [[String: Any]] ?? []).map(ActivityAttributionReasonCount.init(dictionary:))
        explanationLines = (dictionary["explanation_lines"] as? [String] ?? []).map {
            MemoryApprovalMemory.safeText($0, fallback: "", maxLength: 180)
        }.filter { !$0.isEmpty }
        rawAccess = dictionary["raw_access"] as? Bool ?? false
    }

    var kind: StatusKind {
        if rawAccess { return .danger }
        if status == "poor" { return .danger }
        if status == "watch" { return .warning }
        if status == "ok" { return .ok }
        return .neutral
    }

    var visibleMediaLabel: String {
        visibleMediaSeconds > 0 ? ActivitySessionRollup.durationLabel(visibleMediaSeconds) : "0s"
    }

    var shortLine: String {
        if let reason = reasonCounts.first {
            return "Unknown \(unknownShareLabel)，低置信 \(lowConfidenceShareLabel)；主要原因 \(reason.label)。"
        }
        return "Unknown \(unknownShareLabel)，低置信 \(lowConfidenceShareLabel)。"
    }
}

struct ActivityAttributionReasonCount: Identifiable {
    var reason = ""
    var label = ""
    var count = 0

    var id: String { reason.isEmpty ? label : reason }

    init(dictionary: [String: Any]) {
        reason = MemoryApprovalMemory.safeText(dictionary["reason"], fallback: "", maxLength: 80)
        label = MemoryApprovalMemory.safeText(dictionary["label"], fallback: reason, maxLength: 80)
        count = ActivitySessionRollup.int(dictionary["count"])
    }
}

struct ActivitySession {
    var id = ""
    var startedAt = ""
    var endedAt = ""
    var effectiveEndedAt = ""
    var status = "unknown"
    var startReason = ""
    var endReason = ""
    var durationSeconds = 0
    var durationLabel = "0s"
    var coverageRatio = 0.0
    var sampleCount = 0
    var privacyEventCount = 0
    var gapEventCount = 0
    var primaryFocusShare: [ActivitySessionRollup] = []
    var visibleScreenShare: [ActivitySessionRollup] = []
    var topApps: [ActivitySessionRollup] = []
    var visibleApps: [ActivitySessionRollup] = []
    var topDomains: [ActivitySessionRollup] = []
    var visibleDomains: [ActivitySessionRollup] = []
    var summary = "暂无可归因活动。"
    var attributionQuality = ActivityAttributionQuality()
    var unknownShare = 0.0
    var lowConfidenceShare = 0.0
    var explanationLines: [String] = []
    var rawAccess = false

    init() {}

    init?(dictionary: [String: Any]?) {
        guard let dictionary else { return nil }
        id = dictionary["id"] as? String ?? ""
        startedAt = dictionary["started_at"] as? String ?? ""
        endedAt = dictionary["ended_at"] as? String ?? ""
        effectiveEndedAt = dictionary["effective_ended_at"] as? String ?? ""
        status = dictionary["status"] as? String ?? "unknown"
        startReason = dictionary["start_reason"] as? String ?? ""
        endReason = dictionary["end_reason"] as? String ?? ""
        durationSeconds = ActivitySessionRollup.int(dictionary["duration_seconds"])
        durationLabel = dictionary["duration_label"] as? String ?? ActivitySessionRollup.durationLabel(durationSeconds)
        coverageRatio = ActivitySessionRollup.double(dictionary["coverage_ratio"])
        sampleCount = ActivitySessionRollup.int(dictionary["sample_count"])
        privacyEventCount = ActivitySessionRollup.int(dictionary["privacy_event_count"])
        gapEventCount = ActivitySessionRollup.int(dictionary["gap_event_count"])
        primaryFocusShare = (dictionary["primary_focus_share"] as? [[String: Any]] ?? []).map(ActivitySessionRollup.init(dictionary:))
        visibleScreenShare = (dictionary["visible_screen_share"] as? [[String: Any]] ?? []).map(ActivitySessionRollup.init(dictionary:))
        topApps = (dictionary["top_apps"] as? [[String: Any]] ?? []).map(ActivitySessionRollup.init(dictionary:))
        visibleApps = (dictionary["visible_apps"] as? [[String: Any]] ?? []).map(ActivitySessionRollup.init(dictionary:))
        topDomains = (dictionary["top_domains"] as? [[String: Any]] ?? []).map(ActivitySessionRollup.init(dictionary:))
        visibleDomains = (dictionary["visible_domains"] as? [[String: Any]] ?? []).map(ActivitySessionRollup.init(dictionary:))
        summary = MemoryApprovalMemory.safeText(dictionary["summary"], fallback: summary, maxLength: 220)
        attributionQuality = ActivityAttributionQuality(dictionary: dictionary["attribution_quality"] as? [String: Any] ?? [:])
        unknownShare = ActivitySessionRollup.double(dictionary["unknown_share"])
        lowConfidenceShare = ActivitySessionRollup.double(dictionary["low_confidence_share"])
        explanationLines = (dictionary["explanation_lines"] as? [String] ?? []).map {
            MemoryApprovalMemory.safeText($0, fallback: "", maxLength: 180)
        }.filter { !$0.isEmpty }
        rawAccess = dictionary["raw_access"] as? Bool ?? false
    }

    var statusLabel: String {
        if status == "open" { return "进行中" }
        if status == "closed" { return "已结束" }
        return status.isEmpty ? "未知" : status
    }

    var statusKind: StatusKind {
        rawAccess ? .danger : (status == "open" ? .ok : .neutral)
    }

    var coverageLabel: String {
        ActivitySessionRollup.percent(coverageRatio)
    }

    var topPrimaryLabel: String {
        primaryFocusShare.first?.label ?? "暂无"
    }

    var workShareLabel: String {
        shareLabel(category: "work_ai", fallback: "工作 / AI 协作")
    }

    var mediaShareLabel: String {
        shareLabel(category: "media_entertainment", fallback: "媒体娱乐")
    }

    var screenTimeLine: String {
        "隐私保护 \(privacyEventCount)，缺口 \(gapEventCount)，覆盖 \(coverageLabel)，\(attributionQuality.shortLine)"
    }

    func shareLabel(category: String, fallback: String) -> String {
        let row = primaryFocusShare.first { $0.category == category || $0.label == fallback }
        return row?.shareLabel ?? "0.0%"
    }
}

struct ActivitySessionsAggregate {
    var totalDurationSeconds = 0
    var totalDurationLabel = "0s"
    var sessionCount = 0
    var workSeconds = 0
    var workShare = 0.0
    var mediaSeconds = 0
    var mediaShare = 0.0
    var topCategories: [ActivitySessionRollup] = []
    var topApps: [ActivitySessionRollup] = []

    init() {}

    init(dictionary: [String: Any]) {
        totalDurationSeconds = ActivitySessionRollup.int(dictionary["total_duration_seconds"])
        totalDurationLabel = dictionary["total_duration_label"] as? String ?? ActivitySessionRollup.durationLabel(totalDurationSeconds)
        sessionCount = ActivitySessionRollup.int(dictionary["session_count"])
        workSeconds = ActivitySessionRollup.int(dictionary["work_seconds"])
        workShare = ActivitySessionRollup.double(dictionary["work_share"])
        mediaSeconds = ActivitySessionRollup.int(dictionary["media_seconds"])
        mediaShare = ActivitySessionRollup.double(dictionary["media_share"])
        topCategories = (dictionary["top_categories"] as? [[String: Any]] ?? []).map(ActivitySessionRollup.init(dictionary:))
        topApps = (dictionary["top_apps"] as? [[String: Any]] ?? []).map(ActivitySessionRollup.init(dictionary:))
    }

    var workShareLabel: String {
        ActivitySessionRollup.percent(workShare)
    }

    var mediaShareLabel: String {
        ActivitySessionRollup.percent(mediaShare)
    }
}

struct ActivitySessionsToday {
    var sessionCount = 0
    var summary = ActivitySessionsAggregate()
    var screenTimeBoundary = ScreenTimeBoundary()

    init() {}

    init(dictionary: [String: Any]) {
        sessionCount = ActivitySessionRollup.int(dictionary["session_count"])
        summary = ActivitySessionsAggregate(dictionary: dictionary["summary"] as? [String: Any] ?? [:])
        screenTimeBoundary = ScreenTimeBoundary(dictionary: dictionary["screen_time_boundary"] as? [String: Any] ?? [:])
    }
}

struct ConfirmedPrimaryActivity {
    let summary: String
    let app: String
    let startedAt: String
    let endedAt: String
    let durationSeconds: Int
    let occurrenceState: String
    let confirmationSource: String

    init?(dictionary: [String: Any]?) {
        guard let dictionary else { return nil }
        let occurrenceState = dictionary["occurrence_state"] as? String ?? ""
        guard ["evidence_confirmed", "user_confirmed"].contains(occurrenceState) else { return nil }
        self.occurrenceState = occurrenceState
        summary = MemoryApprovalMemory.safeText(
            dictionary["summary"],
            fallback: "已确认活动",
            maxLength: 420
        )
        app = MemoryApprovalMemory.safeText(dictionary["app"], fallback: "", maxLength: 80)
        startedAt = dictionary["started_at"] as? String ?? ""
        endedAt = dictionary["ended_at"] as? String ?? ""
        durationSeconds = max(
            0,
            dictionary["duration_seconds"] as? Int
                ?? (dictionary["duration_seconds"] as? NSNumber)?.intValue
                ?? 0
        )
        confirmationSource = dictionary["confirmation_source"] as? String ?? ""
    }

    var durationLabel: String {
        ActivitySessionRollup.durationLabel(durationSeconds)
    }

    var sourceLabel: String {
        occurrenceState == "user_confirmed" ? "你已确认" : "直接证据确认"
    }

    var detailLine: String {
        [app, durationLabel, sourceLabel].filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

struct ConfirmedActivityStatus {
    var source = ""
    var rawAccess = false
    var rangeFrom = ""
    var rangeTo = ""
    var confirmedCount = 0
    var pendingCount = 0
    var userConfirmedCount = 0
    var userRejectedCount = 0
    var totalDurationSeconds = 0
    var primaryActivity: ConfirmedPrimaryActivity?

    init() {}

    init(dictionary: [String: Any]?) {
        guard let dictionary else { return }
        source = dictionary["source"] as? String ?? ""
        rawAccess = dictionary["raw_access"] as? Bool ?? true
        let timeRange = dictionary["time_range"] as? [String: Any] ?? [:]
        rangeFrom = timeRange["from"] as? String ?? ""
        rangeTo = timeRange["to"] as? String ?? ""
        confirmedCount = max(0, Self.int(dictionary["confirmed_count"]))
        pendingCount = max(0, Self.int(dictionary["pending_count"]))
        userConfirmedCount = max(0, Self.int(dictionary["user_confirmed_count"]))
        userRejectedCount = max(0, Self.int(dictionary["user_rejected_count"]))
        totalDurationSeconds = max(0, Self.int(dictionary["total_duration_seconds"]))
        if isAvailable {
            primaryActivity = ConfirmedPrimaryActivity(dictionary: dictionary["primary_activity"] as? [String: Any])
        } else {
            confirmedCount = 0
            pendingCount = 0
            userConfirmedCount = 0
            userRejectedCount = 0
            totalDurationSeconds = 0
            primaryActivity = nil
        }
    }

    var isAvailable: Bool {
        ConfirmedActivityContractPolicy.isReadable(source: source, rawAccess: rawAccess)
    }

    var totalDurationLabel: String {
        ActivitySessionRollup.durationLabel(totalDurationSeconds)
    }

    private static func int(_ value: Any?) -> Int {
        value as? Int ?? (value as? NSNumber)?.intValue ?? 0
    }
}

struct StatusSnapshot {
    var status = "unknown"
    var captureAllowedBySession = false
    var activities = 0
    var privacyEvents = 0
    var partialCleaningCountKnown = false
    var gaps = 0
    var rawResidue = 0
    var screenCaptureExpiresAt = ""
    var screenCaptureEnabled = false
    var recordingPaused = false
    var fileProgressLabel = "文件追踪状态待确认"
    var captureIntervalSeconds = 15
    var pausedRemainingSeconds = 0
    var lastCaptureAt = ""
    var nextCaptureAt = ""
    var lastError = ""
    var pauseMode = ""
    var pauseLabel = "未暂停"
    var statusExplanation = "等待状态更新。"
    var attentionItems: [AttentionItem] = []
    var soakScorecard = SoakScorecard()
    var sessionTransitionEvidence = SessionTransitionEvidenceStatus()
    var runtimeIdentity = RuntimeIdentity()
    var dashboardHealth = DashboardHealth()
    var captureFreshness = CaptureFreshness()
    var screenRecordingPermission = ScreenRecordingPermissionStatus()
    var coverageHealth = CoverageHealth()
    var privacyHealth = PrivacyHealth()
    var confirmationEvidenceHealth = ConfirmationEvidenceHealth()
    var hygieneCleanup = HygieneCleanup()
    var haAutomation = HAAutomation()
    var qualityAdvice = QualityAdvice()
    var structuredSignalHealth = StructuredSignalHealth()
    var qualityHealth = QualityHealth()
    var documentChanges = DocumentChangesSnapshot()
    var confirmedActivity = ConfirmedActivityStatus()
    var currentActivitySession: ActivitySession?
    var todayActivitySessions = ActivitySessionsToday()
    var launchAgentLoaded = false
    var launchAgentRunning = false
    var reasonCode = "status_not_loaded"
    var observedAt = ""
    var lastSuccessfulReadAt: String?
    var captureActionsAvailable = false
    var hasCurrentTrustedState = false
    var serviceStateKnown = false
    var countsStateKnown = false
    var requiresFullRefresh = true

    static func failClosedUnknown(
        observedAt: String,
        lastSuccessfulReadAt: String?,
        reasonCode: String
    ) -> StatusSnapshot {
        var snapshot = StatusSnapshot()
        snapshot.status = "unknown"
        snapshot.reasonCode = reasonCode.isEmpty ? "status_unknown" : reasonCode
        snapshot.observedAt = observedAt
        snapshot.lastSuccessfulReadAt = lastSuccessfulReadAt
        snapshot.captureAllowedBySession = false
        snapshot.captureActionsAvailable = false
        snapshot.hasCurrentTrustedState = false
        snapshot.serviceStateKnown = false
        snapshot.countsStateKnown = false
        snapshot.requiresFullRefresh = true
        snapshot.statusExplanation = snapshot.unknownStatusLine
        snapshot.pauseLabel = "状态未知"
        snapshot.documentChanges = DocumentChangesSnapshot()
        snapshot.confirmedActivity = ConfirmedActivityStatus()
        return snapshot
    }

    var isFailClosedUnknown: Bool {
        ["unknown", "busy", "unavailable"].contains(status.lowercased())
    }

    var displayStateUnknown: Bool {
        isFailClosedUnknown || !hasCurrentTrustedState || !serviceStateKnown || !countsStateKnown
    }

    var observationTimeLabel: String {
        observedAt.isEmpty ? "观察时间未知" : "观察时间 \(observedAt)"
    }

    var unknownStatusLine: String {
        "状态未知 · \(observationTimeLabel)"
    }

    var captureBlockedBySession: Bool {
        !captureAllowedBySession
    }

    var recordingControlAvailable: Bool {
        RecordingAvailability.allows(status: status, trusted: hasCurrentTrustedState && !displayStateUnknown,
                                     serviceKnown: serviceStateKnown)
    }

    var captureInteractionBlocked: Bool {
        !captureActionsAvailable || captureBlockedBySession || displayStateUnknown
    }

    var hasKnownCaptureError: Bool {
        !lastError.isEmpty && status != "session_state_unknown"
    }

    var statusLabel: String {
        if displayStateUnknown { return "status unknown" }
        if launchAgentLoaded && !launchAgentRunning { return "service error" }
        if !launchAgentLoaded && status != "capturing" { return "not recording" }
        if reasonCode.hasPrefix("document_worker_heartbeat_") { return "file tracking needs attention" }
        if reasonCode.hasPrefix("daemon_heartbeat_") { return "daemon needs attention" }
        if hasKnownCaptureError { return "error" }
        if status == "session_state_unknown" { return "session unknown" }
        if status == "paused" || status == "locked" { return status }
        if captureFreshness.isStale { return "stale capture" }
        return status
    }

    var serviceLabel: String {
        guard serviceStateKnown, !displayStateUnknown else { return "unknown" }
        return launchAgentRunning ? "running" : (launchAgentLoaded ? "error" : "stopped")
    }

    var serviceLabelCN: String {
        guard serviceStateKnown, !displayStateUnknown else { return "状态未知" }
        return launchAgentRunning ? "运行中" : (launchAgentLoaded ? "异常" : "已停止")
    }

    var recordingButtonTitle: String {
        guard captureActionsAvailable else { return "仅可刷新状态" }
        return launchAgentRunning ? "停止自动记录" : (launchAgentLoaded ? "重启自动记录" : "启动自动记录")
    }

    var coreHasCaptured: Bool {
        !lastCaptureAt.isEmpty || activities > 0
    }

    var statusLabelCN: String {
        if displayStateUnknown { return "状态未知" }
        if launchAgentLoaded && !launchAgentRunning { return "服务异常" }
        if !launchAgentLoaded && status != "capturing" { return "未记录" }
        if reasonCode.hasPrefix("document_worker_heartbeat_") { return "文件追踪需检查" }
        if reasonCode.hasPrefix("daemon_heartbeat_") { return "后台进度需检查" }
        if hasKnownCaptureError { return "错误" }
        if status == "paused" { return "已暂停" }
        if status == "locked" { return "已锁屏" }
        if status == "session_state_unknown" { return "会话状态未知" }
        if captureFreshness.isStale { return "采集停滞" }
        switch status {
        case "capturing": return "采集中"
        case "ready", "running": return "记录中"
        default: return status
        }
    }

    var statusKind: StatusKind {
        if displayStateUnknown { return .warning }
        if launchAgentLoaded && !launchAgentRunning { return .danger }
        if !launchAgentLoaded && status != "capturing" { return .warning }
        if status == "error" || hasKnownCaptureError { return .danger }
        if status == "paused" || captureBlockedBySession { return .warning }
        if captureFreshness.isStale { return .danger }
        if status == "capturing" || status == "ready" || status == "running" { return .ok }
        return .neutral
    }

    func nextCaptureSeconds(now: Date) -> Int? {
        guard launchAgentRunning, lastError.isEmpty else { return nil }
        guard status != "paused", !captureInteractionBlocked else { return nil }
        guard let next = Self.isoFormatter.date(from: nextCaptureAt) else {
            return lastCaptureAt.isEmpty ? 0 : nil
        }
        return max(0, Int(ceil(next.timeIntervalSince(now))))
    }

    func nextCaptureValue(now: Date) -> String {
        if displayStateUnknown { return "状态未知" }
        if !launchAgentRunning {
            return launchAgentLoaded ? "异常" : "未启动"
        }
        if status == "busy" {
            return "写库中"
        }
        if status == "paused" {
            if pauseMode == "until_lock" { return "到锁屏" }
            if pauseMode == "manual" { return "手动开启" }
            return formatDuration(pausedRemainingSeconds)
        }
        if status == "locked" {
            return "已暂停"
        }
        if captureBlockedBySession {
            return "已保护"
        }
        if captureFreshness.isStale {
            return "停滞"
        }
        guard lastError.isEmpty else { return "错误" }
        if let seconds = nextCaptureSeconds(now: now) {
            return seconds <= 0 ? "现在" : formatDuration(seconds)
        }
        return "等待中"
    }

    func nextCaptureSubtitle(now: Date) -> String {
        if displayStateUnknown { return unknownStatusLine }
        if !launchAgentRunning {
            return launchAgentLoaded ? "服务异常；暂停状态会保留" : "记录服务未启动；不会自动采集"
        }
        if status == "busy" {
            return "后台正在写入，稍后自动刷新"
        }
        if status == "paused" {
            return "\(pauseLabel)，不会保存关键帧或摘要"
        }
        if status == "locked" {
            return "锁屏期间停止采集；解锁后继续"
        }
        if status == "session_state_unknown" {
            return "无法确认当前会话，已按隐私优先停止采集"
        }
        if status == "unavailable" || status == "unknown" {
            return "状态证据暂时不可读，已停止手动采集入口"
        }
        if captureFreshness.isStale {
            return "服务在跑，但最近没有新采集"
        }
        if !lastError.isEmpty {
            return "先处理最近错误"
        }
        if nextCaptureSeconds(now: now) == 0 {
            return "正在等待下一轮写入"
        }
        return "每 \(captureIntervalSeconds) 秒采集一次 keyframe"
    }

    func nextCaptureStatusLine(now: Date) -> String {
        if displayStateUnknown { return unknownStatusLine }
        if !launchAgentRunning {
            return launchAgentLoaded ? "下次截屏：记录服务异常" : "下次截屏：记录服务未运行"
        }
        if status == "busy" {
            return "状态刷新：后台写入中"
        }
        if status == "paused" {
            return "下次截屏：\(pauseLabel)"
        }
        if status == "locked" {
            return "锁屏期间停止采集；解锁后继续"
        }
        if status == "session_state_unknown" {
            return "会话状态未知：停止采集"
        }
        if status == "unavailable" || status == "unknown" {
            return "状态不可读：已保护"
        }
        if captureFreshness.isStale {
            return "下次截屏：采集停滞"
        }
        if let seconds = nextCaptureSeconds(now: now) {
            return "下次截屏：\(formatDuration(seconds))"
        }
        return "下次截屏：等待采集状态"
    }

    func menuBarCountdownLabel(now: Date) -> String {
        if let seconds = nextCaptureSeconds(now: now) {
            if captureFreshness.isStale, seconds == 0,
               let rollingSeconds = rollingCaptureCountdownSeconds(now: now) {
                return "\(rollingSeconds)s"
            }
            return "\(seconds)s"
        }
        if let seconds = rollingCaptureCountdownSeconds(now: now) {
            return "\(seconds)s"
        }
        return "--"
    }

    func menuBarTitle(now: Date) -> String {
        menuBarCountdownLabel(now: now)
    }

    private func rollingCaptureCountdownSeconds(now: Date) -> Int? {
        guard launchAgentRunning,
              status != "paused",
              !captureInteractionBlocked,
              lastError.isEmpty,
              captureIntervalSeconds > 0,
              let last = Self.isoFormatter.date(from: lastCaptureAt)
        else {
            return nil
        }
        let elapsed = max(0, now.timeIntervalSince(last))
        let interval = Double(max(1, captureIntervalSeconds))
        let remainder = elapsed.truncatingRemainder(dividingBy: interval)
        if remainder == 0 {
            return 0
        }
        return max(0, Int(ceil(interval - remainder)))
    }

    private func formatDuration(_ seconds: Int) -> String {
        if seconds < 60 {
            return "\(max(0, seconds)) 秒"
        }
        let minutes = seconds / 60
        let remainder = seconds % 60
        if remainder == 0 {
            return "\(minutes) 分钟"
        }
        return "\(minutes) 分 \(remainder) 秒"
    }

    private static let isoFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()
}

struct ConfirmationEvidenceHealth {
    var contractVersion = 1
    var actionable = ConfirmationEvidenceHealthBucket()
    var historical = ConfirmationEvidenceHealthBucket()
    var rawAccess = false

    init(dictionary: [String: Any] = [:]) {
        contractVersion = dictionary["contract_version"] as? Int ?? 1
        actionable = ConfirmationEvidenceHealthBucket(
            dictionary: dictionary["actionable"] as? [String: Any] ?? dictionary
        )
        historical = ConfirmationEvidenceHealthBucket(
            dictionary: dictionary["historical"] as? [String: Any] ?? [:]
        )
        rawAccess = dictionary["raw_access"] as? Bool ?? false
    }
}

struct ConfirmationEvidenceHealthBucket {
    var status = "ok"
    var availableCount = 0
    var unavailableCount = 0
    var deletePendingCount = 0
    var retainedBytes = 0
    var errorCount = 0

    init(dictionary: [String: Any] = [:]) {
        status = dictionary["status"] as? String ?? "ok"
        availableCount = dictionary["available_count"] as? Int ?? 0
        unavailableCount = dictionary["unavailable_count"] as? Int ?? 0
        deletePendingCount = dictionary["delete_pending_count"] as? Int ?? 0
        retainedBytes = dictionary["retained_bytes"] as? Int ?? 0
        errorCount = dictionary["error_count"] as? Int ?? 0
    }
}

private final class CoreOutputBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()

    func append(_ newData: Data) {
        guard !newData.isEmpty else { return }
        lock.lock()
        data.append(newData)
        lock.unlock()
    }

    func snapshot(adding newData: Data = Data()) -> Data {
        lock.lock()
        if !newData.isEmpty {
            data.append(newData)
        }
        let current = data
        lock.unlock()
        return current
    }
}

private final class ActivityConfirmationParseBox: @unchecked Sendable {
    let snapshot: ActivityConfirmationSnapshot
    let pendingGroups: [ActivityConfirmationQuestionGroup]
    let mode: ActivityConfirmationModePreset?

    init(jsonText: String) {
        let snapshot = ActivityConfirmationSnapshot(jsonText: jsonText)
        self.snapshot = snapshot
        pendingGroups = snapshot.pendingGroups
        if !snapshot.isBlocked,
           let data = jsonText.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let modeValue = object["mode"] as? String {
            mode = ActivityConfirmationModePreset(rawValue: modeValue)
        } else {
            mode = nil
        }
    }
}

private final class ActivityConfirmationCardsParseBox: @unchecked Sendable {
    let page: ActivityConfirmationCardsPage

    init(jsonText: String) {
        page = ActivityConfirmationCardsPage(jsonText: jsonText)
    }
}

private final class ActivityConfirmationRevisionParseBox: @unchecked Sendable {
    let snapshot: ActivityConfirmationRevisionSnapshot

    init(jsonText: String) {
        snapshot = ActivityConfirmationRevisionSnapshot(jsonText: jsonText)
    }
}

@MainActor
final class CoreRunner: ObservableObject {
    @Published var statusLine = "Unknown"
    @Published var lastOutput = ""
    @Published var searchText = ""
    @Published var dailyReviewText = ""
    @Published var reviewDocumentChanges = DocumentChangesSnapshot()
    @Published var documentTrackingSnapshot = DocumentChangesSnapshot()
    @Published var documentChangesMessage = ""
    @Published var documentChangesLoading = false
    @Published var fileHistorySnapshot = DocumentChangesSnapshot()
    @Published var fileHistoryMessage = ""
    @Published var fileHistoryLoading = false
    @Published var markdownExportMessage = ""
    @Published var lastMarkdownExportPath = ""
    @Published var aiHandoffLastSummary = "尚未生成 AI 接手包。"
    @Published var aiConnectorStatus = AIConnectorStatus()
    @Published var aiConnectorLoading = false
    @Published var aiReviewSnapshot = AIReviewSnapshot()
    @Published var aiReviewLoading = false
    @Published var deleteText = ""
    @Published var statusSnapshot = StatusSnapshot()
    @Published var screenRecordingAllowed = CGPreflightScreenCaptureAccess()
    @Published var backupHygieneMessage = ""
    @Published var backupHygieneRunning = false
    @Published var diagnosticsStatus = DiagnosticsStatus()
    @Published var diagnosticsLoading = false
    @Published var privacyPolicyStatus = PrivacyPolicyStatus()
    @Published var privacyPolicyPreview = PrivacyPolicyPreview()
    @Published var privacyPolicyLoading = false
    @Published var memoryReviewStatus = MemoryReviewStatus()
    @Published var memoryReviewList = MemoryReviewList()
    @Published var memoryReviewPreview = MemoryReviewPreview()
    @Published var memoryReviewLoading = false
    @Published var memoryReviewMessage = ""
    @Published var memoryRevealSession = MemoryRevealSession()
    @Published var memoryReviewRevealedText = ""
    @Published var memoryReviewRevealedID = ""
    @Published var memoryLedgerStatus = MemoryLedgerStatus()
    @Published var memoryAuditStatus = MemoryAuditStatus()
    @Published var memoryApprovalStatus = MemoryApprovalStatus()
    @Published var memoryApprovalCandidates = MemoryApprovalCandidates()
    @Published var memoryGovernanceMessage = ""
    @Published var memoryGovernanceLoading = false
    @Published var memoryAuditExportPath = ""
    @Published var activityConfirmationSnapshot = ActivityConfirmationSnapshot()
    @Published var activityConfirmationPendingGroups: [ActivityConfirmationQuestionGroup] = []
    @Published var activityConfirmationLoading = false
    @Published var activityConfirmationLoadingMore = false
    @Published var activityConfirmationUsesFastCards = false
    @Published var activityConfirmationHasMore = false
    @Published var activityConfirmationInsufficientGroups: [ActivityConfirmationQuestionGroup] = []
    @Published var activityConfirmationInsufficientLoading = false
    @Published var activityConfirmationInsufficientHasMore = false
    private var activityConfirmationInsufficientNextCursor = ""
    @Published var activityConfirmationDiagnosticsSnapshot = ActivityConfirmationSnapshot()
    @Published var activityConfirmationDiagnosticsLoading = false
    @Published var activityConfirmationMessage = ""
    @Published var activityConfirmationMessageSeverity: ActivityConfirmationClarificationSeverity = .neutral
    @Published var activityConfirmationWorkingGroupKeys: Set<String> = []
    @Published var activityConfirmationClarifyingGroupKeys: Set<String> = []
    @Published var activityConfirmationClarifications: [String: ActivityConfirmationClarification] = [:]
    @Published var activityConfirmationMode = ActivityConfirmationModePreset(
        rawValue: UserDefaults.standard.string(forKey: "pam.activity-confirmation.mode") ?? ""
    ) ?? .standard
    @Published var activityConfirmationConfigurationLoading = false
    @Published var activityConfirmationConfigurationMessage = ""
    @Published var manualActivityEntryLoading = false
    @Published var manualActivityEntryMessage = ""
    @Published var decisionQueueSnapshot = DecisionQueueSnapshot()
    @Published var decisionQueueLoading = false
    @Published var decisionQueueMessage = ""
    @Published var lastStatusUpdatedAt: Date?
    @Published var lastMemoryGovernanceUpdatedAt: Date?
    @Published var lastActivityConfirmationUpdatedAt: Date?
    @Published var lastDecisionQueueUpdatedAt: Date?
    @Published private var captureFeedbackUntil: Date?
    private var ticker: Timer?
    @Published private(set) var isRefreshingStatus = false
    private let statusRefreshReads = ProductionStatusRefreshReads()
    private var productionStatusState = ProductionStatusClientState()
    private var productionDiagnosticsState = ProductionDiagnosticsClientState()
    private var lastStatusRefreshAt = Date.distantPast
    private var lastFullStatusRefreshAt = Date.distantPast
    private var zeroCountdownArmed = true
    private var reviewRequestID = 0
    private var searchRequestID = 0
    private var aiReviewRequestID = 0
    private var memoryGovernanceLoaded = false
    private var activityConfirmationLoaded = false
    private var activityConfirmationSnapshotMessage = ""
    private var activityConfirmationRevision = ""
    private var activityConfirmationNextCursor = ""
    private var activityConfirmationRefreshGeneration: UInt64 = 0
    private var activityConfirmationMutationGeneration: UInt64 = 0
    private var activityConfirmationRefreshNeeded = false
    private var decisionQueueLoaded = false
    private var diagnosticsLoaded = false
    private var controlWindowVisible = false
    private var menuVisible = false
    private var activityConfirmationViewVisible = false
    private var tickDate = Date()

    var exportStatusSystemImage: String {
        if markdownExportMessage == "导出已取消。" {
            return "xmark.circle"
        }
        if exportMessageIsFailure {
            return "exclamationmark.triangle"
        }
        return "checkmark.circle"
    }

    var exportStatusColor: Color {
        if markdownExportMessage == "导出已取消。" {
            return .secondary
        }
        if exportMessageIsFailure {
            return .orange
        }
        return .green
    }

    private var exportMessageIsFailure: Bool {
        let failureTerms = ["失败", "exited with", "Traceback", "Could not run", "Error:"]
        return failureTerms.contains { markdownExportMessage.localizedCaseInsensitiveContains($0) }
    }

    var memoryGovernanceMessageIsFailure: Bool {
        let failureTerms = ["失败", "异常", "无法解析", "不存在", "exited with", "Traceback", "Could not run", "Error:"]
        return failureTerms.contains { memoryGovernanceMessage.localizedCaseInsensitiveContains($0) }
    }

    var decisionQueueMessageIsFailure: Bool {
        let failureTerms = ["失败", "异常", "无法解析", "不存在", "exited with", "Traceback", "Could not run", "Error:"]
        return failureTerms.contains { decisionQueueMessage.localizedCaseInsensitiveContains($0) }
    }

    private func setActivityConfirmationMessage(
        _ message: String,
        severity: ActivityConfirmationClarificationSeverity
    ) {
        activityConfirmationMessage = message
        activityConfirmationMessageSeverity = severity
    }

    private func setActivityConfirmationSnapshotMessage(
        _ message: String,
        severity: ActivityConfirmationClarificationSeverity
    ) {
        setActivityConfirmationMessage(message, severity: severity)
        activityConfirmationSnapshotMessage = message
    }

    private func clearActivityConfirmationSnapshotMessage() {
        // A successful read clears its own warning, preserving any newer
        // answer or clarification feedback shown in the meantime.
        if activityConfirmationMessage == activityConfirmationSnapshotMessage {
            setActivityConfirmationMessage("", severity: .neutral)
        }
        activityConfirmationSnapshotMessage = ""
    }

    var memoryRawAccessEnabled: Bool {
        effectiveMemoryLedgerStatus.rawAccess || memoryAuditStatus.rawAccess || memoryApprovalStatus.rawAccess || memoryApprovalCandidates.rawAccess
    }

    var memoryDestructiveActionsEnabled: Bool {
        effectiveMemoryLedgerStatus.destructiveActions || memoryAuditStatus.destructiveActions || memoryApprovalStatus.destructiveActions || memoryApprovalCandidates.destructiveActions
    }

    var effectiveMemoryLedgerStatus: MemoryLedgerStatus {
        if memoryLedgerStatus.hasUsefulData || !memoryAuditStatus.ledger.hasUsefulData {
            return memoryLedgerStatus
        }
        return memoryAuditStatus.ledger
    }

    var memoryGovernanceUpdatedLabel: String {
        guard let lastMemoryGovernanceUpdatedAt else { return "等待读取" }
        return "更新 \(Self.shortTimeFormatter.string(from: lastMemoryGovernanceUpdatedAt))"
    }

    var decisionQueueUpdatedLabel: String {
        guard let lastDecisionQueueUpdatedAt else { return "等待读取" }
        return "更新 \(Self.shortTimeFormatter.string(from: lastDecisionQueueUpdatedAt))"
    }

    var activityConfirmationUpdatedLabel: String {
        guard let lastActivityConfirmationUpdatedAt else { return "等待读取" }
        return "更新 \(Self.shortTimeFormatter.string(from: lastActivityConfirmationUpdatedAt))"
    }

    init() {
        // Keep ticking while the menu is open; menu tracking does not run default-mode timers.
        let timer = Timer(timeInterval: 1, repeats: true) { _ in
            Task { @MainActor [weak self] in
                self?.tick()
            }
        }
        timer.tolerance = 0.1
        RunLoop.main.add(timer, forMode: .common)
        ticker = timer
    }

    deinit {
        ticker?.invalidate()
    }

    var screenRecordingWarningActive: Bool {
        if statusSnapshot.screenRecordingPermission.denied { return true }
        if statusSnapshot.screenRecordingPermission.observedGranted { return false }
        return statusSnapshot.launchAgentRunning
            && statusSnapshot.captureAllowedBySession
            && statusSnapshot.status != "paused"
            && statusSnapshot.status != "locked"
            && statusSnapshot.status != "session_state_unknown"
    }

    func captureFeedbackActive(at date: Date = Date()) -> Bool {
        guard let captureFeedbackUntil else { return false }
        return date < captureFeedbackUntil
    }

    private func tick() {
        tickDate = Date()
        updateCaptureFeedback()
        let elapsed = tickDate.timeIntervalSince(lastStatusRefreshAt)
        let interactive = controlWindowVisible || menuVisible
        let quickInterval: TimeInterval = interactive ? 5 : 30
        let fullInterval: TimeInterval = interactive ? 180 : 300
        let shouldRefresh = elapsed >= quickInterval
        if shouldRefresh {
            let fullRefreshDue = tickDate.timeIntervalSince(lastFullStatusRefreshAt) >= fullInterval
            Task {
                if fullRefreshDue {
                    await refreshStatus(coalescingIfLoading: true)
                } else {
                    await refreshQuickStatus()
                }
            }
        }
        let confirmationInterval: TimeInterval = interactive ? 60 : 300
        let lastConfirmationRefresh = lastActivityConfirmationUpdatedAt ?? .distantPast
        if activityConfirmationViewVisible,
           controlWindowVisible,
           tickDate.timeIntervalSince(lastConfirmationRefresh) >= confirmationInterval,
           !activityConfirmationLoading {
            Task { await refreshActivityConfirmationsIfNeeded(maxAge: confirmationInterval) }
        }
    }

    func setControlWindowVisible(_ visible: Bool) {
        controlWindowVisible = visible
        if !visible { clearRevealedMemory() }
    }

    func setMenuVisible(_ visible: Bool) {
        menuVisible = visible
    }

    func setActivityConfirmationViewVisible(_ visible: Bool) {
        activityConfirmationViewVisible = visible
    }

    private func updateCaptureFeedback() {
        if let captureFeedbackUntil, tickDate >= captureFeedbackUntil {
            self.captureFeedbackUntil = nil
        }
        guard statusSnapshot.launchAgentRunning,
              statusSnapshot.lastError.isEmpty,
              statusSnapshot.status != "paused",
              !statusSnapshot.captureInteractionBlocked,
              !statusSnapshot.captureFreshness.isStale,
              !statusSnapshot.lastCaptureAt.isEmpty,
              let seconds = statusSnapshot.nextCaptureSeconds(now: tickDate)
        else {
            zeroCountdownArmed = true
            return
        }

        if seconds > 0 {
            zeroCountdownArmed = true
            return
        }

        guard zeroCountdownArmed else { return }
        zeroCountdownArmed = false
        captureFeedbackUntil = tickDate.addingTimeInterval(1.4)
        Task {
            try? await Task.sleep(nanoseconds: 600_000_000)
            await refreshQuickStatus()
        }
    }

    func refreshStatus(coalescingIfLoading: Bool = false) async {
        isRefreshingStatus = true
        defer {
            if !statusRefreshReads.isRunning { isRefreshingStatus = false }
        }
        await statusRefreshReads.request(
            .full, coalescingFull: coalescingIfLoading,
            quick: { [self] in await performQuickStatusRead() },
            full: { [self] in await performFullStatusRead() }
        )
    }

    private func performFullStatusRead() async {
        defer { lastStatusRefreshAt = Date() }
        screenRecordingAllowed = CGPreflightScreenCaptureAccess()
        let result = await runCore(["status"])
        lastOutput = result
        if let parsed = snapshotFromJSON(result) {
            statusSnapshot = parsed
            if !documentChangesLoading {
                documentTrackingSnapshot = statusSnapshot.documentChanges
            }
            if parsed.hasCurrentTrustedState && !parsed.requiresFullRefresh {
                if let successfulReadAt = parsed.lastSuccessfulReadAt,
                   let successfulDate = Self.statusObservationDate(from: successfulReadAt) {
                    lastFullStatusRefreshAt = Date()
                    lastStatusUpdatedAt = successfulDate
                } else {
                    statusSnapshot = failClosedStatusSnapshot(
                        reasonCode: "invalid_successful_status_time",
                        interface: .fullStatus
                    )
                    if !documentChangesLoading {
                        documentTrackingSnapshot = statusSnapshot.documentChanges
                    }
                    lastFullStatusRefreshAt = .distantPast
                }
            } else {
                // Unknown/busy/unavailable is an observation, not a successful
                // status read.  Force the next interaction through full status
                // without advancing the last-success timestamp.
                lastFullStatusRefreshAt = .distantPast
            }
        } else {
            statusSnapshot = failClosedStatusSnapshot(
                reasonCode: "invalid_full_status_payload",
                interface: .fullStatus
            )
            if !documentChangesLoading {
                documentTrackingSnapshot = statusSnapshot.documentChanges
            }
            lastFullStatusRefreshAt = .distantPast
        }
        statusLine = statusLine(from: statusSnapshot)
        LocalNotificationCoordinator.shared.updateSystemHealth(snapshot: statusSnapshot)
    }

    func refreshForInteraction() async {
        let fullEvidenceDue = lastStatusUpdatedAt == nil
            || Date().timeIntervalSince(lastFullStatusRefreshAt) >= 180
        if fullEvidenceDue {
            await refreshStatus()
        } else {
            await refreshQuickStatus()
        }
    }

    func refreshQuickStatus() async {
        isRefreshingStatus = true
        defer {
            if !statusRefreshReads.isRunning { isRefreshingStatus = false }
        }
        await statusRefreshReads.request(
            .quick, coalescingFull: true,
            quick: { [self] in await performQuickStatusRead() },
            full: { [self] in await performFullStatusRead() }
        )
    }

    private func performQuickStatusRead() async -> Bool {
        defer { lastStatusRefreshAt = Date() }
        let result = await runCore(["status", "--quick"])
        lastOutput = result
        if let parsed = quickSnapshotFromJSON(result, preserving: statusSnapshot) {
            statusSnapshot = parsed
            if !parsed.isFailClosedUnknown,
               let successfulReadAt = parsed.lastSuccessfulReadAt,
               let successfulDate = Self.statusObservationDate(from: successfulReadAt) {
                lastStatusUpdatedAt = successfulDate
            }
            if parsed.requiresFullRefresh || parsed.isFailClosedUnknown {
                if !documentChangesLoading {
                    documentTrackingSnapshot = parsed.documentChanges
                }
                lastFullStatusRefreshAt = .distantPast
            }
        } else {
            statusSnapshot = failClosedStatusSnapshot(
                reasonCode: "invalid_quick_status_payload",
                interface: .quickStatus
            )
            if !documentChangesLoading {
                documentTrackingSnapshot = statusSnapshot.documentChanges
            }
            lastFullStatusRefreshAt = .distantPast
        }
        statusLine = statusLine(from: statusSnapshot)
        LocalNotificationCoordinator.shared.updateSystemHealth(snapshot: statusSnapshot)
        return statusSnapshot.requiresFullRefresh
    }

    var statusUpdatedLabel: String {
        guard let lastStatusUpdatedAt else { return "等待完整状态" }
        return "上次成功 " + Self.shortTimeFormatter.string(from: lastStatusUpdatedAt)
    }

    private func productionMutationAllowed(
        _ operation: String,
        requiresCaptureControls: Bool = true
    ) -> Bool {
        guard !statusSnapshot.displayStateUnknown,
              statusSnapshot.hasCurrentTrustedState
        else {
            lastOutput = "状态未知；仅允许刷新状态，已阻止\(operation)。"
            return false
        }
        if requiresCaptureControls && !statusSnapshot.captureActionsAvailable {
            lastOutput = "当前隐私或会话状态不允许\(operation)。"
            return false
        }
        return true
    }

    func pause(minutes: Int = 15) async {
        guard statusSnapshot.recordingControlAvailable, productionMutationAllowed("暂停记录", requiresCaptureControls: false) else { return }
        lastOutput = await runCore(["pause", "--minutes", "\(minutes)"])
        await refreshStatus()
    }

    func pauseUntilLock() async {
        guard statusSnapshot.recordingControlAvailable, productionMutationAllowed("暂停到锁屏", requiresCaptureControls: false) else { return }
        lastOutput = await runCore(["pause", "--until-lock"])
        await refreshStatus()
    }

    func pauseToday() async {
        guard statusSnapshot.recordingControlAvailable, productionMutationAllowed("今天不记录", requiresCaptureControls: false) else { return }
        lastOutput = await runCore(["pause", "--today"])
        await refreshStatus()
    }

    func pauseUntilManualResume() async {
        guard statusSnapshot.recordingControlAvailable, productionMutationAllowed("手动暂停", requiresCaptureControls: false) else { return }
        lastOutput = await runCore(["pause", "--manual"])
        await refreshStatus()
    }

    func codexSharing(action: String, payload: [String: Any]) async -> String {
        await contextSharing(consumer: .codex, action: action, payload: payload)
    }

    func contextSharing(consumer: ContextConsumer, action: String, payload: [String: Any]) async -> String {
        if action != "snapshot" && !productionMutationAllowed("更新共享摘要", requiresCaptureControls: false) {
            return "{\"status\":\"rejected\"}"
        }
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let input = String(data: data, encoding: .utf8) else { return "{}" }
        return await runCore([consumer == .agi ? "agi-sharing" : "codex-sharing", "--action", action, "--payload-stdin"], standardInput: input)
    }

    func feedbackRequest(_ action: String, payload: [String: Any] = [:]) async -> String {
        let allowed = ["status", "detail", "pause", "resume", "stop", "decision", "notifications", "notifications-ack", "autonomy-pause", "autonomy-resume"]
        guard allowed.contains(action) else { return "{}" }
        let data = try? JSONSerialization.data(withJSONObject: payload)
        let input = data.flatMap { String(data: $0, encoding: .utf8) }
        let python = Self.pythonExecutableURL(), core = Self.coreDirectory()
        return await Task.detached {
            Self.runCoreBlocking([action, "--payload-stdin"], standardInput: input, pythonURL: python, coreURL: core,
                module: "activity_mvp.feedback.cli", timeoutOverride: 12)
        }.value
    }

    func reflectionRequest(_ action: String, payload: [String: Any] = [:]) async -> String {
        guard ["status", "list", "detail", "configure", "analyze", "feedback"].contains(action),
              let data = try? JSONSerialization.data(withJSONObject: payload),
              let input = String(data: data, encoding: .utf8) else { return "{}" }
        let python = Self.pythonExecutableURL(), core = Self.coreDirectory()
        return await Task.detached {
            Self.runCoreBlocking([action, "--payload-stdin"], standardInput: input, pythonURL: python, coreURL: core,
                module: "activity_mvp.reflection.cli", timeoutOverride: 12)
        }.value
    }

    func evolutionRequest(_ action: EvolutionAction) async -> String {
        let python = Self.pythonExecutableURL(), core = Self.coreDirectory()
        return await Task.detached {
            Self.runCoreBlocking([action.rawValue], standardInput: nil, pythonURL: python, coreURL: core,
                module: "activity_mvp.evolution_bridge", timeoutOverride: action == .replay ? 45 : 12)
        }.value
    }

    func beginScreenCapture() async {
        guard productionMutationAllowed("采集 30 分钟", requiresCaptureControls: false) else { return }
        lastOutput = await runCore(["configure-screen-capture", "--enabled", "on"])
        await refreshStatus()
    }

    func endScreenCapture() async {
        guard productionMutationAllowed("结束屏幕采集", requiresCaptureControls: false) else { return }
        lastOutput = await runCore(["configure-screen-capture", "--enabled", "off"])
        await refreshStatus()
    }

    func resume() async {
        guard statusSnapshot.recordingControlAvailable, productionMutationAllowed("继续记录", requiresCaptureControls: false) else { return }
        lastOutput = await runCore(["resume"])
        await refreshStatus()
    }

    func restoreRecording() async {
        if statusSnapshot.launchAgentRunning {
            await resume()
        } else {
            await startRecordingService()
        }
    }

    func startRecordingService() async {
        guard statusSnapshot.recordingControlAvailable, productionMutationAllowed("启动记录服务", requiresCaptureControls: false) else { return }
        lastOutput = await runCore(["install-launch-agent", "--preserve-privacy-profile"])
        await refreshStatus()
    }

    @discardableResult
    func stopRecordingService(requiresCaptureControls: Bool = true) async -> Bool {
        guard productionMutationAllowed(
            "停止记录服务",
            requiresCaptureControls: requiresCaptureControls
        ) else { return false }
        _ = await runCore(["configure-screen-capture", "--enabled", "off"])
        lastOutput = await runCore(["uninstall-launch-agent"])
        await refreshStatus()
        return !statusSnapshot.launchAgentLoaded && !statusSnapshot.launchAgentRunning
    }

    func stopRecordingAndQuit() async {
        await EvolutionModel.shared.stopAll(runner: self)
        if EvolutionModel.shared.mode == "stopped" && EvolutionModel.shared.available
            && !statusSnapshot.displayStateUnknown && !statusSnapshot.launchAgentLoaded && !statusSnapshot.launchAgentRunning {
            NSApp.terminate(nil)
        } else {
            markdownExportMessage = EvolutionModel.shared.stopResults
            AppWindowPresenter.shared.show(runner: self, tab: .evolution)
        }
    }

    func captureNow() async {
        guard productionMutationAllowed("立即采集") else { return }
        lastOutput = await runCore(["capture-keyframes"])
        await refreshStatus()
    }





    func runBackupHygieneCheck() async {
        if backupHygieneRunning { return }
        backupHygieneRunning = true
        backupHygieneMessage = "正在运行只读卫生检查..."
        let raw = await runCore(["backup-hygiene"])
        backupHygieneMessage = backupHygieneSummary(from: raw)
        backupHygieneRunning = false
        await refreshStatus()
    }

    func refreshDiagnosticsStatus(force: Bool = false, mode: String = "quick") async {
        if diagnosticsLoading { return }
        if diagnosticsLoaded && !force { return }
        diagnosticsLoading = true
        let raw = await runCore(["diagnostics", "status", "--mode", mode])
        if let data = raw.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            _ = productionDiagnosticsState.apply(
                dictionary: object,
                requestedMode: mode
            )
        } else {
            _ = productionDiagnosticsState.apply(
                dictionary: [
                    "status": "invalid",
                    "raw_access": false,
                ],
                requestedMode: mode
            )
        }
        diagnosticsStatus = DiagnosticsStatus(productionState: productionDiagnosticsState)
        diagnosticsLoaded = true
        diagnosticsLoading = false
    }

    func refreshMemoryReview(force: Bool = false) async {
        if memoryReviewLoading { return }
        if !force && memoryReviewStatus.ok && !memoryReviewList.items.isEmpty { return }
        memoryReviewLoading = true
        memoryReviewStatus = MemoryReviewStatus(jsonText: await runCore(["memory-review", "status"]))
        memoryReviewList = MemoryReviewList(
            jsonText: await runCore(["memory-review", "list", "--limit", "500"])
        )
        memoryReviewLoading = false
    }

    /// Reveal one memory's text on demand. Nothing is cached to disk.
    func revealMemory(_ memoryID: String, presentationID: UUID? = nil) async {
        let token = memoryRevealSession.begin(memoryID, presentationID: presentationID)
        memoryReviewRevealedID = memoryID
        memoryReviewRevealedText = ""
        let raw = await runCore(["memory-review", "reveal", "--memory-id", memoryID])
        memoryRevealSession.receive(raw, token: token)
        memoryReviewRevealedID = memoryRevealSession.identifier
        memoryReviewRevealedText = memoryRevealSession.text
    }

    @discardableResult func clearRevealedMemory(presentationID: UUID? = nil) -> Bool {
        guard memoryRevealSession.close(presentationID: presentationID) else { return false }
        memoryReviewRevealedID = ""
        memoryReviewRevealedText = ""
        return true
    }

    private func encodedDecisions(_ decisions: [String: String]) -> String? {
        let payload = decisions.map { ["memory_id": $0.key, "action": $0.value, "note": "AGI 记忆库页面"] }
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    func previewMemoryReviewDecisions(_ decisions: [String: String]) async {
        if memoryReviewLoading || decisions.isEmpty { return }
        guard let encoded = encodedDecisions(decisions) else {
            memoryReviewMessage = "无法编码待提交的裁决。"
            return
        }
        memoryReviewLoading = true
        memoryReviewPreview = MemoryReviewPreview(
            jsonText: await runCore(["memory-review", "preview", "--decisions", encoded])
        )
        memoryReviewMessage = memoryReviewPreview.errorMessage.isEmpty
            ? "已生成预览，确认后才会写入。"
            : memoryReviewPreview.errorMessage
        memoryReviewLoading = false
    }

    func applyMemoryReviewDecisions(_ decisions: [String: String]) async -> Bool {
        guard memoryReviewPreview.isReady else {
            memoryReviewMessage = "请先生成预览。"
            return false
        }
        guard let encoded = encodedDecisions(decisions) else {
            memoryReviewMessage = "无法编码待提交的裁决。"
            return false
        }
        memoryReviewLoading = true
        let raw = await runCore([
            "memory-review", "apply",
            "--decisions", encoded,
            "--confirm-digest", memoryReviewPreview.digest,
        ])
        memoryReviewLoading = false
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            memoryReviewMessage = "写入失败：无法解析结果。"
            return false
        }
        if let failure = object["error"] as? String {
            memoryReviewMessage = "写入失败：\(failure)"
            return false
        }
        let applied = object["applied_count"] as? Int ?? 0
        memoryReviewMessage = "已写入 \(applied) 条裁决，AI 召回立即按新结果执行。"
        memoryReviewPreview = MemoryReviewPreview()
        await refreshMemoryReview(force: true)
        return true
    }

    func refreshPrivacyPolicyStatus() async {
        if privacyPolicyLoading { return }
        privacyPolicyLoading = true
        let raw = await runCore(["privacy-policy", "status"])
        privacyPolicyStatus = PrivacyPolicyStatus(jsonText: raw)
        privacyPolicyLoading = false
    }

    func previewPrivacyPolicy(app: String, domain: String) async {
        if privacyPolicyLoading { return }
        privacyPolicyLoading = true
        var arguments = ["privacy-policy", "preview"]
        let trimmedApp = app.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedDomain = domain.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedApp.isEmpty {
            arguments += ["--app", trimmedApp]
        }
        if !trimmedDomain.isEmpty {
            arguments += ["--domain", trimmedDomain]
        }
        let raw = await runCore(arguments)
        privacyPolicyPreview = PrivacyPolicyPreview(jsonText: raw)
        privacyPolicyLoading = false
    }

    func configurePrivacyProfile(_ profile: String) async {
        guard ["strict", "personal_high_precision"].contains(profile) else { return }
        if privacyPolicyLoading { return }
        privacyPolicyLoading = true
        let previewRaw = await runCore(["privacy-policy", "profile-preview", "--profile", profile])
        guard confirmPrivacyProfileChange(profile, previewText: privacyProfilePreviewSummary(from: previewRaw, profile: profile)) else {
            markdownExportMessage = "隐私档位切换已取消。"
            privacyPolicyLoading = false
            return
        }
        let raw = await runCore(["configure-privacy-profile", "--profile", profile])
        markdownExportMessage = privacyProfileConfigureSummary(from: raw, profile: profile)
        privacyPolicyLoading = false
        await refreshPrivacyPolicyStatus()
        await refreshStatus()
    }

    func refreshMemoryGovernanceIfNeeded() async {
        guard !memoryGovernanceLoaded else { return }
        await refreshMemoryGovernance()
    }

    func refreshMemoryGovernance() async {
        if memoryGovernanceLoading { return }
        memoryGovernanceLoading = true
        memoryGovernanceMessage = "正在读取记忆治理和审批状态..."
        let ledgerRaw = await runCore(["memory-ledger", "status"])
        let auditRaw = await runCore(["memory-audit", "--format", "json", "--limit", "50"])
        let approvalStatusRaw = await runCore(["memory-approval", "status", "--limit", "50"])
        let approvalCandidatesRaw = await runCore(["memory-approval", "candidates", "--limit", "50"])
        let ledger = MemoryLedgerStatus(jsonText: ledgerRaw)
        let audit = MemoryAuditStatus(jsonText: auditRaw)
        memoryLedgerStatus = ledger
        memoryAuditStatus = audit
        memoryApprovalStatus = MemoryApprovalStatus(jsonText: approvalStatusRaw)
        memoryApprovalCandidates = MemoryApprovalCandidates(jsonText: approvalCandidatesRaw)
        lastMemoryGovernanceUpdatedAt = Date()
        memoryGovernanceLoaded = true
        memoryGovernanceMessage = memoryGovernanceSummary(ledger: effectiveMemoryLedgerStatus, audit: audit, candidates: memoryApprovalCandidates)
        memoryGovernanceLoading = false
    }

    func openMemoryLedger() {
        let ledger = effectiveMemoryLedgerStatus
        guard !ledger.ledgerPath.isEmpty else {
            memoryGovernanceMessage = "台账路径未返回；先重新读取记忆治理状态。"
            return
        }
        guard FileManager.default.fileExists(atPath: ledger.ledgerPath) else {
            memoryGovernanceMessage = "台账不存在：\(ledger.displayName)。未创建文件。"
            return
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: ledger.ledgerPath))
        memoryGovernanceMessage = "已打开台账：\(ledger.displayName)。"
    }

    func exportMemoryAuditMarkdown() async {
        if memoryGovernanceLoading { return }
        guard let url = chooseMemoryAuditExportURL() else {
            memoryGovernanceMessage = "导出已取消。"
            memoryAuditExportPath = ""
            return
        }
        memoryGovernanceLoading = true
        memoryGovernanceMessage = "正在导出只读 memory-audit 报告..."
        let raw = await runCore(["memory-audit", "--format", "markdown", "--limit", "50", "--out", url.path])
        let result = memoryAuditExportSummary(from: raw, fallback: url)
        memoryGovernanceMessage = result.message
        memoryAuditExportPath = result.path
        memoryGovernanceLoading = false
    }

    func openMemoryAuditExport() {
        guard !memoryAuditExportPath.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: memoryAuditExportPath))
    }

    func previewMemoryApproval(request: MemoryApprovalRequest, memoryText: String) async -> MemoryApprovalPreviewResult {
        var arguments = ["memory-approval", "preview", "--action", request.action.rawValue]
        switch request.action {
        case .delete:
            arguments += ["--ledger-id", request.ledgerID]
        case .rewrite:
            arguments += ["--ledger-id", request.ledgerID, "--replacement-text", memoryText]
        case .approveCandidate:
            arguments += ["--package", request.packageID, "--candidate-key", request.candidateKey, "--memory-text", memoryText]
        case .promoteReviewOnly:
            arguments += ["--package", request.packageID, "--candidate-key", request.candidateKey, "--memory-text", memoryText]
        }
        let raw = await runCore(arguments)
        let result = MemoryApprovalPreviewResult(jsonText: raw)
        memoryGovernanceMessage = result.displayMessage
        return result
    }

    func applyMemoryApproval(planID: String) async -> MemoryApprovalPreviewResult {
        let raw = await runCore(["memory-approval", "apply", "--plan-id", planID])
        let result = MemoryApprovalPreviewResult(jsonText: raw)
        memoryGovernanceMessage = result.displayMessage
        await refreshMemoryGovernance()
        return result
    }

    func refreshActivityConfirmationsIfNeeded(maxAge: TimeInterval = 30) async {
        if activityConfirmationLoaded,
           let lastActivityConfirmationUpdatedAt,
           Date().timeIntervalSince(lastActivityConfirmationUpdatedAt) < maxAge {
            return
        }
        if activityConfirmationLoaded && activityConfirmationUsesFastCards {
            await refreshActivityConfirmationsIfRevisionChanged()
            return
        }
        await refreshActivityConfirmations()
    }

    func refreshActivityConfirmations(allowDuringOperation: Bool = false) async {
        if activityConfirmationLoading || activityConfirmationLoadingMore { return }
        if !allowDuringOperation,
           (!activityConfirmationWorkingGroupKeys.isEmpty
               || !activityConfirmationClarifyingGroupKeys.isEmpty) {
            return
        }
        activityConfirmationLoading = true
        activityConfirmationRefreshGeneration &+= 1
        let refreshGeneration = activityConfirmationRefreshGeneration
        let mutationGeneration = activityConfirmationMutationGeneration
        defer {
            activityConfirmationLoading = false
            if activityConfirmationRefreshNeeded {
                activityConfirmationRefreshNeeded = false
                Task { await self.reloadActivityConfirmationsPreservingDepth() }
            }
        }
        let fastCardsArguments = [
            "cards",
            "--range", "7d",
            "--limit", "12",
            "--section", "pending_confirmation",
        ]
        var cardsRaw = await runConfirmationCardsCore(fastCardsArguments)
        var cardsParsed = await parseActivityConfirmationCards(cardsRaw)
        if !cardsParsed.page.isValid, cardsRaw.contains("unrecognized arguments") {
            // The deployed core predates --section; retry once without it.
            cardsRaw = await runConfirmationCardsCore([
                "cards", "--range", "7d", "--limit", "12",
            ])
            cardsParsed = await parseActivityConfirmationCards(cardsRaw)
        }
        if !cardsParsed.page.isValid,
           let coreError = CoreOutputErrorClassifier.classify(cardsRaw),
           coreError == .databaseBusy || coreError == .timedOut {
            // Transient contention: never rerun the same expensive query on
            // the 180-second slow path — keep whatever is on screen and let
            // the next poll retry naturally.
            setActivityConfirmationSnapshotMessage(
                "数据库正在维护中，已保留当前卡片；稍后会自动重试。",
                severity: .neutral
            )
            lastActivityConfirmationUpdatedAt = Date()
            return
        }
        if cardsParsed.page.isValid {
            guard refreshGeneration == activityConfirmationRefreshGeneration,
                  mutationGeneration == activityConfirmationMutationGeneration
            else {
                activityConfirmationRefreshNeeded = true
                return
            }
            applyActivityConfirmationCardsPage(cardsParsed.page, replacing: true)
            return
        }

        // v0.3.19 and earlier cores do not expose the fast card contract.
        // Fall back to the v4 list without weakening any response validation.
        let raw = await runCore([
            "activity-confirmation", "list",
            "--range", "7d",
            "--limit", "80",
            "--format", "json",
        ])
        let parsed = await Task.detached(priority: .userInitiated) {
            ActivityConfirmationParseBox(jsonText: raw)
        }.value
        let snapshot = parsed.snapshot
        guard refreshGeneration == activityConfirmationRefreshGeneration,
              mutationGeneration == activityConfirmationMutationGeneration
        else {
            activityConfirmationRefreshNeeded = true
            return
        }
        if activityConfirmationLoaded && snapshot.isBlocked {
            setActivityConfirmationSnapshotMessage(
                "待确认数据库暂时不可读；已保留上一次成功加载的卡片。",
                severity: .warning
            )
            return
        }
        activityConfirmationUsesFastCards = false
        activityConfirmationHasMore = false
        activityConfirmationRevision = ""
        activityConfirmationNextCursor = ""
        activityConfirmationSnapshot = snapshot
        activityConfirmationPendingGroups = parsed.pendingGroups
        if let coreMode = parsed.mode {
            activityConfirmationMode = coreMode
            UserDefaults.standard.set(coreMode.rawValue, forKey: "pam.activity-confirmation.mode")
        }
        LocalNotificationCoordinator.shared.update(snapshot: snapshot, pendingGroups: parsed.pendingGroups)
        lastActivityConfirmationUpdatedAt = Date()
        activityConfirmationLoaded = true
        let visibleGroupKeys = Set(parsed.pendingGroups.map(\.groupKey))
        activityConfirmationClarifications = activityConfirmationClarifications.filter {
            visibleGroupKeys.contains($0.key)
        }
        if snapshot.isBlocked {
            setActivityConfirmationSnapshotMessage(snapshot.summaryLine, severity: .warning)
        } else {
            clearActivityConfirmationSnapshotMessage()
        }
    }

    func loadMoreActivityConfirmationCards() async {
        guard activityConfirmationUsesFastCards,
              activityConfirmationHasMore,
              !activityConfirmationNextCursor.isEmpty,
              !activityConfirmationLoading,
              !activityConfirmationLoadingMore
        else { return }
        activityConfirmationLoadingMore = true
        let generationFence = ActivityConfirmationGenerationFence(
            refreshGeneration: activityConfirmationRefreshGeneration,
            mutationGeneration: activityConfirmationMutationGeneration
        )
        defer {
            activityConfirmationLoadingMore = false
            if activityConfirmationRefreshNeeded {
                activityConfirmationRefreshNeeded = false
                Task { await self.reloadActivityConfirmationsPreservingDepth() }
            }
        }
        let fastCardsArguments = [
            "cards",
            "--range", "7d",
            "--limit", "12",
            "--section", "pending_confirmation",
            "--cursor", activityConfirmationNextCursor,
        ]
        let raw = await runConfirmationCardsCore(fastCardsArguments)
        let parsed = await parseActivityConfirmationCards(raw)
        guard parsed.page.isValid else {
            let coreError = CoreOutputErrorClassifier.classify(raw)
            setActivityConfirmationMessage(
                coreError == .databaseBusy || coreError == .timedOut
                    ? "数据库正在维护中，已保留当前页面；稍后可再试。"
                    : "更多待确认卡片暂时无法读取；已保留当前页面。",
                severity: .warning
            )
            return
        }
        guard generationFence.allows(
            currentRefreshGeneration: activityConfirmationRefreshGeneration,
            currentMutationGeneration: activityConfirmationMutationGeneration
        ) else {
            activityConfirmationRefreshNeeded = true
            return
        }
        let mustReplace = parsed.page.cursorReset
            || parsed.page.revision != activityConfirmationRevision
        applyActivityConfirmationCardsPage(parsed.page, replacing: mustReplace)
        if mustReplace {
            setActivityConfirmationMessage(
                "待确认列表已变化，已从最新第一页重新开始。",
                severity: .neutral
            )
        }
    }

    /// Lazily pages the information_insufficient section into its collapsed
    /// panel.  These clues never share the answerable queue's page slots.
    func loadInsufficientActivityConfirmationCards(reset: Bool) async {
        guard activityConfirmationUsesFastCards,
              !activityConfirmationInsufficientLoading
        else { return }
        if !reset && !activityConfirmationInsufficientHasMore { return }
        activityConfirmationInsufficientLoading = true
        defer { activityConfirmationInsufficientLoading = false }
        var arguments = [
            "cards",
            "--range", "7d",
            "--limit", "12",
            "--section", "information_insufficient",
        ]
        if !reset, !activityConfirmationInsufficientNextCursor.isEmpty {
            arguments += ["--cursor", activityConfirmationInsufficientNextCursor]
        }
        let raw = await runConfirmationCardsCore(arguments)
        let parsed = await parseActivityConfirmationCards(raw)
        guard parsed.page.isValid else {
            let coreError = CoreOutputErrorClassifier.classify(raw)
            setActivityConfirmationMessage(
                coreError == .databaseBusy || coreError == .timedOut
                    ? "数据库正在维护中，信息不足线索稍后可再试。"
                    : "信息不足线索暂时无法读取。",
                severity: .warning
            )
            return
        }
        var combined = reset ? [] : activityConfirmationInsufficientGroups
        var existingKeys = Set(combined.map(\.groupKey))
        for group in parsed.page.groups where !existingKeys.contains(group.groupKey) {
            combined.append(group)
            existingKeys.insert(group.groupKey)
        }
        activityConfirmationInsufficientGroups = combined
        activityConfirmationInsufficientHasMore = parsed.page.hasMore
        activityConfirmationInsufficientNextCursor = parsed.page.nextCursor
    }

    func loadActivityConfirmationDiagnosticsIfNeeded(force: Bool = false) async {
        guard activityConfirmationUsesFastCards,
              !activityConfirmationDiagnosticsLoading
        else { return }
        if !force, activityConfirmationDiagnosticsSnapshot.status != "unknown" {
            return
        }
        activityConfirmationDiagnosticsLoading = true
        let raw = await runCore([
            "activity-confirmation", "list",
            "--range", "7d",
            "--limit", "80",
            "--format", "json",
        ])
        let parsed = await Task.detached(priority: .utility) {
            ActivityConfirmationParseBox(jsonText: raw)
        }.value
        activityConfirmationDiagnosticsSnapshot = parsed.snapshot
        activityConfirmationDiagnosticsLoading = false
    }

    private func refreshActivityConfirmationsIfRevisionChanged() async {
        guard activityConfirmationUsesFastCards,
              !activityConfirmationLoading,
              !activityConfirmationLoadingMore
        else { return }
        let fastRevisionArguments = [
            "revision",
            "--range", "7d",
        ]
        let raw = await runConfirmationCardsCore(fastRevisionArguments)
        let parsed = await parseActivityConfirmationRevision(raw)
        guard parsed.isValid else {
            // A failed (or busy) probe must never escalate into a full
            // refresh; skipping this poll keeps the visible pages intact.
            return
        }
        if parsed.revision == activityConfirmationRevision {
            lastActivityConfirmationUpdatedAt = Date()
            clearActivityConfirmationSnapshotMessage()
            return
        }
        await reloadActivityConfirmationsPreservingDepth()
    }

    /// Re-fetches the answerable queue up to the currently visible page depth
    /// and swaps it in atomically, so background revision changes and card
    /// answers no longer collapse the list back to page one.
    private func reloadActivityConfirmationsPreservingDepth() async {
        guard activityConfirmationUsesFastCards, activityConfirmationLoaded else {
            await refreshActivityConfirmations(allowDuringOperation: true)
            return
        }
        if activityConfirmationLoading || activityConfirmationLoadingMore {
            activityConfirmationRefreshNeeded = true
            return
        }
        activityConfirmationLoading = true
        activityConfirmationRefreshGeneration &+= 1
        let refreshGeneration = activityConfirmationRefreshGeneration
        let mutationGeneration = activityConfirmationMutationGeneration
        defer {
            activityConfirmationLoading = false
            if activityConfirmationRefreshNeeded {
                activityConfirmationRefreshNeeded = false
                Task { await self.reloadActivityConfirmationsPreservingDepth() }
            }
        }
        let depth = max(1, min(4, (activityConfirmationPendingGroups.count + 11) / 12))
        var pages: [ActivityConfirmationCardsPage] = []
        var cursor = ""
        for _ in 0..<depth {
            var arguments = [
                "cards",
                "--range", "7d",
                "--limit", "12",
                "--section", "pending_confirmation",
            ]
            if !cursor.isEmpty {
                arguments += ["--cursor", cursor]
            }
            let raw = await runConfirmationCardsCore(arguments)
            let parsed = await parseActivityConfirmationCards(raw)
            guard parsed.page.isValid else {
                let coreError = CoreOutputErrorClassifier.classify(raw)
                if coreError == .databaseBusy || coreError == .timedOut {
                    setActivityConfirmationSnapshotMessage(
                        "数据库正在维护中，已保留当前卡片；稍后会自动重试。",
                        severity: .neutral
                    )
                    lastActivityConfirmationUpdatedAt = Date()
                }
                return
            }
            if let first = pages.first, parsed.page.revision != first.revision {
                // The queue moved again mid-chain; keep the old state and let
                // the next poll pick up the newer revision.
                return
            }
            pages.append(parsed.page)
            cursor = parsed.page.nextCursor
            if cursor.isEmpty { break }
        }
        guard let firstPage = pages.first else { return }
        guard refreshGeneration == activityConfirmationRefreshGeneration,
              mutationGeneration == activityConfirmationMutationGeneration
        else {
            activityConfirmationRefreshNeeded = true
            return
        }
        applyActivityConfirmationCardsPage(firstPage, replacing: true)
        for page in pages.dropFirst() {
            applyActivityConfirmationCardsPage(page, replacing: false)
        }
    }

    private func parseActivityConfirmationCards(
        _ raw: String
    ) async -> ActivityConfirmationCardsParseBox {
        await Task.detached(priority: .userInitiated) {
            ActivityConfirmationCardsParseBox(jsonText: raw)
        }.value
    }

    private func parseActivityConfirmationRevision(
        _ raw: String
    ) async -> ActivityConfirmationRevisionSnapshot {
        await Task.detached(priority: .utility) {
            ActivityConfirmationRevisionParseBox(jsonText: raw)
        }.value.snapshot
    }

    private func applyActivityConfirmationCardsPage(
        _ page: ActivityConfirmationCardsPage,
        replacing: Bool
    ) {
        var combinedGroups = replacing ? [] : activityConfirmationPendingGroups
        var existingKeys = Set(combinedGroups.map(\.groupKey))
        for group in page.groups where !existingKeys.contains(group.groupKey) {
            combinedGroups.append(group)
            existingKeys.insert(group.groupKey)
        }
        var snapshot = page.snapshot
        snapshot.questionGroups = combinedGroups
        activityConfirmationSnapshot = snapshot
        activityConfirmationPendingGroups = combinedGroups
        activityConfirmationUsesFastCards = true
        activityConfirmationHasMore = page.hasMore
        activityConfirmationRevision = page.revision
        activityConfirmationNextCursor = page.nextCursor
        if replacing {
            activityConfirmationDiagnosticsSnapshot = ActivityConfirmationSnapshot()
            // The clue panel snapshot belongs to the previous revision; drop
            // it so the next expansion reloads fresh pages.
            activityConfirmationInsufficientGroups = []
            activityConfirmationInsufficientHasMore = false
            activityConfirmationInsufficientNextCursor = ""
        }
        LocalNotificationCoordinator.shared.update(snapshot: snapshot, pendingGroups: combinedGroups)
        lastActivityConfirmationUpdatedAt = Date()
        activityConfirmationLoaded = true
        activityConfirmationClarifications = activityConfirmationClarifications.filter {
            existingKeys.contains($0.key)
        }
        clearActivityConfirmationSnapshotMessage()
    }

    func configureActivityConfirmationMode(_ mode: ActivityConfirmationModePreset) async {
        guard !activityConfirmationConfigurationLoading else { return }
        let previousMode = activityConfirmationMode
        activityConfirmationConfigurationLoading = true
        activityConfirmationConfigurationMessage = "正在更新待确认召回强度…"
        let command = ActivityConfirmationCommandBuilder.configureCommand(mode: mode)
        let raw = await runCore(command.arguments)
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              ActivityConfirmationMutationResponseValidator.configureResponseIsSafe(
                  object,
                  expectedMode: mode
              )
        else {
            activityConfirmationMode = previousMode
            activityConfirmationConfigurationMessage = "召回强度更新失败或响应未满足安全契约；已保留原设置。"
            activityConfirmationConfigurationLoading = false
            return
        }
        activityConfirmationMode = mode
        UserDefaults.standard.set(mode.rawValue, forKey: "pam.activity-confirmation.mode")
        activityConfirmationConfigurationMessage = "已切换为\(mode.title)；Privacy Firewall 与事实确认门槛保持不变。"
        activityConfirmationConfigurationLoading = false
        activityConfirmationLoaded = false
        await refreshActivityConfirmations()
    }

    func addManualActivity(
        safeLabel: String,
        workstream: String,
        outcome: String,
        startedAt: Date,
        endedAt: Date
    ) async -> Bool {
        guard !manualActivityEntryLoading else { return false }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        guard let command = ActivityConfirmationCommandBuilder.manualAddCommand(
            safeLabel: safeLabel,
            workstream: workstream,
            outcome: outcome,
            startedAt: formatter.string(from: startedAt),
            endedAt: formatter.string(from: endedAt)
        ) else {
            manualActivityEntryMessage = "请完整填写活动摘要、工作流、结果和有效时间范围。"
            return false
        }
        manualActivityEntryLoading = true
        manualActivityEntryMessage = "正在本机补记…"
        let raw = await runCore(command.arguments, standardInput: command.standardInput)
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              ActivityConfirmationMutationResponseValidator.manualAddResponseIsSafe(
                  object,
                  expectedWorkstream: workstream,
                  expectedOutcome: outcome
              )
        else {
            manualActivityEntryMessage = "补记结果无法解析或未满足本机安全契约；未确认是否已记录。"
            manualActivityEntryLoading = false
            return false
        }
        manualActivityEntryMessage = "已补记为本机已确认事实。"
        manualActivityEntryLoading = false
        activityConfirmationLoaded = false
        await refreshActivityConfirmations()
        await refreshStatus()
        setActivityConfirmationMessage("已补记一项系统遗漏，并更新今天的已确认事实。", severity: .success)
        return true
    }

    func answerActivityConfirmation(
        group: ActivityConfirmationQuestionGroup,
        answer: ActivityConfirmationAnswer,
        reason: ActivityConfirmationReason?,
        note: String,
        workstream: String,
        outcome: String
    ) async {
        guard !activityConfirmationWorkingGroupKeys.contains(group.groupKey),
              !activityConfirmationClarifyingGroupKeys.contains(group.groupKey),
              let target = group.submissionTarget
        else { return }
        let request = ActivityConfirmationAnswerRequest(answer: answer, selectedReason: reason)
        guard request.isValid else {
            setActivityConfirmationMessage("请先选择没有发生的原因；系统不会替你推断。", severity: .warning)
            return
        }
        guard let command = ActivityConfirmationCommandBuilder.answerCommand(
            target: target,
            request: request,
            note: note,
            workstream: workstream,
            outcome: outcome,
            structuredDetailsSupported: group.contractVersion >= 4
        ) else {
            setActivityConfirmationMessage(
                "这张卡缺少安全提交所需的版本信息，已停止操作并等待刷新。",
                severity: .warning
            )
            return
        }
        activityConfirmationMutationGeneration &+= 1
        activityConfirmationWorkingGroupKeys.insert(group.groupKey)
        defer {
            activityConfirmationWorkingGroupKeys.remove(group.groupKey)
        }
        let raw = await runCore(command.arguments, standardInput: command.standardInput)
        if CoreOutputErrorClassifier.classify(raw) == .databaseBusy {
            setActivityConfirmationMessage(
                "数据库正在维护，这次回答没有提交；请稍后重试。",
                severity: .warning
            )
            return
        }
        let coreResultMessage = activityConfirmationAnswerSummary(
            from: raw,
            group: group,
            answer: answer,
            workstream: workstream,
            outcome: outcome
        )
        let resultIsWarning = ActivityConfirmationMessagePolicy.isWarning(coreResultMessage)
        let resultMessage = answer == .ignored && !resultIsWarning
            ? "已忽略该问题；不表示发生或未发生，也不会用于系统校准。"
            : coreResultMessage
        activityConfirmationClarifications.removeValue(forKey: group.groupKey)
        await reloadActivityConfirmationsPreservingDepth()
        let resultSeverity: ActivityConfirmationClarificationSeverity
        if resultIsWarning {
            resultSeverity = .warning
        } else if answer == .ignored {
            resultSeverity = .neutral
        } else {
            resultSeverity = .success
        }
        setActivityConfirmationMessage(resultMessage, severity: resultSeverity)
    }

    func clarifyActivityConfirmation(
        _ group: ActivityConfirmationQuestionGroup,
        note: String
    ) async {
        guard !group.isLegacy else {
            setActivityConfirmationMessage("当前 Core 不支持对这张兼容卡重新梳理。", severity: .warning)
            return
        }
        guard !activityConfirmationWorkingGroupKeys.contains(group.groupKey),
              !activityConfirmationClarifyingGroupKeys.contains(group.groupKey)
        else { return }
        guard let command = ActivityConfirmationCommandBuilder.clarifyCommand(
            groupKey: group.groupKey,
            memberRevision: group.memberRevision,
            note: note
        ) else {
            setActivityConfirmationMessage(
                "这张卡缺少安全重新梳理所需的版本信息，已停止操作并等待刷新。",
                severity: .warning
            )
            return
        }
        activityConfirmationMutationGeneration &+= 1
        activityConfirmationClarifyingGroupKeys.insert(group.groupKey)
        defer {
            activityConfirmationClarifyingGroupKeys.remove(group.groupKey)
        }
        let raw = await runCore(command.arguments, standardInput: command.standardInput)
        if CoreOutputErrorClassifier.classify(raw) == .databaseBusy {
            setActivityConfirmationMessage(
                "数据库正在维护，这次重新梳理没有执行；请稍后重试。",
                severity: .warning
            )
            return
        }
        let response = ActivityConfirmationClarifyResponse(jsonText: raw)
        if response.status == "clarified" {
            if let clarification = response.clarification {
                activityConfirmationClarifications[group.groupKey] = clarification
            }
            for clarifiedGroup in response.questionGroups {
                if let clarification = clarifiedGroup.clarification {
                    activityConfirmationClarifications[clarifiedGroup.groupKey] = clarification
                } else if clarifiedGroup.groupKey == response.groupKey,
                          let clarification = response.clarification {
                    activityConfirmationClarifications[clarifiedGroup.groupKey] = clarification
                }
            }
        }
        if response.status == "clarified" || response.status == "stale" || response.status == "blocked" {
            await reloadActivityConfirmationsPreservingDepth()
        }
        setActivityConfirmationMessage(response.displayMessage, severity: response.severity)
    }

    func revokeActivityConfirmationCalibration(_ calibration: ActivityConfirmationCalibration) async {
        guard calibration.canRevoke, !activityConfirmationLoading else { return }
        activityConfirmationLoading = true
        let raw = await runCore([
            "activity-confirmation", "revoke-calibration",
            "--calibration-key", calibration.calibrationKey,
        ])
        let resultMessage = activityConfirmationRevokeSummary(from: raw)
        activityConfirmationLoading = false
        activityConfirmationLoaded = false
        await refreshActivityConfirmations()
        setActivityConfirmationMessage(
            resultMessage,
            severity: ActivityConfirmationMessagePolicy.isWarning(resultMessage) ? .warning : .success
        )
    }

    func refreshDecisionQueueIfNeeded() async {
        guard !decisionQueueLoaded else { return }
        await refreshDecisionQueue()
    }

    func refreshDecisionQueue() async {
        if decisionQueueLoading { return }
        decisionQueueLoading = true
        decisionQueueMessage = "正在读取待决策队列..."
        let raw = await runCore(["decision-queue", "list", "--range", "today", "--limit", "80", "--format", "json"])
        let snapshot = DecisionQueueSnapshot(jsonText: raw)
        decisionQueueSnapshot = snapshot
        lastDecisionQueueUpdatedAt = Date()
        decisionQueueLoaded = true
        decisionQueueMessage = snapshot.summaryLine
        decisionQueueLoading = false
    }

    func recordDecisionQueueAction(item: DecisionQueueItem, action: String) async {
        guard ["context-only", "never-write"].contains(action) else { return }
        if decisionQueueLoading { return }
        decisionQueueLoading = true
        let raw = await runCore([
            "decision-queue", "record",
            "--item-key", item.itemKey,
            "--action", action,
            "--generalize", "similar",
        ])
        decisionQueueMessage = decisionQueueRecordSummary(from: raw)
        decisionQueueLoading = false
        await refreshDecisionQueue()
    }

    func previewDecisionQueueWrite(request: DecisionQueueWriteRequest, memoryText: String) async -> MemoryApprovalPreviewResult {
        var arguments = [
            "decision-queue", "preview-write",
            "--item-key", request.itemKey,
            "--memory-text", memoryText,
        ]
        if !request.packageID.isEmpty {
            arguments += ["--package", request.packageID]
        }
        let raw = await runCore(arguments)
        let result = MemoryApprovalPreviewResult(jsonText: raw)
        decisionQueueMessage = result.displayMessage
        return result
    }

    func applyDecisionQueuePlan(planID: String) async -> MemoryApprovalPreviewResult {
        let raw = await runCore(["decision-queue", "apply", "--plan-id", planID])
        let result = MemoryApprovalPreviewResult(jsonText: raw)
        decisionQueueMessage = result.displayMessage
        await refreshDecisionQueue()
        await refreshMemoryGovernance()
        return result
    }

    func search(query: String, from: String? = nil, to: String? = nil) {
        let requestID = nextSearchRequestID()
        guard let payload = try? JSONSerialization.data(
            withJSONObject: ["query": query],
            options: [.sortedKeys]
        ),
        payload.count <= 1_024,
        let standardInput = String(data: payload, encoding: .utf8)
        else {
            searchText = "搜索内容不满足本机安全输入契约；未执行搜索。"
            return
        }
        Task {
            let result = await runCore(
                rangeArguments(base: ["search", "--query-stdin"], from: from, to: to),
                standardInput: standardInput
            )
            guard requestID == searchRequestID else { return }
            searchText = result
        }
    }

    func clearSearchResults() {
        searchRequestID += 1
        searchText = ""
    }

    func refreshDocumentChangesStatus() async {
        if documentChangesLoading { return }
        documentChangesLoading = true
        defer { documentChangesLoading = false }
        let raw = await runCore(["document-changes", "status"])
        let parsed = documentChangesSnapshotFromJSON(raw)
        documentTrackingSnapshot = parsed
        if parsed.isBlocked {
            documentChangesMessage = parsed.limitations.first ?? "文档追踪接口未满足安全边界，已停止展示和操作。"
        } else if !parsed.isAvailable {
            documentChangesMessage = "文档追踪状态暂不可读，已停止展示和操作。"
        } else {
            documentChangesMessage = "文档追踪状态已刷新。"
        }
    }

    func refreshFileHistory(range: String) async {
        guard ["today", "yesterday", "7d", "30d", "90d"].contains(range) else {
            fileHistoryMessage = "时间范围不符合安全契约；未执行查询。"
            return
        }
        if fileHistoryLoading { return }
        fileHistoryLoading = true
        defer { fileHistoryLoading = false }
        let raw = await runCore([
            "document-changes", "history", "--range", range, "--limit", "1000",
        ])
        let parsed = documentChangesSnapshotFromJSON(raw)
        fileHistorySnapshot = parsed
        if parsed.isBlocked {
            fileHistoryMessage = parsed.limitations.first ?? "文件历史未通过安全契约。"
        } else if !parsed.isAvailable {
            fileHistoryMessage = "文件历史暂不可读；未把未知状态显示为 0。"
        } else if parsed.coverage.isComplete {
            fileHistoryMessage = "只读文件历史已刷新。"
        } else {
            fileHistoryMessage = "覆盖不完整；当前展示的是至少记录到的文件。"
        }
    }

    func configureDocumentChanges(enabled: Bool) async {
        await runDocumentChangesConfiguration(
            ["--enabled", enabled ? "on" : "off"],
            successMessage: enabled ? "文档修改追踪已启用。" : "文档修改追踪已停用。",
            verification: { $0.coverage.enabled == enabled }
        )
    }

    func addDocumentWatchRoot(reauthorize: Bool = false) async {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "授权追踪"
        panel.message = "选择一个允许 PAM 监听文件变化的目录。真实路径会保存在本机 0600 权限的运行配置中，但不会进入 SQLite、日报或导出。"
        guard panel.runModal() == .OK, let url = panel.url else {
            documentChangesMessage = "添加目录已取消。"
            return
        }
        let previousRootRefs = Set(documentTrackingSnapshot.roots.map(\.rootRef))
        await runDocumentChangesConfiguration(
            [reauthorize ? "--reauthorize-root" : "--add-root", url.path],
            successMessage: reauthorize
                ? "已撤销无法核验的旧授权，并只保留新选择的目录。"
                : "已添加一个授权目录；界面只保留 Core 返回的脱敏标签。",
            verification: { snapshot in
                !Set(snapshot.roots.map(\.rootRef)).subtracting(previousRootRefs).isEmpty
            }
        )
    }

    func reauthorizeDocumentWatchRoot() async {
        guard confirmDocumentBaselineChange(
            title: "重新授权文档追踪目录",
            message: "当前授权记录无法安全核验。继续后会撤销全部旧目录和对应基线，只授权你接下来明确选择的一个目录。"
        ) else {
            documentChangesMessage = "重新授权已取消。"
            return
        }
        await addDocumentWatchRoot(reauthorize: true)
    }

    func suspendDocumentWatchRoot(rootRef: String) async {
        guard !rootRef.isEmpty else { return }
        await runDocumentChangesConfiguration(
            ["--suspend-root", rootRef],
            successMessage: "已停止追踪，原记录和审核历史保留。其余目录会重新核对覆盖。",
            verification: { snapshot in !snapshot.roots.contains(where: { $0.rootRef == rootRef }) }
        )
    }

    func removeDocumentWatchRoot(rootRef: String, displayLabel: String) async {
        guard !rootRef.isEmpty else {
            documentChangesMessage = "目录标识为空，未执行移除。"
            return
        }
        guard confirmDocumentBaselineChange(
            title: "移除文档追踪目录",
            message: "将停止追踪“\(displayLabel)”并清除该目录的安全基线。若以后重新添加，第一次变化可能先显示为 baseline_gap，直到建立新的可比基线。"
        ) else {
            documentChangesMessage = "移除目录已取消。"
            return
        }
        await runDocumentChangesConfiguration(
            ["--remove-root", rootRef],
            successMessage: "已移除授权目录并刷新文档追踪状态。",
            verification: { snapshot in !snapshot.roots.contains(where: { $0.rootRef == rootRef }) }
        )
    }

    func resetDocumentChangesBaseline() async {
        guard confirmDocumentBaselineChange(
            title: "重建文档安全基线",
            message: "这会清除当前授权目录的结构指纹并重新建立基线。重建期间覆盖不完整，下一次变化可能先显示为 baseline_gap，不能据此判断没有其它修改。"
        ) else {
            documentChangesMessage = "重建安全基线已取消。"
            return
        }
        await runDocumentChangesConfiguration(
            ["--reset-baseline"],
            successMessage: "已请求重建安全基线；建立期间会明确显示覆盖不完整。",
            verification: { $0.isAvailable }
        )
    }

    private func confirmDocumentBaselineChange(title: String, message: String) -> Bool {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "确认")
        alert.addButton(withTitle: "取消")
        return alert.runModal() == .alertFirstButtonReturn
    }

    private func runDocumentChangesConfiguration(
        _ arguments: [String],
        successMessage: String,
        verification: (DocumentChangesSnapshot) -> Bool
    ) async {
        if documentChangesLoading { return }
        documentChangesLoading = true
        defer { documentChangesLoading = false }
        let raw = await runCore(["document-changes", "configure"] + arguments)
        guard documentChangesConfigurationSucceeded(raw) else {
            documentChangesMessage = "配置命令未返回可验证的成功状态；界面保留原状态，未声称配置已生效。"
            return
        }
        let verificationRaw = await runCore(["document-changes", "status"])
        let verifiedSnapshot = documentChangesSnapshotFromJSON(verificationRaw)
        guard verifiedSnapshot.isAvailable, verification(verifiedSnapshot) else {
            documentChangesMessage = verifiedSnapshot.isBlocked
                ? (verifiedSnapshot.limitations.first ?? "配置后的状态未通过安全契约校验；界面保留原状态。")
                : "配置后的 enabled/目录状态无法独立验证；界面保留原状态，未声称配置已生效。"
            return
        }
        documentTrackingSnapshot = verifiedSnapshot
        documentChangesMessage = successMessage
    }

    private func documentChangesConfigurationSucceeded(_ raw: String) -> Bool {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let status = object["status"] as? String
        else {
            return false
        }
        return ["ok", "configured", "updated"].contains(status)
    }

    func beginDailyReviewRequest() -> Int {
        let requestID = nextReviewRequestID()
        aiReviewRequestID += 1
        aiReviewLoading = false
        markdownExportMessage = ""
        lastMarkdownExportPath = ""
        dailyReviewText = "正在加载统计概览..."
        reviewDocumentChanges = DocumentChangesSnapshot()
        return requestID
    }

    @discardableResult
    func loadDailyReview(requestID: Int, from: String? = nil, to: String? = nil) async -> Bool {
        let raw = await runCore(rangeArguments(base: ["daily-review"], from: from, to: to))
        guard requestID == reviewRequestID else { return false }
        reviewDocumentChanges = documentChangesSnapshotFromJSON(
            raw, nestedKey: "document_changes", allowOwnerLocalObservation: true
        )
        dailyReviewText = overviewDisplayText(from: raw)
        return true
    }

    func refreshAIReview(from: String? = nil, to: String? = nil, label: String) async {
        let requestID = nextAIReviewRequestID()
        aiReviewLoading = true
        let raw = await runCore(
            rangeArguments(
                base: ["ai-handoff", "package", "--format", "json", "--max-items", "24"],
                from: from,
                to: to
            )
        )
        guard requestID == aiReviewRequestID else { return }
        aiReviewSnapshot = AIReviewSnapshot(jsonText: raw, fallbackLabel: label)
        aiReviewLoading = false
    }

    func refreshAIConnectorStatus() async {
        if aiConnectorLoading { return }
        aiConnectorLoading = true
        let raw = await runCore(["ai-connector", "status"])
        aiConnectorStatus = AIConnectorStatus(jsonText: raw)
        aiConnectorLoading = false
    }

    func startAIConnector() async {
        if aiConnectorLoading { return }
        aiConnectorLoading = true
        let raw = await runCore(["ai-connector", "start"])
        aiConnectorStatus = AIConnectorStatus(jsonText: raw)
        markdownExportMessage = aiConnectorStatus.message.isEmpty ? "AI 连接器启动命令已返回。" : aiConnectorStatus.message
        aiConnectorLoading = false
    }

    func stopAIConnector() async {
        if aiConnectorLoading { return }
        aiConnectorLoading = true
        let raw = await runCore(["ai-connector", "stop"])
        aiConnectorStatus = AIConnectorStatus(jsonText: raw)
        markdownExportMessage = aiConnectorStatus.message.isEmpty ? "AI 连接器停止命令已返回。" : aiConnectorStatus.message
        aiConnectorLoading = false
    }

    func copyAIConnectorStartCommand() {
        guard !aiConnectorStatus.startCommand.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(aiConnectorStatus.startCommand, forType: .string)
        markdownExportMessage = "已复制 AI 连接器启动命令。"
    }

    func copyAIConnectorBaseURL() {
        guard !aiConnectorStatus.baseURL.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(aiConnectorStatus.baseURL, forType: .string)
        markdownExportMessage = "已复制 AI 连接器本机地址。"
    }

    private func confirmPrivacyProfileChange(_ profile: String, previewText: String) -> Bool {
        let alert = NSAlert()
        alert.messageText = "切换隐私档位"
        alert.informativeText = previewText
        alert.alertStyle = .warning
        alert.addButton(withTitle: "确认切换")
        alert.addButton(withTitle: "取消")
        return alert.runModal() == .alertFirstButtonReturn
    }

    private func privacyProfilePreviewSummary(from raw: String, profile: String) -> String {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return profile == "strict"
                ? "将切换到严格模式。预览无法解析；此操作仍只使用内置 profile，不新增任意 app/domain 规则。"
                : "将切换到个人高精度模式。预览无法解析；此操作仍只使用内置 profile，不新增任意 app/domain 规则。"
        }
        let summary = object["summary"] as? String ?? "将切换内置 privacy profile。"
        let counts = object["rule_counts"] as? [String: Any] ?? [:]
        let hard = counts["hard_block_apps"] as? Int ?? 0
        let comm = counts["communication_apps"] as? Int ?? 0
        let domains = counts["high_risk_domain_hints"] as? Int ?? 0
        let boundaries = object["unchanged_boundaries"] as? [String] ?? []
        let boundaryText = boundaries.prefix(4).joined(separator: "、")
        return """
        \(summary)

        预览计数：硬保护 App \(hard)，沟通 App \(comm)，高风险域名提示 \(domains)。
        不变边界：\(boundaryText.isEmpty ? "密码、验证码、支付、医疗等硬保护不变。" : boundaryText)。
        本操作只切换内置 profile；不新增、不删除任意 app/domain 规则。
        """
    }

    private func privacyProfileConfigureSummary(from raw: String, profile: String) -> String {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return "隐私档位命令已返回，但无法解析；请刷新隐私规则状态。"
        }
        let status = object["status"] as? String ?? "unknown"
        if status == "ok" {
            return "隐私档位已切换为 \(profile)。"
        }
        return "隐私档位切换未成功：\(status)。"
    }

    func exportTimelineMarkdown(from: String? = nil, to: String? = nil, label: String, kind: MarkdownExportKind) async {
        guard let url = chooseMarkdownExportURL(label: label, kind: kind) else {
            markdownExportMessage = "导出已取消。"
            lastMarkdownExportPath = ""
            return
        }
        let raw = await runCore(rangeArguments(base: ["timeline", "--format", kind.coreFormat, "--out", url.path], from: from, to: to))
        let result = markdownExportSummary(from: raw, kind: kind)
        markdownExportMessage = result.message
        lastMarkdownExportPath = result.path
    }

    func exportQualityMarkdown(from: String? = nil, to: String? = nil, label: String) async {
        guard let url = chooseMarkdownExportURL(label: label, kind: .quality) else {
            markdownExportMessage = "导出已取消。"
            lastMarkdownExportPath = ""
            return
        }
        let raw = await runCore(rangeArguments(base: ["quality-report", "--format", "markdown", "--out", url.path], from: from, to: to))
        let result = qualityExportSummary(from: raw)
        markdownExportMessage = result.message
        lastMarkdownExportPath = result.path
    }

    func exportAIHandoff(from: String? = nil, to: String? = nil, label: String) async {
        guard let url = chooseAIHandoffExportURL(label: label) else {
            markdownExportMessage = "导出已取消。"
            lastMarkdownExportPath = ""
            return
        }
        let raw = await runCore(
            rangeArguments(
                base: ["ai-handoff", "export", "--format", "markdown", "--out", url.path],
                from: from,
                to: to
            )
        )
        let result = aiHandoffExportSummary(from: raw, label: label)
        markdownExportMessage = result.message
        lastMarkdownExportPath = result.path
        if !result.summary.isEmpty {
            aiHandoffLastSummary = result.summary
        }
    }

    func exportFullMirror(from: String? = nil, to: String? = nil, label: String) async {
        guard let url = chooseFullMirrorExportURL(label: label) else {
            markdownExportMessage = "导出已取消。"
            lastMarkdownExportPath = ""
            return
        }
        let raw = await runCore(
            rangeArguments(
                base: ["package", "--no-markdown", "--out", Self.haPackageCacheDirectory().path],
                from: from,
                to: to
            )
        )
        let result = fullMirrorExportSummary(from: raw, destination: url)
        markdownExportMessage = result.message
        lastMarkdownExportPath = result.path
    }

    func openLastMarkdownExport() {
        guard !lastMarkdownExportPath.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: lastMarkdownExportPath))
    }

    func revealLastMarkdownExport() {
        guard !lastMarkdownExportPath.isEmpty else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: lastMarkdownExportPath)])
    }

    func deleteRange(from: String, to: String, reason: String) async {
        guard !from.isEmpty, !to.isEmpty else {
            deleteText = "请先选择要删除的时间段。"
            return
        }
        guard await authenticateForDeletion() else {
            deleteText = "删除已取消，或本机确认未通过。"
            return
        }
        let raw = await runCore(["delete-range", "--from", from, "--to", to, "--reason", reason])
        deleteText = deletionSummary(from: raw)
        await refreshStatus()
    }

    func openScreenRecordingSettings() {
        openURL("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")
    }

    func openDataFolder() {
        NSWorkspace.shared.open(Self.coreDirectory().appendingPathComponent("data", isDirectory: true))
    }

    private func openURL(_ raw: String) {
        guard let url = URL(string: raw) else { return }
        NSWorkspace.shared.open(url)
    }

    func runCore(_ arguments: [String], standardInput: String? = nil) async -> String {
        let pythonURL = Self.pythonExecutableURL()
        let coreURL = Self.coreDirectory()
        return await Task.detached {
            Self.runCoreBlocking(
                arguments,
                standardInput: standardInput,
                pythonURL: pythonURL,
                coreURL: coreURL
            )
        }.value
    }

    private func runConfirmationCardsCore(_ arguments: [String]) async -> String {
        let pythonURL = Self.pythonExecutableURL()
        let coreURL = Self.coreDirectory()
        let databasePath = coreURL
            .appendingPathComponent("data", isDirectory: true)
            .appendingPathComponent("activity.sqlite")
            .path
        return await Task.detached {
            Self.runCoreBlocking(
                ["--db", databasePath] + arguments,
                standardInput: nil,
                pythonURL: pythonURL,
                coreURL: coreURL,
                module: "activity_mvp.confirmation_cards_cli",
                timeoutOverride: 8
            )
        }.value
    }

    nonisolated private static func runCoreBlocking(
        _ arguments: [String],
        standardInput: String?,
        pythonURL: URL,
        coreURL: URL,
        module: String = "activity_mvp",
        timeoutOverride: TimeInterval? = nil
    ) -> String {
        let process = Process()
        process.executableURL = pythonURL
        process.arguments = ["-m", module] + arguments
        process.currentDirectoryURL = coreURL
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = coreURL.path
        environment["PERSONAL_AI_MONITOR_PYTHON"] = pythonURL.path
        process.environment = environment

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        let inputPipe: Pipe?
        if standardInput != nil {
            let pipe = Pipe()
            process.standardInput = pipe
            inputPipe = pipe
        } else {
            inputPipe = nil
        }
        let outputBuffer = CoreOutputBuffer()
        let reader = pipe.fileHandleForReading
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in
            finished.signal()
        }

        do {
            try process.run()
            if let inputPipe {
                inputPipe.fileHandleForWriting.write(
                    standardInput?.data(using: .utf8) ?? Data()
                )
                try? inputPipe.fileHandleForWriting.close()
            }
            let readerGroup = DispatchGroup()
            readerGroup.enter()
            DispatchQueue.global(qos: .utility).async {
                outputBuffer.append(reader.readDataToEndOfFile())
                readerGroup.leave()
            }

            let timeoutSeconds = timeoutOverride ?? Self.coreCommandTimeoutSeconds(for: arguments)
            let timedOut = finished.wait(timeout: .now() + timeoutSeconds) == .timedOut
            if timedOut {
                process.terminate()
                if finished.wait(timeout: .now() + 2) == .timedOut, process.isRunning {
                    kill(process.processIdentifier, SIGKILL)
                    _ = finished.wait(timeout: .now() + 2)
                }
            }
            // A grandchild process inheriting the pipe's write end could keep
            // readDataToEndOfFile blocked forever; a bounded wait keeps this
            // path from becoming an unrecoverable hang while the buffer still
            // holds whatever was read so far.
            _ = readerGroup.wait(timeout: .now() + 5)
            let data = outputBuffer.snapshot()
            let output = String(data: data, encoding: .utf8) ?? ""
            if timedOut {
                return "personal-ai-monitor-core timed out after \(Int(timeoutSeconds))s\n\(output)"
            }
            if process.terminationStatus == 0 {
                return output
            }
            return "personal-ai-monitor-core exited with \(process.terminationStatus)\n\(output)"
        } catch {
            try? inputPipe?.fileHandleForWriting.close()
            return "Could not run personal-ai-monitor-core: \(error.localizedDescription)"
        }
    }

    nonisolated private static func coreCommandTimeoutSeconds(for arguments: [String]) -> TimeInterval {
        guard let command = arguments.first else { return 180 }
        switch command {
        case "attention-center":
            // This is a read budget, never a deadline for an owner's choice.
            // Full retained history can take longer on a busy disk. Writes
            // keep their existing timeout and unknown-result safeguards.
            let action = arguments.dropFirst().first
            return action == "snapshot" || action == "detail" ? 60 : 20
        case "memory-review":
            return 20
        case "status":
            // Full status verifies today's file attribution under a shared
            // authorization lease. Real histories can exceed the quick-read
            // budget; aborting at 12s caused an endless unknown/retry cycle.
            return arguments.contains("--quick") ? 12 : 45
        case "ha-automation-status":
            return 20
        // Interactive card answers hold at most a 5s write lease plus a 30s
        // SQLite busy window; 45s covers the worst case without leaving the
        // user staring at a 3-minute silent hang.
        case "activity-confirmation":
            return 45
        case "backup-hygiene":
            return 60
        case "ai-handoff", "ai-connector", "memory-audit", "memory-ledger", "memory-approval", "decision-queue", "privacy-policy", "diagnostics", "document-changes":
            return 90
        default:
            return 180
        }
    }

    private func backupHygieneSummary(from raw: String) -> String {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return "只读卫生检查已返回，但无法解析为 JSON；未执行删除。"
        }
        let runtimePermissions = (object["runtime_permission_summary"] as? [String: Any])?["candidate_count"] as? Int ?? 0
        let backups = (object["risk_summary"] as? [String: Any])?["candidate_count"] as? Int ?? 0
        let haResidue = (object["ha_cache_residue_summary"] as? [String: Any])?["candidate_count"] as? Int ?? 0
        let exportCache = (object["activity_export_cache_summary"] as? [String: Any])?["candidate_count"] as? Int ?? 0
        let automationPlan = object["automation_plan"] as? [String: Any]
        let backupRotation = automationPlan?["backup_rotation"] as? [String: Any]
        let actions = backupRotation?["actions"] as? [[String: Any]] ?? []
        let strictHealthOK = actions.compactMap { $0["strict_cleanup_health_ok"] as? Bool }.first
        let strictText: String
        if let strictHealthOK {
            strictText = strictHealthOK ? "strict health 正常" : "strict health 阻断"
        } else {
            strictText = "strict health 未返回"
        }
        return "只读卫生检查完成：runtime 权限候选 \(runtimePermissions)，备份候选 \(backups)，HA cache 残留 \(haResidue)，导出缓存 \(exportCache)；\(strictText)。未执行删除。"
    }

    private func memoryGovernanceSummary(ledger: MemoryLedgerStatus, audit: MemoryAuditStatus, candidates: MemoryApprovalCandidates) -> String {
        if ledger.status == "error" || audit.status == "error" {
            return "记忆治理状态读取异常；未执行删除、改写或同步。"
        }
        if memoryRawAccessEnabled || memoryDestructiveActionsEnabled {
            return "记忆治理状态返回了异常边界；raw 或破坏性动作标记不是 false。未执行任何写入。"
        }
        let auditText = audit.itemCount == 0 ? "审计未发现可疑项" : "审计发现 \(audit.itemCount) 条可疑项"
        let candidateText = candidates.status == "ok" ? "候选 eligible \(candidates.eligibleCandidateCount)、review_only \(candidates.reviewOnlyCandidateCount)" : "候选状态 \(candidates.statusLabel)"
        return "记忆治理状态已读取：台账 \(ledger.currentMemoryCount) 条，待处理 \(ledger.pendingActionCount) 条，人工查找 \(ledger.needsManualLookupCount) 条；\(candidateText)；\(auditText)。"
    }

    private func decisionQueueRecordSummary(from raw: String) -> String {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return "待决策处理结果无法解析；未确认是否已记录。"
        }
        let status = object["status"] as? String ?? "unknown"
        let message = object["message"] as? String ?? ""
        if status == "recorded" {
            return message.isEmpty ? "已记录你的处理方式。" : message
        }
        return message.isEmpty ? "待决策处理未完成：\(status)。" : message
    }

    private func activityConfirmationAnswerSummary(
        from raw: String,
        group: ActivityConfirmationQuestionGroup,
        answer: ActivityConfirmationAnswer,
        workstream: String,
        outcome: String
    ) -> String {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              ActivityConfirmationSafetyBoundary.permits(
                  rawAccess: object["raw_access"],
                  destructiveActions: object["destructive_actions"],
                  memoryWriteActions: object["memory_write_actions"]
              )
        else {
            return "待确认回答无法解析或未满足安全契约；未确认是否已记录。"
        }
        if group.contractVersion >= 4 {
            switch ActivityConfirmationMutationResponseValidator.v4GroupAnswerResult(
                object,
                expectedGroupKey: group.groupKey,
                expectedAnswer: answer,
                expectedWorkstream: workstream,
                expectedOutcome: outcome
            ) {
            case .recorded:
                let message = MemoryApprovalMemory.safeText(object["message"], fallback: "", maxLength: 320)
                return message.isEmpty ? "已记录你的回答，并重新整理待确认内容。" : message
            case .stale:
                return "这组线索在你回答前发生了变化，未提交任何部分结果；已刷新为最新问题。"
            case .unsafe:
                return "Core 返回的组、回答或业务字段与本次提交不一致；未把它当成成功记录。"
            }
        }
        let status = object["status"] as? String ?? "unknown"
        let message = MemoryApprovalMemory.safeText(object["message"], fallback: "", maxLength: 320)
        if ["recorded", "answered", "resolved", "deferred", "ok"].contains(status) {
            return message.isEmpty ? "已记录你的回答，并重新整理待确认内容。" : message
        }
        if status == "stale" {
            return "这组线索在你回答前发生了变化，未提交任何部分结果；已刷新为最新问题。"
        }
        return message.isEmpty ? "待确认回答未完成：\(status)。" : message
    }

    private func activityConfirmationRevokeSummary(from raw: String) -> String {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              ActivityConfirmationSafetyBoundary.permits(
                  rawAccess: object["raw_access"],
                  destructiveActions: object["destructive_actions"],
                  memoryWriteActions: object["memory_write_actions"]
              )
        else {
            return "校准撤销结果无法解析或未满足安全契约；未确认是否已撤销。"
        }
        let status = object["status"] as? String ?? "unknown"
        let message = MemoryApprovalMemory.safeText(object["message"], fallback: "", maxLength: 320)
        if ["revoked", "ok"].contains(status) {
            return message.isEmpty ? "已撤销这条校准，并重新读取最近校准。" : message
        }
        return message.isEmpty ? "校准撤销未完成：\(status)。" : message
    }

    private func memoryAuditExportSummary(from raw: String, fallback: URL) -> (message: String, path: String) {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return ("memory-audit 报告导出返回无法解析；未执行删除或改写。", "")
        }
        let path = object["markdown_path"] as? String ?? fallback.path
        let status = object["status"] as? String ?? "unknown"
        let rawAccess = object["raw_access"] as? Bool ?? false
        guard !rawAccess else {
            return ("memory-audit 报告导出异常：raw_access 不是 false。", "")
        }
        let fileName = URL(fileURLWithPath: path).lastPathComponent
        return ("已导出只读 memory-audit 报告：\(Self.qualityStatusLabel(status))，\(fileName)。", path)
    }

    private static func pythonExecutableURL() -> URL {
        if let configured = ProcessInfo.processInfo.environment["PERSONAL_AI_MONITOR_PYTHON"], !configured.isEmpty {
            return URL(fileURLWithPath: configured)
        }
        if let launchAgentPython = launchAgentPythonExecutableURL() {
            return launchAgentPython
        }
        let frameworkPython = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
        if FileManager.default.isExecutableFile(atPath: frameworkPython) {
            return URL(fileURLWithPath: frameworkPython)
        }
        return URL(fileURLWithPath: "/usr/bin/python3")
    }

    private static func launchAgentPythonExecutableURL() -> URL? {
        let plistURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("LaunchAgents", isDirectory: true)
            .appendingPathComponent("app.personalai.monitor.core.plist")
        guard let data = try? Data(contentsOf: plistURL),
              let plist = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String: Any],
              let arguments = plist["ProgramArguments"] as? [String],
              let pythonPath = pythonPathFromLaunchAgentArguments(arguments),
              FileManager.default.isExecutableFile(atPath: pythonPath)
        else {
            return nil
        }
        return URL(fileURLWithPath: pythonPath)
    }

    private static func pythonPathFromLaunchAgentArguments(_ arguments: [String]) -> String? {
        if let direct = arguments.first {
            let executableName = URL(fileURLWithPath: direct).lastPathComponent.lowercased()
            if executableName.contains("python") {
                return direct
            }
        }
        guard let legacyCommand = arguments.last else { return nil }
        return pythonPathFromLaunchAgentCommand(legacyCommand)
    }

    private static func pythonPathFromLaunchAgentCommand(_ command: String) -> String? {
        guard let execRange = command.range(of: "exec ") else { return nil }
        var remainder = command[execRange.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
        guard !remainder.isEmpty else { return nil }
        if remainder.first == "'" {
            remainder.removeFirst()
            guard let endQuote = remainder.firstIndex(of: "'") else { return nil }
            return String(remainder[..<endQuote])
        }
        return remainder.split(separator: " ", maxSplits: 1, omittingEmptySubsequences: true).first.map(String.init)
    }

    private func rangeArguments(base: [String], from: String?, to: String?) -> [String] {
        var arguments = base
        if let from, !from.isEmpty {
            arguments += ["--from", from]
        }
        if let to, !to.isEmpty {
            arguments += ["--to", to]
        }
        return arguments
    }

    private func nextReviewRequestID() -> Int {
        reviewRequestID += 1
        return reviewRequestID
    }

    private func nextSearchRequestID() -> Int {
        searchRequestID += 1
        return searchRequestID
    }

    private func nextAIReviewRequestID() -> Int {
        aiReviewRequestID += 1
        return aiReviewRequestID
    }

    private func documentChangesSnapshotFromJSON(
        _ raw: String, nestedKey: String? = nil, allowOwnerLocalObservation: Bool = false
    ) -> DocumentChangesSnapshot {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return DocumentChangesSnapshot()
        }
        if let nestedKey {
            return documentChangesSnapshot(from: object[nestedKey], allowOwnerLocalObservation: allowOwnerLocalObservation)
        }
        return DocumentChangesSnapshot(dictionary: object)
    }

    private func documentChangesSnapshot(
        from value: Any?, allowOwnerLocalObservation: Bool = false
    ) -> DocumentChangesSnapshot {
        guard let dictionary = value as? [String: Any] else {
            return DocumentChangesSnapshot()
        }
        return DocumentChangesSnapshot(dictionary: dictionary, allowOwnerLocalObservation: allowOwnerLocalObservation)
    }

    private func overviewDisplayText(from raw: String) -> String {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return raw
        }
        let timeRange = object["time_range"] as? [String: Any] ?? [:]
        let coverage = object["coverage"] as? [String: Any] ?? [:]
        let fromLabel = Self.localTimestamp(timeRange["from"] as? String)
        let toLabel = Self.localTimestamp(timeRange["to"] as? String)
        let apps = (object["top_apps"] as? [String] ?? []).prefix(5).joined(separator: "、")
        let domains = (object["top_domains"] as? [String] ?? []).prefix(5).joined(separator: "、")
        let highlights = (object["highlights"] as? [String] ?? []).prefix(6)
        var lines = [
            "# 统计概览",
            "",
            "时间：\(fromLabel) 到 \(toLabel)",
            "",
            "- 可回顾活动：\(object["activity_count"] as? Int ?? 0) 条",
            "- 主要 App：\(apps.isEmpty ? "无" : apps)",
            "- 主要网站：\(domains.isEmpty ? "无" : domains)",
            "- 隐私保护事件：\(object["privacy_event_count"] as? Int ?? 0) 条",
            "- 未记录缺口：\(object["gap_event_count"] as? Int ?? 0) 条",
            "- 覆盖小时：\(coverage["recorded_hours"] as? Int ?? 0)",
            "- 丢弃帧：\(coverage["frames_dropped"] as? Int ?? 0)",
            "",
            "安全边界：source=local_clear_activity_only，raw_access=false，cloud=off",
        ]
        if !highlights.isEmpty {
            lines += ["", "## 片段预览", ""]
            for item in highlights {
                lines.append("- \(item)")
            }
        }
        return lines.joined(separator: "\n")
    }

    private func chooseMarkdownExportURL(label: String, kind: MarkdownExportKind) -> URL? {
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.allowedContentTypes = [UTType(filenameExtension: "md") ?? .plainText]
        panel.nameFieldStringValue = defaultMarkdownFileName(label: label, kind: kind)
        return panel.runModal() == .OK ? panel.url : nil
    }

    private func chooseMemoryAuditExportURL() -> URL? {
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.allowedContentTypes = [UTType(filenameExtension: "md") ?? .plainText]
        panel.nameFieldStringValue = defaultMemoryAuditFileName()
        return panel.runModal() == .OK ? panel.url : nil
    }

    private func chooseAIHandoffExportURL(label: String) -> URL? {
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.allowedContentTypes = [UTType(filenameExtension: "md") ?? .plainText]
        panel.nameFieldStringValue = defaultAIHandoffFileName(label: label)
        return panel.runModal() == .OK ? panel.url : nil
    }

    private func chooseFullMirrorExportURL(label: String) -> URL? {
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.allowedContentTypes = [.json]
        panel.nameFieldStringValue = defaultFullMirrorFileName(label: label)
        return panel.runModal() == .OK ? panel.url : nil
    }

    private func defaultMarkdownFileName(label: String, kind: MarkdownExportKind) -> String {
        let cleaned = label
            .replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: ":", with: "-")
            .replacingOccurrences(of: " ", with: "-")
        return "Personal-AI-Monitor-\(cleaned)-\(kind.fileSuffix).md"
    }

    private func defaultMemoryAuditFileName() -> String {
        let stamp = Self.fileTimestampFormatter.string(from: Date())
        return "Personal-AI-Monitor-\(stamp)-memory-audit.md"
    }

    private func defaultAIHandoffFileName(label: String) -> String {
        let cleaned = label
            .replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: ":", with: "-")
            .replacingOccurrences(of: " ", with: "-")
        return "Personal-AI-Monitor-\(cleaned)-ai-handoff.md"
    }

    private func defaultFullMirrorFileName(label: String) -> String {
        let cleaned = label
            .replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: ":", with: "-")
            .replacingOccurrences(of: " ", with: "-")
        return "Personal-AI-Monitor-\(cleaned)-ha-source-package.json"
    }

    private func markdownExportSummary(from raw: String, kind: MarkdownExportKind) -> (message: String, path: String) {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return (raw, "")
        }
        let path = object["markdown_path"] as? String ?? ""
        let activityCount = object["activity_count"] as? Int ?? 0
        let privacyCount = object["privacy_event_count"] as? Int ?? 0
        let gapCount = object["gap_event_count"] as? Int ?? 0
        let totalCount = object["total_event_count"] as? Int ?? activityCount
        guard !path.isEmpty else { return (raw, "") }
        if kind == .learning {
            let learning = object["learning_digest"] as? [String: Any] ?? [:]
            let useful = learning["useful_count"] as? Int ?? 0
            let lowSignal = learning["low_signal_count"] as? Int ?? 0
            let protected = learning["protected_count"] as? Int ?? privacyCount
            let duplicate = learning["duplicate_count"] as? Int ?? 0
            let gap = learning["gap_count"] as? Int ?? gapCount
            return ("已导出学习摘要：\(useful) 条可学习、\(lowSignal) 条低信号、\(protected) 条保护、\(duplicate) 条重复、\(gap) 条缺口。\(path)", path)
        }
        return ("已导出 \(totalCount) 条：\(activityCount) 条活动、\(privacyCount) 条保护事件、\(gapCount) 条缺口。\(path)", path)
    }

    private func qualityExportSummary(from raw: String) -> (message: String, path: String) {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return (raw, "")
        }
        let path = object["markdown_path"] as? String ?? ""
        let status = object["status"] as? String ?? "unknown"
        guard !path.isEmpty else { return (raw, "") }
        return ("已导出质量报告：\(Self.qualityStatusLabel(status))。\(path)", path)
    }

    private func aiHandoffExportSummary(from raw: String, label: String) -> (message: String, path: String, summary: String) {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return (raw, "", "AI 接手包返回无法解析；未显示任何活动正文。")
        }
        let status = object["status"] as? String ?? "unknown"
        if status != "ok" {
            let error = object["error"] as? String ?? "隐私扫描或生成流程阻断"
            return ("AI 接手包生成失败：\(error)", "", "最近生成失败：\(error)")
        }
        let path = object["markdown_path"] as? String ?? object["path"] as? String ?? ""
        let activityCount = object["activity_count"] as? Int ?? 0
        let readableCount = object["readable_activity_count"] as? Int ?? 0
        let projectCount = object["project_count"] as? Int ?? 0
        let scan = object["privacy_scan"] as? [String: Any] ?? [:]
        let scanStatus = scan["status"] as? String ?? "ok"
        let privacyText = scanStatus == "ok" ? "隐私扫描通过" : "隐私扫描阻断"
        let summary = "最近生成 \(Self.shortTimeFormatter.string(from: Date()))：\(label)，活动 \(activityCount) 条，可读 \(readableCount) 条，项目 \(projectCount) 个，raw_access=false，\(privacyText)。"
        guard !path.isEmpty else {
            return ("AI 接手包已生成，但没有返回文件路径。", "", summary)
        }
        return ("已生成 AI 接手包：活动 \(activityCount) 条，可读 \(readableCount) 条，项目 \(projectCount) 个，\(privacyText)。\(path)", path, summary)
    }

    private static func qualityStatusLabel(_ status: String) -> String {
        switch status {
        case "ok": return "正常"
        case "watch": return "观察"
        case "needs_review": return "需复核"
        default: return status.isEmpty ? "未知" : status
        }
    }

    private func fullMirrorExportSummary(from raw: String, destination: URL) -> (message: String, path: String) {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return (raw, "")
        }
        let sourcePath = object["json_path"] as? String ?? ""
        let packageID = object["package_id"] as? String ?? "unknown"
        let activityCount = object["activity_count"] as? Int ?? 0
        guard !sourcePath.isEmpty else { return (raw, "") }
        do {
            let fileManager = FileManager.default
            if fileManager.fileExists(atPath: destination.path) {
                try fileManager.removeItem(at: destination)
            }
            try fileManager.copyItem(at: Self.packageSourceURL(from: sourcePath), to: destination)
            return ("已下载 HA 安全包：\(activityCount) 条活动，包 ID \(packageID)。\(destination.path)", destination.path)
        } catch {
            return ("HA 安全包已生成，但复制到所选位置失败：\(error.localizedDescription)。源文件：\(sourcePath)", sourcePath)
        }
    }

    private static let isoFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = .current
        return formatter
    }()

    private static let statusObservationFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return formatter
    }()

    private static let fractionalStatusObservationFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return formatter
    }()

    private static func statusObservationDate(from value: String) -> Date? {
        statusObservationFormatter.date(from: value)
            ?? fractionalStatusObservationFormatter.date(from: value)
    }

    private static let localDisplayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }()

    private static let shortTimeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = .current
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()

    private static let fileTimestampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter
    }()

    private static func localTimestamp(_ value: String?) -> String {
        guard let value, !value.isEmpty else { return "-" }
        return LocalStatusTime.label(value)
    }

    private func deletionSummary(from raw: String) -> String {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return raw
        }
        let deleted = object["activities_deleted"] as? Int ?? object["deleted_activity_count"] as? Int ?? 0
        let packages = object["packages_revoked"] as? [Any] ?? []
        let cascade = object["cascade"] as? [String: Any] ?? [:]
        let searchIndex = cascade["search_index"] as? Int ?? 0
        let summaries = cascade["segments_touched"] as? Int ?? 0
        let coverage = cascade["coverage_ledger"] as? Int ?? 0
        let provenance = cascade["activity_provenance"] as? Int ?? 0
        let cacheFiles = cascade["ha_package_cache_files"] as? Int ?? 0
        return """
        已删除 \(deleted) 条本地活动。
        已撤销 \(packages.count) 个重叠 HA 包，并删除 \(cacheFiles) 个 HA 包缓存文件。
        已清理搜索索引 \(searchIndex) 条、摘要/时间段 \(summaries) 处、CoverageLedger \(coverage) 条、索引引用 \(provenance) 条。
        """
    }

    private func authenticateForDeletion() async -> Bool {
        await withCheckedContinuation { continuation in
            let context = LAContext()
            var error: NSError?
            let reason = "确认删除本机 AGI 活动数据。"
            guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
                continuation.resume(returning: false)
                return
            }
            context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, _ in
                continuation.resume(returning: success)
            }
        }
    }

    private func snapshotFromJSON(_ text: String) -> StatusSnapshot? {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return failClosedStatusSnapshot(
                reasonCode: "invalid_full_status_json",
                interface: .fullStatus
            )
        }

        let transition = productionStatusState.apply(dictionary: object, interface: .fullStatus)
        guard transition.accepted else {
            return failClosedStatusSnapshot(
                reasonCode: productionStatusState.reasonCode,
                interface: .fullStatus
            )
        }
        if transition.mustClearCurrentState {
            return statusSnapshotFromProductionState()
        }

        guard object["runtime_identity"] is [String: Any],
              object["launch_agent_loaded"] is Bool,
              object["launch_agent_running"] is Bool,
              object["capture_allowed_by_session"] is Bool,
              object["today_activity_count"] is Int,
              object["today_privacy_count"] is Int,
              object["today_gap_count"] is Int,
              object["raw_residue_count"] is Int,
              object["raw_access"] as? Bool == false
        else {
            return failClosedStatusSnapshot(
                reasonCode: "invalid_full_status_shape",
                interface: .fullStatus
            )
        }

        let attention = (object["attention_items"] as? [[String: Any]] ?? []).map(AttentionItem.init(dictionary:))
        let soakScorecard = SoakScorecard(dictionary: object["soak_scorecard"] as? [String: Any] ?? [:])
        let sessionTransitionEvidence = SessionTransitionEvidenceStatus(
            dictionary: object["session_transition_evidence"] as? [String: Any] ?? [:]
        )
        let runtime = RuntimeIdentity(dictionary: object["runtime_identity"] as? [String: Any] ?? [:])
        let dashboard = DashboardHealth(dictionary: object["dashboard_health"] as? [String: Any] ?? [:])
        let freshness = CaptureFreshness(dictionary: object["capture_freshness"] as? [String: Any] ?? [:])
        let screenRecordingPermission = ScreenRecordingPermissionStatus(
            dictionary: object["screen_recording_permission"] as? [String: Any] ?? [:]
        )
        let coverage = CoverageHealth(dictionary: object["coverage_health"] as? [String: Any] ?? [:])
        let privacy = PrivacyHealth(dictionary: object["privacy_health"] as? [String: Any] ?? [:])
        let confirmationEvidenceHealth = ConfirmationEvidenceHealth(
            dictionary: object["confirmation_evidence_health"] as? [String: Any] ?? [:]
        )
        let hygieneCleanup = HygieneCleanup(dictionary: object["hygiene_cleanup"] as? [String: Any] ?? [:])
        let haAutomation = HAAutomation(dictionary: object["ha_automation"] as? [String: Any] ?? [:])
        let qualityAdvice = QualityAdvice(dictionary: object["quality_advice"] as? [String: Any] ?? [:])
        let structuredSignalHealth = StructuredSignalHealth(dictionary: object["structured_signal_health"] as? [String: Any] ?? [:])
        let qualityHealth = QualityHealth(dictionary: object["quality_health"] as? [String: Any] ?? [:])
        let documentChanges = documentChangesSnapshot(from: object["document_changes"])
        let confirmedActivity = ConfirmedActivityStatus(dictionary: object["confirmed_activity"] as? [String: Any])
        let currentActivitySession = ActivitySession(dictionary: object["current_activity_session"] as? [String: Any])
        let todayActivitySessions = ActivitySessionsToday(dictionary: object["today_activity_sessions"] as? [String: Any] ?? [:])
        return StatusSnapshot(
            status: productionStatusState.status,
            captureAllowedBySession: object["capture_allowed_by_session"] as? Bool ?? false,
            activities: object["today_activity_count"] as? Int ?? 0,
            privacyEvents: object["today_privacy_count"] as? Int ?? 0,
            partialCleaningCountKnown: ((object["privacy_health"] as? [String: Any])?["partial_redacted_count"] as? Int).map { $0 >= 0 } ?? false,
            gaps: object["today_gap_count"] as? Int ?? 0,
            rawResidue: object["raw_residue_count"] as? Int ?? 0,
            screenCaptureExpiresAt: (object["screen_capture_lease"] as? [String: Any])?["expires_at"] as? String ?? "",
            screenCaptureEnabled: object["screen_capture_enabled"] as? Bool ?? false,
            recordingPaused: object["recording_pause_active"] as? Bool ?? false,
            fileProgressLabel: object["file_progress_label"] as? String ?? "文件追踪状态待确认",
            captureIntervalSeconds: object["capture_interval_seconds"] as? Int ?? 15,
            pausedRemainingSeconds: object["paused_remaining_seconds"] as? Int ?? 0,
            lastCaptureAt: object["last_capture_at"] as? String ?? "",
            nextCaptureAt: object["next_capture_at"] as? String ?? "",
            lastError: object["last_error"] as? String ?? "",
            pauseMode: object["pause_mode"] as? String ?? "",
            pauseLabel: object["pause_label"] as? String ?? "未暂停",
            statusExplanation: object["status_explanation"] as? String ?? "等待状态更新。",
            attentionItems: attention,
            soakScorecard: soakScorecard,
            sessionTransitionEvidence: sessionTransitionEvidence,
            runtimeIdentity: runtime,
            dashboardHealth: dashboard,
            captureFreshness: freshness,
            screenRecordingPermission: screenRecordingPermission,
            coverageHealth: coverage,
            privacyHealth: privacy,
            confirmationEvidenceHealth: confirmationEvidenceHealth,
            hygieneCleanup: hygieneCleanup,
            haAutomation: haAutomation,
            qualityAdvice: qualityAdvice,
            structuredSignalHealth: structuredSignalHealth,
            qualityHealth: qualityHealth,
            documentChanges: documentChanges,
            confirmedActivity: confirmedActivity,
            currentActivitySession: currentActivitySession,
            todayActivitySessions: todayActivitySessions,
            launchAgentLoaded: object["launch_agent_loaded"] as? Bool ?? false,
            launchAgentRunning: object["launch_agent_running"] as? Bool ?? false,
            reasonCode: productionStatusState.reasonCode,
            observedAt: productionStatusState.observedAt,
            lastSuccessfulReadAt: productionStatusState.lastSuccessfulReadAt,
            captureActionsAvailable: transition.actionAvailability == .captureControls,
            hasCurrentTrustedState: productionStatusState.hasCurrentTrustedState,
            serviceStateKnown: true,
            countsStateKnown: true,
            requiresFullRefresh: transition.requiresFullRefresh
        )
    }

    private func quickSnapshotFromJSON(_ text: String, preserving previous: StatusSnapshot) -> StatusSnapshot? {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["interface"] as? String == "quick_status"
        else {
            return failClosedStatusSnapshot(
                reasonCode: "invalid_quick_status_json",
                interface: .quickStatus
            )
        }

        let transition = productionStatusState.apply(dictionary: object, interface: .quickStatus)
        guard transition.accepted else {
            return failClosedStatusSnapshot(
                reasonCode: productionStatusState.reasonCode,
                interface: .quickStatus
            )
        }
        if transition.mustClearCurrentState {
            return statusSnapshotFromProductionState()
        }

        let mayRetainFullEvidence = productionStatusState.hasCurrentFullSnapshot
            && previous.hasCurrentTrustedState
            && previous.serviceStateKnown
            && previous.countsStateKnown
        var snapshot = mayRetainFullEvidence ? previous : StatusSnapshot()
        snapshot.status = productionStatusState.status
        snapshot.statusExplanation = object["status_explanation"] as? String ?? snapshot.statusExplanation
        snapshot.reasonCode = productionStatusState.reasonCode
        snapshot.observedAt = productionStatusState.observedAt
        snapshot.lastSuccessfulReadAt = productionStatusState.lastSuccessfulReadAt
        snapshot.captureAllowedBySession = object["capture_allowed_by_session"] as? Bool ?? false
        snapshot.screenCaptureExpiresAt = (object["screen_capture_lease"] as? [String: Any])?["expires_at"] as? String ?? ""
        snapshot.screenCaptureEnabled = object["screen_capture_enabled"] as? Bool ?? false
        snapshot.recordingPaused = object["recording_pause_active"] as? Bool ?? false
        snapshot.fileProgressLabel = object["file_progress_label"] as? String ?? "文件追踪状态待确认"
        snapshot.captureIntervalSeconds = object["capture_interval_seconds"] as? Int ?? snapshot.captureIntervalSeconds
        snapshot.pausedRemainingSeconds = object["paused_remaining_seconds"] as? Int ?? snapshot.pausedRemainingSeconds
        snapshot.pauseMode = object["pause_mode"] as? String ?? snapshot.pauseMode
        snapshot.pauseLabel = object["pause_label"] as? String ?? snapshot.pauseLabel
        snapshot.lastCaptureAt = object["last_capture_at"] as? String ?? snapshot.lastCaptureAt
        snapshot.nextCaptureAt = object["next_capture_at"] as? String ?? snapshot.nextCaptureAt

        if let loaded = object["launch_agent_loaded"] as? Bool,
           let running = object["launch_agent_running"] as? Bool {
            snapshot.launchAgentLoaded = loaded
            snapshot.launchAgentRunning = running
            snapshot.serviceStateKnown = true
        } else {
            snapshot.serviceStateKnown = mayRetainFullEvidence
        }

        if let activities = object["today_activity_count"] as? Int,
           let privacyEvents = object["today_privacy_count"] as? Int,
           let gaps = object["today_gap_count"] as? Int,
           let rawResidue = object["raw_residue_count"] as? Int {
            snapshot.activities = activities
            snapshot.privacyEvents = privacyEvents
            snapshot.gaps = gaps
            snapshot.rawResidue = rawResidue
            snapshot.countsStateKnown = true
        } else {
            snapshot.countsStateKnown = mayRetainFullEvidence
        }

        if let dashboard = object["dashboard_health"] as? [String: Any] {
            snapshot.dashboardHealth = DashboardHealth(dictionary: dashboard)
        }
        if let freshness = object["capture_freshness"] as? [String: Any] {
            snapshot.captureFreshness = CaptureFreshness(dictionary: freshness)
        }
        if let coverage = object["coverage_health"] as? [String: Any] {
            snapshot.coverageHealth = CoverageHealth(dictionary: coverage)
        }
        if let privacy = object["privacy_health"] as? [String: Any] {
            snapshot.privacyHealth = PrivacyHealth(dictionary: privacy)
        }
        if let quality = object["quality_health"] as? [String: Any] {
            snapshot.qualityHealth = QualityHealth(dictionary: quality)
        }
        if let structured = object["structured_signal_health"] as? [String: Any] {
            snapshot.structuredSignalHealth = StructuredSignalHealth(dictionary: structured)
        }
        if let documentChanges = object["document_changes"] {
            snapshot.documentChanges = documentChangesSnapshot(from: documentChanges)
        }
        if let confirmed = object["confirmed_activity"] as? [String: Any] {
            snapshot.confirmedActivity = ConfirmedActivityStatus(dictionary: confirmed)
        }
        if let currentSession = object["current_activity_session"] as? [String: Any] {
            snapshot.currentActivitySession = ActivitySession(dictionary: currentSession)
        }
        if let todaySessions = object["today_activity_sessions"] as? [String: Any] {
            snapshot.todayActivitySessions = ActivitySessionsToday(dictionary: todaySessions)
        }

        if let hasError = object["last_error_present"] as? Bool {
            if hasError {
                if snapshot.lastError.isEmpty {
                    snapshot.lastError = "recent_capture_error"
                }
            } else {
                snapshot.lastError = ""
            }
        }

        snapshot.hasCurrentTrustedState = snapshot.serviceStateKnown && snapshot.countsStateKnown
        snapshot.captureActionsAvailable = transition.actionAvailability == .captureControls
            && snapshot.hasCurrentTrustedState
        snapshot.requiresFullRefresh = transition.requiresFullRefresh || !snapshot.hasCurrentTrustedState
        if snapshot.requiresFullRefresh && !snapshot.hasCurrentTrustedState {
            snapshot.statusExplanation = "快速状态已恢复；完整服务、计数、健康、会话、文档和可信上下文等待全量刷新。"
            snapshot.documentChanges = DocumentChangesSnapshot()
            snapshot.confirmedActivity = ConfirmedActivityStatus()
            snapshot.currentActivitySession = nil
            snapshot.todayActivitySessions = ActivitySessionsToday()
        }
        return snapshot
    }

    private func statusSnapshotFromProductionState() -> StatusSnapshot {
        StatusSnapshot.failClosedUnknown(
            observedAt: productionStatusState.observedAt,
            lastSuccessfulReadAt: productionStatusState.lastSuccessfulReadAt,
            reasonCode: productionStatusState.reasonCode
        )
    }

    private func failClosedStatusSnapshot(
        reasonCode: String,
        interface: ProductionStatusInterface
    ) -> StatusSnapshot {
        let observedAt = Self.statusObservationFormatter.string(from: Date())
        let lastSuccessfulReadAt: Any = productionStatusState.lastSuccessfulReadAt ?? NSNull()
        let object: [String: Any] = [
            "status": "unknown",
            "reason_code": reasonCode.isEmpty ? "invalid_status_payload" : reasonCode,
            "interface": interface == .fullStatus ? "full_status" : "quick_status",
            "observed_at": observedAt,
            "last_successful_read_at": lastSuccessfulReadAt,
            "capture_allowed_by_session": false,
            "actions_enabled": false,
            "raw_access": false,
        ]
        _ = productionStatusState.apply(dictionary: object, interface: interface)
        return statusSnapshotFromProductionState()
    }

    private func statusLine(from snapshot: StatusSnapshot) -> String {
        if snapshot.displayStateUnknown {
            return snapshot.unknownStatusLine
        }
        if !snapshot.countsStateKnown || !snapshot.serviceStateKnown {
            return "状态未知 · \(snapshot.observationTimeLabel) · 等待完整状态刷新"
        }
        let activityLine = snapshot.confirmedActivity.isAvailable
            ? "今日已确认 \(snapshot.confirmedActivity.confirmedCount) 条"
            : "今日确认事实暂不可读"
        let base = "\(snapshot.statusLabelCN) - \(activityLine)，\(snapshot.privacyEvents) 条已保护"
        if snapshot.hasKnownCaptureError {
            return "\(base) - 最近采集错误仍未解决"
        }
        if snapshot.screenRecordingPermission.denied {
            return "\(base) - 需要屏幕录制权限"
        }
        if screenRecordingWarningActive {
            return "\(base) - 后台采集权限待确认"
        }
        if !snapshot.launchAgentLoaded && snapshot.status != "capturing" {
            return "\(base) - 服务已停止"
        }
        return base
    }

    nonisolated private static func coreDirectory() -> URL {
        if let override = ProcessInfo.processInfo.environment["PERSONAL_AI_MONITOR_CORE_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        let bundle = Bundle.main.bundleURL
        let repoRoot = bundle.deletingLastPathComponent().deletingLastPathComponent()
        let repoCore = repoRoot.appendingPathComponent("core", isDirectory: true)
        if FileManager.default.fileExists(atPath: repoCore.path) {
            return repoCore
        }
        let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support", isDirectory: true)
        return appSupport
            .appendingPathComponent("Personal AI Monitor", isDirectory: true)
            .appendingPathComponent("core", isDirectory: true)
    }

    nonisolated private static func haPackageCacheDirectory() -> URL {
        coreDirectory()
            .appendingPathComponent("data", isDirectory: true)
            .appendingPathComponent("ha_packages", isDirectory: true)
    }

    nonisolated private static func packageSourceURL(from path: String) -> URL {
        if path.hasPrefix("/") {
            return URL(fileURLWithPath: path)
        }
        return coreDirectory().appendingPathComponent(path)
    }
}
