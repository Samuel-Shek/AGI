import AppKit
import CryptoKit
import Darwin
import Foundation
import PAMInteractionModel
import SwiftUI

// MARK: - App root and feature flag

extension Phase2Destination {
    var systemImage: String {
        switch self {
        case .today: return "square.grid.2x2"
        case .find: return "magnifyingglass"
        case .memory: return "brain.head.profile"
        case .settings: return "gearshape"
        case .legacy: return "clock.arrow.circlepath"
        }
    }
}

struct Phase2ProcessOutput: Sendable {
    let standardOutput: Data
    let standardError: Data
    let exitCode: Int32
    let timedOut: Bool
}

enum Phase2ProcessExecutionError: Error, Sendable {
    case timedOut
}

final class Phase2CoreProcessExecutor: @unchecked Sendable {
    func execute(
        arguments: [String],
        standardInput: Data?,
        timeout: TimeInterval = 8
    ) async -> Phase2ProcessOutput {
        let pythonURL = Self.pythonExecutableURL()
        let coreURL = Self.coreDirectory()
        return await Task.detached(priority: .userInitiated) {
            Self.executeBlocking(
                arguments: arguments,
                standardInput: standardInput,
                timeout: max(1, min(timeout, 15)),
                pythonURL: pythonURL,
                coreURL: coreURL
            )
        }.value
    }

    private static func executeBlocking(
        arguments: [String],
        standardInput: Data?,
        timeout: TimeInterval,
        pythonURL: URL,
        coreURL: URL
    ) -> Phase2ProcessOutput {
        let process = Process()
        process.executableURL = pythonURL
        process.arguments = ["-m", "activity_mvp"] + arguments
        process.currentDirectoryURL = coreURL
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = coreURL.path
        environment["PERSONAL_AI_MONITOR_PYTHON"] = pythonURL.path
        process.environment = environment

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
        let inputPipe: Pipe? = standardInput == nil ? nil : Pipe()
        process.standardInput = inputPipe

        let stdout = Phase2DataBuffer()
        let stderr = Phase2DataBuffer()
        let readers = DispatchGroup()
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in finished.signal() }

        do {
            try process.run()
            if let inputPipe {
                inputPipe.fileHandleForWriting.write(standardInput ?? Data())
                try? inputPipe.fileHandleForWriting.close()
            }
            readers.enter()
            DispatchQueue.global(qos: .userInitiated).async {
                stdout.append(stdoutPipe.fileHandleForReading.readDataToEndOfFile())
                readers.leave()
            }
            readers.enter()
            DispatchQueue.global(qos: .utility).async {
                stderr.append(stderrPipe.fileHandleForReading.readDataToEndOfFile())
                readers.leave()
            }

            let timedOut = finished.wait(timeout: .now() + timeout) == .timedOut
            if timedOut {
                process.terminate()
                if finished.wait(timeout: .now() + 1) == .timedOut, process.isRunning {
                    kill(process.processIdentifier, SIGKILL)
                    _ = finished.wait(timeout: .now() + 1)
                }
            }
            readers.wait()
            return Phase2ProcessOutput(
                standardOutput: stdout.snapshot(),
                standardError: stderr.snapshot(),
                exitCode: process.terminationStatus,
                timedOut: timedOut
            )
        } catch {
            try? inputPipe?.fileHandleForWriting.close()
            return Phase2ProcessOutput(
                standardOutput: Data(),
                standardError: Data(error.localizedDescription.utf8),
                exitCode: -1,
                timedOut: false
            )
        }
    }

    private static func pythonExecutableURL() -> URL {
        if let configured = ProcessInfo.processInfo.environment["PERSONAL_AI_MONITOR_PYTHON"],
           !configured.isEmpty {
            return URL(fileURLWithPath: configured)
        }
        let frameworkPython = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
        if FileManager.default.isExecutableFile(atPath: frameworkPython) {
            return URL(fileURLWithPath: frameworkPython)
        }
        return URL(fileURLWithPath: "/usr/bin/python3")
    }

    private static func coreDirectory() -> URL {
        if let configured = ProcessInfo.processInfo.environment["PERSONAL_AI_MONITOR_CORE_DIR"],
           !configured.isEmpty {
            return URL(fileURLWithPath: configured, isDirectory: true)
        }
        let bundle = Bundle.main.bundleURL
        let repositoryCore = bundle
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("core", isDirectory: true)
        if FileManager.default.fileExists(atPath: repositoryCore.path) {
            return repositoryCore
        }
        let applicationSupport = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/Application Support", isDirectory: true)
        return applicationSupport
            .appendingPathComponent("Personal AI Monitor", isDirectory: true)
            .appendingPathComponent("core", isDirectory: true)
    }
}

extension Phase2CoreProcessExecutor: CoreCommandExecuting {
    func execute(_ request: CoreCommandRequest) async throws -> CoreCommandResult {
        let output = await execute(
            arguments: request.arguments,
            standardInput: request.standardInput,
            timeout: request.timeoutSeconds
        )
        guard !output.timedOut else { throw Phase2ProcessExecutionError.timedOut }
        return CoreCommandResult(
            exitCode: output.exitCode,
            standardOutput: output.standardOutput,
            standardError: output.standardError
        )
    }
}

struct Phase2ContextConsoleService: ContextConsoleServicing {
    let executor: any CoreCommandExecuting

    init(executor: any CoreCommandExecuting = Phase2CoreProcessExecutor()) {
        self.executor = executor
    }

    func loadSnapshot(query: ContextSnapshotQuery) async throws -> ContextSnapshotV1 {
        let data = try await run(try ContextConsoleCoreCommands.snapshot(query))
        return try ContextSnapshotV1(data: data)
    }

    func loadMemoryCandidates() async throws -> MemoryCandidateListV1 {
        let data = try await run(ContextConsoleCoreCommands.memoryCandidates())
        return try MemoryCandidateListV1(data: data)
    }

    func loadDeliveryStatus() async throws -> DeliveryStatusV1 {
        let data = try await run(ContextConsoleCoreCommands.deliveryStatus())
        return try DeliveryStatusV1(data: data)
    }

    func loadProductExperimentStatus() async throws -> ProductExperimentStatusV1 {
        let data = try await run(ContextConsoleCoreCommands.productExperimentStatus())
        return try ProductExperimentStatusV1(data: data)
    }

    func buildLocalHandoff(projectRef: String) async throws -> LocalHandoffV1 {
        let data = try await run(
            try ContextConsoleCoreCommands.localHandoff(projectRef: projectRef)
        )
        return try LocalHandoffV1(data: data)
    }

    func submitProductFeedback(
        _ request: ProductFeedbackRequestV1
    ) async throws -> ProductFeedbackV1 {
        let data = try await run(
            try ContextConsoleCoreCommands.productFeedback(request)
        )
        return try ProductFeedbackV1(data: data)
    }

    func submitOwnerAction(
        _ action: OwnerContextActionV1
    ) async throws -> OwnerContextActionResultV1 {
        let data = try await run(
            try ContextConsoleCoreCommands.ownerAction(action)
        )
        return try OwnerContextActionResultV1(data: data)
    }

    private func run(_ request: CoreCommandRequest) async throws -> Data {
        let result = try await executor.execute(request)
        _ = try CoreCommandJSONBoundary.decodeObject(from: result)
        return result.standardOutput
    }
}

private final class Phase2DataBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()

    func append(_ newData: Data) {
        guard !newData.isEmpty else { return }
        lock.lock()
        data.append(newData)
        lock.unlock()
    }

    func snapshot() -> Data {
        lock.lock()
        let result = data
        lock.unlock()
        return result
    }
}

enum Phase2MenuHealth: String {
    case normal = "正常"
    case paused = "暂停"
    case error = "异常"

    static func resolve(_ snapshot: StatusSnapshot) -> Self {
        switch Phase2SystemHealth.resolve(
            displayStateUnknown: snapshot.displayStateUnknown,
            status: snapshot.status,
            statusIsDanger: snapshot.statusKind == .danger,
            hasKnownCaptureError: snapshot.hasKnownCaptureError,
            launchAgentLoaded: snapshot.launchAgentLoaded,
            launchAgentRunning: snapshot.launchAgentRunning,
            captureFreshnessIsStale: snapshot.captureFreshness.isStale,
            captureBlockedBySession: snapshot.captureBlockedBySession
        ) {
        case .normal:
            return .normal
        case .paused:
            return .paused
        case .error:
            return .error
        }
    }
}

struct PAMAppRootView: View {
    @ObservedObject var runner: CoreRunner
    @ObservedObject var windowState: AppWindowState
    private let phase2Enabled = false

    var body: some View {
        Group {
            if phase2Enabled {
                Phase2ContextConsoleRoot(runner: runner, windowState: windowState)
                    .transition(.opacity)
            } else {
                ContentView(runner: runner, windowState: windowState)
            }
        }
        .animation(.easeOut(duration: 0.16), value: phase2Enabled)
    }
}

// MARK: - Presentation-only safe models

enum Phase2UIPresentationState: Equatable {
    case loading
    case available
    case disabled
    case unavailable(reason: String, observedAt: String)

    var isAvailable: Bool {
        if case .available = self { return true }
        return false
    }
}

struct Phase2UIContextItem: Identifiable, Hashable {
    let id: String
    let kind: String
    let safeLabel: String
    let sourceLabel: String
    let occurredAt: String
}

struct Phase2UIClue: Identifiable, Hashable {
    let id: String
    let safeLabel: String
    let sourceLabel: String
}

struct Phase2UIProject: Identifiable, Hashable {
    let id: String
    let title: String
    let verifiedItems: [Phase2UIContextItem]
    let compatibilityItems: [Phase2UIContextItem]
    let clues: [Phase2UIClue]
    let nextStep: String?
    let handoffEligible: Bool
    let ownerActionEligible: Bool

    var resultCount: Int {
        verifiedItems.filter { ["结果", "已完成", "已送达"].contains($0.kind) }.count
    }
}

struct Phase2UIConfirmation: Identifiable, Hashable {
    let id: String
    let revision: String
    let project: String
    let title: String
    let question: String
    let evidenceLabel: String
    let answerOptions: Set<String>
}

enum Phase2UIDeliveryStage: String, Hashable {
    case needsInput
    case prepared
    case consumed
    case delivered
    case failed
    case withdrawn
    case unknown

    var title: String {
        switch self {
        case .needsInput: return "待补充"
        case .prepared: return "AGI 已准备"
        case .consumed: return "Hermes 已消费"
        case .delivered: return "飞书已送达"
        case .failed: return "送达失败"
        case .withdrawn: return "已撤回 / 受限"
        case .unknown: return "暂无可信回执"
        }
    }

    var systemImage: String {
        switch self {
        case .needsInput: return "questionmark.circle"
        case .prepared: return "shippingbox"
        case .consumed: return "arrow.down.circle"
        case .delivered: return "checkmark.seal"
        case .failed: return "exclamationmark.triangle"
        case .withdrawn: return "arrow.uturn.backward.circle"
        case .unknown: return "minus.circle"
        }
    }

    var tone: Phase2Tone {
        switch self {
        case .delivered: return .success
        case .failed, .withdrawn: return .danger
        case .needsInput: return .attention
        case .prepared, .consumed: return .accent
        case .unknown: return .muted
        }
    }
}

struct Phase2UIDelivery: Identifiable, Hashable {
    let id: String
    let project: String
    let stage: Phase2UIDeliveryStage
    let updatedAt: String
}

struct Phase2UIMemory: Identifiable, Hashable {
    let id: String
    let safeSummary: String
    let project: String
    let state: String
    let source: String
    let expiresAt: String
}

struct Phase2UIHandoff: Hashable {
    let sourceRef: String
    let projectRef: String
    let renderToken: Phase2HandoffRenderToken?
    let project: String
    let objective: String
    let results: [String]
    let blockers: [String]
    let nextSteps: [String]
    let sources: [String]
    let unresolvedQuestions: [String]

    var renderIdentity: Phase2UIHandoffRenderIdentity {
        Phase2UIHandoffRenderIdentity(
            sourceRef: sourceRef,
            projectRef: projectRef,
            timerGeneration: renderToken?.timerGeneration
        )
    }
}

struct Phase2UIHandoffRenderIdentity: Hashable {
    let sourceRef: String
    let projectRef: String
    let timerGeneration: UInt64?
}

@MainActor
final class Phase2ConsoleCoordinator: ObservableObject {
    @Published private(set) var presentationState: Phase2UIPresentationState = .loading
    @Published private(set) var todayProjects: [Phase2UIProject] = []
    @Published private(set) var searchProjects: [Phase2UIProject] = []
    @Published private(set) var confirmations: [Phase2UIConfirmation] = []
    @Published private(set) var deliveries: [Phase2UIDelivery] = []
    @Published private(set) var memories: [Phase2UIMemory] = []
    @Published private(set) var handoff: Phase2UIHandoff?
    @Published private(set) var lastUpdatedAt = ""
    @Published private(set) var message = ""
    @Published private(set) var handoffMessage = ""
    @Published private(set) var feedbackMessage = ""
    @Published private(set) var quickCaptureMessage = ""
    @Published private(set) var confirmationMessage = ""
    @Published private(set) var actionsEnabled = false
    @Published private(set) var feedbackEnabled = false
    @Published private(set) var feedbackSubmitting = false
    @Published private(set) var feedbackActionAuthorization: Phase2FeedbackActionAuthorization?
    @Published private(set) var submittingConfirmationIDs: Set<String> = []
    @Published private(set) var searching = false
    @Published private(set) var buildingHandoff = false
    @Published var selectedProjectID: String?
    @Published var searchQuery = ""
    @Published var searchProjectRef = ""
    @Published var searchTimePreset: Phase2FindTimePreset = .retentionWindow

    private let service: any ContextConsoleServicing
    private var viewState = Phase2ConsoleViewState()
    private var refreshGeneration = Phase2RefreshGenerationState()
    private var resumeTimer = Phase2ResumeTimerState()
    private var searchResultSelection = Phase2SearchResultSelectionState()
    private var experimentFeedbackBinding = Phase2ExperimentFeedbackBindingState()
    private var feedbackActionAuthorizationState = Phase2FeedbackActionAuthorizationState()
    private var handoffRenderCheckpoint = Phase2HandoffRenderCheckpointState()
    private var feedbackCapabilityEnabled = false

    init(service: any ContextConsoleServicing = Phase2ContextConsoleService()) {
        self.service = service
    }

    func refresh(runner: CoreRunner) async {
        let rootGeneration = refreshGeneration.begin()
        viewState.invalidateAllRequests()
        resumeTimer.cancel()
        let token = viewState.beginRequest(for: .today)
        presentationState = .loading
        // A refresh never leaves the previous trusted snapshot actionable
        // while status/Core reads are in flight.
        clearTrustedContent()

        await runner.refreshStatus()
        guard refreshGeneration.isCurrent(rootGeneration) else { return }
        if runner.statusSnapshot.displayStateUnknown {
            guard viewState.applyFailure(
                .unavailable,
                observedAt: Self.isoTimestamp(),
                token: token
            ) else { return }
            clearTrustedContent()
            presentationState = .unavailable(
                reason: "状态未知；旧的可信内容已清空。",
                observedAt: runner.statusSnapshot.observationTimeLabel
            )
            return
        }

        do {
            let query = try ContextSnapshotQuery(includeClues: true, limit: 50)
            let snapshot = try await service.loadSnapshot(query: query)
            guard refreshGeneration.isCurrent(rootGeneration) else { return }
            guard viewState.apply(snapshot: snapshot, for: .today, token: token) else { return }
            apply(snapshot: snapshot, asSearchResult: false)

            guard snapshot.readState == .available else { return }
            guard await loadProductExperimentStatus(rootGeneration: rootGeneration) else { return }
            guard await loadMemoryCandidates(rootGeneration: rootGeneration) else { return }
            _ = await loadDeliveryStatus(rootGeneration: rootGeneration)
        } catch {
            guard refreshGeneration.isCurrent(rootGeneration) else { return }
            guard viewState.applyFailure(
                .unavailable,
                observedAt: Self.isoTimestamp(),
                token: token
            ) else { return }
            clearTrustedContent()
            presentationState = .unavailable(
                reason: "可信上下文读取失败；未使用旧数据或命令行搜索回退。",
                observedAt: Self.isoTimestamp()
            )
        }
    }

    func search(runner: CoreRunner) async {
        viewState.invalidateRequests([.handoff, .feedback])
        invalidateFeedbackActionAuthorization()
        let token = viewState.beginRequest(for: .search)
        let measurementGeneration = resumeTimer.beginSearch(
            atUptimeNanoseconds: Self.uptimeNanoseconds()
        )
        searchResultSelection.beginSearch()
        handoffRenderCheckpoint.cancel()
        handoff = nil
        searchProjects = []
        selectedProjectID = nil
        handoffMessage = ""
        feedbackMessage = ""
        buildingHandoff = false
        feedbackSubmitting = experimentFeedbackBinding.submissionInFlight
        updateFeedbackAvailability()
        searching = true
        guard presentationState.isAvailable else {
            _ = viewState.cancel(token)
            resumeTimer.cancel()
            searching = false
            return
        }
        let trimmed = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        let projectRef = searchProjectRef.isEmpty ? nil : searchProjectRef
        let timeRange = searchTimePreset.utcRange()
        do {
            let query = try ContextSnapshotQuery(
                projectRef: projectRef,
                startAt: timeRange.startAt,
                endAt: timeRange.endAt,
                query: trimmed.isEmpty ? nil : trimmed,
                includeClues: true,
                limit: 100
            )
            let snapshot = try await service.loadSnapshot(query: query)
            guard viewState.apply(snapshot: snapshot, for: .search, token: token) else { return }
            apply(snapshot: snapshot, asSearchResult: true)
            searchResultSelection.accept(
                timerGeneration: measurementGeneration,
                projectRefs: Set(searchProjects.map(\.id))
            )
            searching = false
        } catch {
            guard viewState.applyFailure(
                .unavailable,
                observedAt: Self.isoTimestamp(),
                token: token
            ) else { return }
            searching = false
            resumeTimer.cancel()
            clearTrustedContent()
            presentationState = .unavailable(
                reason: "安全搜索不可用；没有把查询放入命令行参数。",
                observedAt: Self.isoTimestamp()
            )
        }
    }

    func selectProject(_ project: Phase2UIProject) async {
        guard !searching,
              searchResultSelection.canSelect(
                  timerGeneration: resumeTimer.generation,
                  projectRef: project.id
              ),
              searchProjects.first(where: { $0.id == project.id }) == project
        else { return }
        viewState.invalidateRequests([.handoff, .feedback])
        invalidateFeedbackActionAuthorization()
        feedbackSubmitting = experimentFeedbackBinding.submissionInFlight
        buildingHandoff = false
        handoffRenderCheckpoint.cancel()
        let measurementGeneration = resumeTimer.generation
        let timerStillBound = resumeTimer.select(projectRef: project.id)
        selectedProjectID = project.id
        handoff = nil
        handoffMessage = ""
        feedbackMessage = ""
        updateFeedbackAvailability()
        guard project.handoffEligible else {
            resumeTimer.cancel()
            handoffMessage = "该项目目前没有仍然有效的可信上下文；历史兼容记录和待核实线索不能生成续接包。"
            return
        }
        let token = viewState.beginRequest(for: .handoff)
        buildingHandoff = true
        do {
            let result = try await service.buildLocalHandoff(projectRef: project.id)
            if result.readState == .blocked {
                guard viewState.apply(localHandoff: result, token: token) else { return }
                buildingHandoff = false
                resumeTimer.cancel()
                handoff = nil
                handoffMessage = "该项目的可信上下文已过期、撤回或不再满足续接包条件；搜索结果仍保留。"
                return
            }
            guard viewState.apply(localHandoff: result, token: token) else { return }
            buildingHandoff = false
            guard result.readState == .available else {
                clearTrustedContent()
                presentationState = readPresentationState(
                    result.readState,
                    observedAt: result.observedAt
                )
                return
            }
            guard !result.revoked,
                  let sourceRef = result.handoffRef,
                  let returnedProject = result.project,
                  returnedProject.ref == project.id,
                  returnedProject.ref == selectedProjectID,
                  let objective = result.objective,
                  let results = result.results,
                  let blockers = result.blockers,
                  let nextSteps = result.nextSteps,
                  let sources = result.sources,
                  let unresolved = result.unresolvedQuestions
            else {
                clearTrustedContent()
                presentationState = .unavailable(
                    reason: "续接包已撤回或不满足可信契约；旧内容已清空。",
                    observedAt: result.observedAt
                )
                return
            }
            let renderToken = timerStillBound
                ? handoffRenderCheckpoint.stage(
                    sourceRef: sourceRef,
                    projectRef: returnedProject.ref,
                    timerGeneration: measurementGeneration
                )
                : nil
            handoff = Phase2UIHandoff(
                sourceRef: sourceRef,
                projectRef: returnedProject.ref,
                renderToken: renderToken,
                project: returnedProject.displayLabel,
                objective: objective,
                results: results,
                blockers: blockers,
                nextSteps: nextSteps,
                sources: sources.map {
                    "\($0.safeLabel) · \(sourceDisplayLabel($0.sourceLabel, verified: true))"
                },
                unresolvedQuestions: unresolved
            )
            updateFeedbackAvailability()
            if renderToken == nil {
                feedbackMessage = "本次找回计时已取消；请重新搜索后再反馈。"
            } else if experimentFeedbackBinding.submissionCheckpoint == .transportUnknown,
                      experimentFeedbackBinding.pendingRetry?.request.sourceRef == sourceRef {
                feedbackMessage = "上一份反馈回执未到达；只可重试原来的同一选择。"
            } else if experimentFeedbackBinding.runningExperimentRef == nil {
                feedbackMessage = "当前没有运行中的自用实验；本次续接包不会写入实验指标。"
            }
        } catch {
            guard viewState.applyFailure(
                .unavailable,
                observedAt: Self.isoTimestamp(),
                token: token
            ) else { return }
            buildingHandoff = false
            resumeTimer.cancel()
            clearTrustedContent()
            presentationState = .unavailable(
                reason: "续接包读取失败；旧续接包已清空。",
                observedAt: Self.isoTimestamp()
            )
        }
    }

    func cancelSearch() {
        viewState.invalidateRequests([.search, .handoff, .feedback])
        invalidateFeedbackActionAuthorization()
        resumeTimer.cancel()
        searchResultSelection.cancel()
        handoffRenderCheckpoint.cancel()
        searching = false
        buildingHandoff = false
        feedbackSubmitting = experimentFeedbackBinding.submissionInFlight
        handoff = nil
        searchProjects = []
        selectedProjectID = nil
        updateFeedbackAvailability()
    }

    /// Called when the independently refreshed runtime status loses trust.
    /// This invalidates in-flight Context responses before clearing the
    /// published UI arrays, so a slow old response cannot repopulate them.
    func runtimeStatusBecameUnknown(observedAt: String) {
        let timestamp = observedAt.isEmpty ? Self.isoTimestamp() : observedAt
        guard viewState.applyRuntimeTrustLoss(
            .unavailable,
            observedAt: timestamp
        ) else { return }
        clearTrustedContent()
        presentationState = .unavailable(
            reason: "运行状态未知；旧的可信内容和写入能力已清空。",
            observedAt: timestamp
        )
    }

    func beginFindFlow() {
        viewState.invalidateRequests([.search, .handoff, .feedback])
        invalidateFeedbackActionAuthorization()
        let measurementGeneration = resumeTimer.beginFindEntry(
            atUptimeNanoseconds: Self.uptimeNanoseconds()
        )
        searchResultSelection.accept(
            timerGeneration: measurementGeneration,
            projectRefs: Set(searchProjects.map(\.id))
        )
        handoffRenderCheckpoint.cancel()
        searching = false
        buildingHandoff = false
        feedbackSubmitting = experimentFeedbackBinding.submissionInFlight
        selectedProjectID = nil
        handoff = nil
        handoffMessage = ""
        feedbackMessage = ""
        updateFeedbackAvailability()
    }

    /// Called by the preview only after SwiftUI has inserted this exact
    /// verified-only handoff into the visible hierarchy.
    func handoffBecameVisible(renderToken: Phase2HandoffRenderToken) {
        guard let handoff,
              handoff.renderToken == renderToken,
              handoffRenderCheckpoint.consume(
                  renderToken,
                  currentSourceRef: handoff.sourceRef,
                  currentProjectRef: handoff.projectRef,
                  currentTimerGeneration: resumeTimer.generation
              ),
              selectedProjectID == handoff.projectRef
        else { return }
        let measurement = resumeTimer.freeze(
            sourceRef: handoff.sourceRef,
            projectRef: handoff.projectRef,
            atUptimeNanoseconds: Self.uptimeNanoseconds(),
            generation: renderToken.timerGeneration
        )
        if measurement != nil {
            bindFeedbackActionAuthorization(
                sourceRef: handoff.sourceRef,
                projectRef: handoff.projectRef,
                timerGeneration: renderToken.timerGeneration
            )
        } else {
            invalidateFeedbackActionAuthorization()
        }
        updateFeedbackAvailability()
        if measurement == nil {
            feedbackMessage = "本次找回计时已取消；请重新搜索后再反馈。"
        } else if experimentFeedbackBinding.submissionCheckpoint == .transportUnknown,
                  experimentFeedbackBinding.pendingRetry?.request.sourceRef == handoff.sourceRef {
            feedbackMessage = "上一份反馈回执未到达；只可重试原来的同一选择。"
        } else if experimentFeedbackBinding.runningExperimentRef == nil {
            feedbackMessage = "当前没有运行中的自用实验；本次续接包不会写入实验指标。"
        }
    }

    func submitHandoffFeedback(
        choice: Phase2ProductFeedbackChoice,
        expectedAuthorization: Phase2FeedbackActionAuthorization
    ) async {
        // This is the first action in the async entry point. Never read the
        // current preview and reinterpret an older SwiftUI closure as an
        // action for it: the closure must still own the exact handoff/project
        // and monotonic timer generation that created its buttons.
        guard let handoff,
              feedbackActionAuthorization == expectedAuthorization,
              feedbackActionAuthorizationState.authorizes(
                  expectedAuthorization,
                  currentSourceRef: handoff.sourceRef,
                  currentProjectRef: handoff.projectRef,
                  currentTimerGeneration: resumeTimer.generation
              )
        else { return }
        guard feedbackEnabled,
              !feedbackSubmitting,
              let measurement = resumeTimer.measurement,
              measurement.sourceRef == handoff.sourceRef,
              measurement.projectRef == handoff.projectRef,
              let feedbackExperimentRef = experimentFeedbackBinding.experimentRefForSubmission(
                  sourceRef: handoff.sourceRef
              )
        else {
            feedbackMessage = "当前源码安全门禁未开放产品反馈写入。"
            return
        }
        let candidate = ProductFeedbackRequestV1(
            idempotencyKey: Self.makeIdempotencyKey(
                "feedback|\(choice.rawValue)|\(handoff.sourceRef)|\(feedbackExperimentRef)"
            ),
            sourceKind: .localHandoff,
            sourceRef: handoff.sourceRef,
            projectRef: handoff.projectRef,
            useful: choice.useful,
            resumeSeconds: measurement.resumeSeconds,
            experimentRef: feedbackExperimentRef
        )
        guard let request = experimentFeedbackBinding.requestForSubmission(
            candidate,
            choice: choice
        ) else {
            feedbackMessage = "上一份反馈回执状态未知；请重试刚才的同一选择，或重新开始找回。"
            return
        }
        let token = viewState.beginRequest(for: .feedback)
        feedbackSubmitting = true
        updateFeedbackAvailability()
        feedbackMessage = "正在记录…"
        do {
            let result = try await service.submitProductFeedback(request)
            // Presentation navigation may invalidate this token, but it cannot
            // revoke a request that already crossed the Core boundary.
            _ = viewState.cancel(token)
            feedbackSubmitting = false
            guard ["recorded", "replayed"].contains(result.status),
                  result.feedbackRef != nil,
                  result.sourceKind == request.sourceKind,
                  result.sourceRef == request.sourceRef,
                  result.useful == request.useful,
                  result.winRecorded == request.useful,
                  result.experimentRecorded
            else {
                failClosedFeedbackBinding(
                    message: "反馈回执不完整或来源已失效；已清除本次计时，请刷新后重试。"
                )
                return
            }
            guard experimentFeedbackBinding.markRecordedAwaitingStatusRefresh(
                request: request
            ) else {
                failClosedFeedbackBinding(
                    message: "反馈回执与当前提交检查点不一致；已关闭反馈并等待刷新。"
                )
                return
            }
            updateFeedbackAvailability()
            let phase = await reloadProductExperimentStatusAfterFeedback()
            guard let phase else { return }
            guard self.handoff?.sourceRef == handoff.sourceRef,
                  self.handoff?.projectRef == handoff.projectRef
            else { return }
            let recordedMessage = choice.useful
                ? "已记录为一次有用找回，不保存续接包正文。"
                : "已记录为没帮助并计入实验分母，不生成胜绩。"
            feedbackMessage = phase.isRunning
                ? recordedMessage
                : "\(recordedMessage) 当前实验已冻结，不再接受新的反馈。"
        } catch {
            _ = viewState.cancel(token)
            feedbackSubmitting = false
            guard experimentFeedbackBinding.markTransportUnknown(
                request: request
            ) else { return }
            updateFeedbackAvailability()
            feedbackMessage = "反馈回执未到达；请重试刚才的同一选择，不会改写计时或生成重复记录。"
        }
    }

    private func reloadProductExperimentStatusAfterFeedback() async -> ProductExperimentPhase? {
        let token = viewState.beginRequest(for: .experiment)
        do {
            let result = try await service.loadProductExperimentStatus()
            guard viewState.apply(productExperimentStatus: result, token: token) else {
                return nil
            }
            guard result.readState == .available,
                  let phase = result.phase,
                  phase != .unknown
            else {
                clearTrustedContent()
                presentationState = .unavailable(
                    reason: "反馈已记录，但实验状态未知；旧的可信内容已清空。",
                    observedAt: result.observedAt
                )
                feedbackMessage = "反馈已记录，但实验状态不可读；请刷新后继续。"
                return nil
            }
            let stillRunning = experimentFeedbackBinding.applyExperimentStatus(result)
            if !stillRunning {
                resumeTimer.cancel()
                handoffRenderCheckpoint.cancel()
                invalidateFeedbackActionAuthorization()
            }
            updateFeedbackAvailability()
            return phase
        } catch {
            let observedAt = Self.isoTimestamp()
            guard viewState.applyFailure(
                .unavailable,
                observedAt: observedAt,
                token: token
            ) else { return nil }
            clearTrustedContent()
            presentationState = .unavailable(
                reason: "反馈已记录，但实验状态不可读；旧的可信内容已清空。",
                observedAt: observedAt
            )
            feedbackMessage = "反馈已记录，但实验状态不可读；请刷新后继续。"
            return nil
        }
    }

    private func failClosedFeedbackBinding(message: String) {
        experimentFeedbackBinding.failClosed()
        feedbackSubmitting = false
        resumeTimer.cancel()
        handoffRenderCheckpoint.cancel()
        invalidateFeedbackActionAuthorization()
        updateFeedbackAvailability()
        feedbackMessage = message
    }

    func submitQuickCapture(
        idempotencyKey: String,
        summary: String,
        outcome: String,
        project: Phase2UIProject?
    ) async -> Bool {
        guard actionsEnabled else {
            quickCaptureMessage = "当前补记安全门禁未开放。"
            return false
        }
        let token = viewState.beginRequest(for: .ownerAction)
        quickCaptureMessage = "正在本机保存…"
        let now = Date()
        do {
            let action = OwnerContextActionV1(
                action: .manualNote,
                idempotencyKey: idempotencyKey,
                explicitlyConfirmedOwner: true,
                safeLabel: summary,
                outcome: outcome,
                startedAt: Self.isoTimestamp(now.addingTimeInterval(-60)),
                endedAt: Self.isoTimestamp(now),
                projectRef: project?.ownerActionEligible == true ? project?.id : nil,
                projectLabel: project?.ownerActionEligible == true
                    ? nil
                    : (project?.title ?? "未归类")
            )
            let result = try await service.submitOwnerAction(action)
            guard viewState.cancel(token) else { return false }
            guard ["recorded", "replayed"].contains(result.status),
                  result.actionRef != nil,
                  result.confirmationRef != nil,
                  result.contextRef != nil,
                  result.verificationState == "verified"
            else {
                quickCaptureMessage = "Core 未返回完整原子写入回执，未把补记当成成功。"
                return false
            }
            quickCaptureMessage = "已保存为本机已确认事实。"
            return true
        } catch {
            guard viewState.cancel(token) else { return false }
            quickCaptureMessage = "补记失败或回执不满足安全契约。"
            return false
        }
    }

    func submitConfirmation(
        _ confirmation: Phase2UIConfirmation,
        answer: OwnerConfirmationAnswer,
        runner: CoreRunner
    ) async {
        guard actionsEnabled,
              !submittingConfirmationIDs.contains(confirmation.id),
              confirmation.answerOptions.contains(answer.rawValue)
        else {
            confirmationMessage = "当前确认写入门禁未开放，或该答案不在此卡允许范围内。"
            return
        }
        let token = viewState.beginRequest(for: .ownerAction)
        submittingConfirmationIDs.insert(confirmation.id)
        confirmationMessage = "正在原子记录确认与可信上下文…"
        defer { submittingConfirmationIDs.remove(confirmation.id) }
        do {
            let reason: String
            switch answer {
            case .occurred:
                reason = "correct"
            case .ignored:
                reason = "user_ignored"
            case .notOccurred, .deferred:
                reason = "other"
            }
            let action = OwnerContextActionV1(
                action: .confirmCard,
                idempotencyKey: Self.makeIdempotencyKey(
                    "confirm|\(confirmation.id)|\(confirmation.revision)|\(answer.rawValue)"
                ),
                explicitlyConfirmedOwner: true,
                confirmationRef: confirmation.id,
                expectedRevision: confirmation.revision,
                answer: answer,
                reason: reason
            )
            let result = try await service.submitOwnerAction(action)
            guard viewState.cancel(token) else { return }
            let accepted = result.status == "recorded" || result.status == "replayed"
            let receiptMatches = result.actionRef != nil
                && result.confirmationRef == confirmation.id
            let verificationMatches: Bool
            if answer == .occurred {
                verificationMatches = result.contextRef != nil
                    && result.verificationState == "verified"
            } else {
                verificationMatches = result.contextRef == nil
                    && result.verificationState == "non_verifying"
            }
            guard accepted, receiptMatches, verificationMatches else {
                confirmationMessage = "Core 回执不完整；没有把这次回答当成成功。"
                return
            }
            await refresh(runner: runner)
            confirmationMessage = answer == .occurred
                ? "已原子记录本人确认与可信上下文。"
                : "已记录回答；未生成可信上下文或长期记忆。"
        } catch {
            guard viewState.cancel(token) else { return }
            confirmationMessage = "确认失败或回执不满足安全契约。"
        }
    }

    private func loadMemoryCandidates(rootGeneration: UInt64) async -> Bool {
        guard refreshGeneration.isCurrent(rootGeneration) else { return false }
        let token = viewState.beginRequest(for: .memory)
        do {
            let result = try await service.loadMemoryCandidates()
            guard refreshGeneration.isCurrent(rootGeneration) else { return false }
            guard viewState.apply(memoryCandidates: result, token: token) else { return false }
            guard result.readState == .available, let candidates = result.candidates else {
                clearTrustedContent()
                presentationState = readPresentationState(
                    result.readState,
                    observedAt: result.observedAt
                )
                return false
            }
            memories = candidates.map {
                Phase2UIMemory(
                    id: $0.memoryCandidateRef,
                    safeSummary: $0.safeSummary,
                    project: $0.project.displayLabel,
                    state: memoryStateLabel($0),
                    source: sourceDisplayLabel($0.sourceLabel, verified: true),
                    expiresAt: $0.expiresAt
                )
            }
            return true
        } catch {
            guard refreshGeneration.isCurrent(rootGeneration) else { return false }
            guard viewState.applyFailure(
                .unavailable,
                observedAt: Self.isoTimestamp(),
                token: token
            ) else { return false }
            clearTrustedContent()
            presentationState = .unavailable(
                reason: "记忆候选不可读；旧候选已清空。",
                observedAt: Self.isoTimestamp()
            )
            return false
        }
    }

    private func loadProductExperimentStatus(rootGeneration: UInt64) async -> Bool {
        guard refreshGeneration.isCurrent(rootGeneration) else { return false }
        let token = viewState.beginRequest(for: .experiment)
        do {
            let result = try await service.loadProductExperimentStatus()
            guard refreshGeneration.isCurrent(rootGeneration) else { return false }
            guard viewState.apply(productExperimentStatus: result, token: token) else { return false }
            guard result.readState == .available,
                  result.phase != .unknown
            else {
                clearTrustedContent()
                presentationState = .unavailable(
                    reason: "实验状态未知；旧的可信内容已清空。",
                    observedAt: result.observedAt
                )
                return false
            }
            let stillRunning = experimentFeedbackBinding.applyExperimentStatus(result)
            if !stillRunning {
                resumeTimer.cancel()
                invalidateFeedbackActionAuthorization()
            }
            updateFeedbackAvailability()
            return true
        } catch {
            guard refreshGeneration.isCurrent(rootGeneration) else { return false }
            let observedAt = Self.isoTimestamp()
            guard viewState.applyFailure(
                .unavailable,
                observedAt: observedAt,
                token: token
            ) else { return false }
            clearTrustedContent()
            presentationState = .unavailable(
                reason: "实验状态不可读；旧的可信内容已清空。",
                observedAt: observedAt
            )
            // `clearTrustedContent()` has already revoked every presentation
            // capability and called `loseTrustPreservingSubmission()`. Do not
            // fail-close the feedback reducer here: a feedback request may
            // already be inside Core, transport-unknown, or recorded while its
            // post-receipt status checkpoint is pending. Losing the independent
            // experiment-status read must keep that exact idempotent checkpoint
            // globally closed until the old result/retry or a current status
            // response resolves it.
            updateFeedbackAvailability()
            return false
        }
    }

    private func loadDeliveryStatus(rootGeneration: UInt64) async -> Bool {
        guard refreshGeneration.isCurrent(rootGeneration) else { return false }
        let token = viewState.beginRequest(for: .delivery)
        do {
            let result = try await service.loadDeliveryStatus()
            guard refreshGeneration.isCurrent(rootGeneration) else { return false }
            guard viewState.apply(deliveryStatus: result, token: token) else { return false }
            guard result.readState == .available, let daily = result.dailyReport else {
                clearTrustedContent()
                presentationState = readPresentationState(
                    result.readState,
                    observedAt: result.observedAt
                )
                return false
            }
            let activeAttempts = (result.deliveries ?? []).filter { !$0.withdrawn }
            let preparedAt = daily.preparedAt.flatMap { timestamp in
                activeAttempts.contains(where: { $0.preparedAt == timestamp })
                    ? timestamp
                    : nil
            }
            let consumedAt = daily.consumedAt.flatMap { timestamp in
                activeAttempts.contains(where: { $0.consumedAt == timestamp })
                    ? timestamp
                    : nil
            }
            let deliveredAt = daily.deliveredAt.flatMap { timestamp in
                activeAttempts.contains(where: { $0.deliveredAt == timestamp })
                    ? timestamp
                    : nil
            }
            let failedAt = daily.failedAt.flatMap { timestamp in
                activeAttempts.contains(where: { $0.failedAt == timestamp })
                    ? timestamp
                    : nil
            }
            var items: [Phase2UIDelivery] = []
            if daily.needsInput {
                items.append(
                    Phase2UIDelivery(
                        id: "daily-report-needs-input",
                        project: "今日日报",
                        stage: .needsInput,
                        updatedAt: "\(daily.pendingConfirmationCount) 个问题待补充"
                    )
                )
            }
            items.append(
                Phase2UIDelivery(
                    id: "daily-report-prepared",
                    project: "今日日报 · 准备",
                    stage: preparedAt == nil ? .unknown : .prepared,
                    updatedAt: preparedAt ?? "尚无有效 PAM 准备回执"
                )
            )
            items.append(
                Phase2UIDelivery(
                    id: "daily-report-consumed",
                    project: "今日日报 · Hermes",
                    stage: consumedAt == nil ? .unknown : .consumed,
                    updatedAt: consumedAt ?? "尚无有效 Hermes 消费回执"
                )
            )
            let terminalStage: Phase2UIDeliveryStage
            let terminalTime: String
            if let failedAt {
                terminalStage = .failed
                terminalTime = failedAt
            } else if let deliveredAt {
                terminalStage = .delivered
                terminalTime = deliveredAt
            } else {
                terminalStage = .unknown
                terminalTime = "尚无飞书 Provider 送达或失败回执"
            }
            items.append(
                Phase2UIDelivery(
                    id: "daily-report-terminal",
                    project: "今日日报 · 飞书",
                    stage: terminalStage,
                    updatedAt: terminalTime
                )
            )
            items += (result.projects ?? []).map {
                Phase2UIDelivery(
                    id: $0.project.ref,
                    project: $0.project.displayLabel,
                    stage: $0.withdrawn ? .withdrawn : deliveryStage($0.stage),
                    updatedAt: $0.stageAt ?? result.observedAt
                )
            }
            deliveries = items
            return true
        } catch {
            guard refreshGeneration.isCurrent(rootGeneration) else { return false }
            guard viewState.applyFailure(
                .unavailable,
                observedAt: Self.isoTimestamp(),
                token: token
            ) else { return false }
            clearTrustedContent()
            presentationState = .unavailable(
                reason: "送达状态不可读；没有沿用旧回执。",
                observedAt: Self.isoTimestamp()
            )
            return false
        }
    }

    private func apply(snapshot: ContextSnapshotV1, asSearchResult: Bool) {
        presentationState = readPresentationState(
            snapshot.readState,
            observedAt: snapshot.observedAt
        )
        guard snapshot.readState == .available,
              let rawProjects = snapshot.projects,
              let legacy = snapshot.legacyConfirmed,
              let cards = snapshot.confirmationCards
        else {
            clearTrustedContent()
            return
        }
        let mapped = mapProjects(rawProjects, legacy: legacy)
        if asSearchResult {
            searchProjects = mapped
            selectedProjectID = mapped.first?.id
            // Search snapshots use a wider time/query scope. They must never
            // replace Today's confirmation queue or mutate write capability.
            return
        }
        todayProjects = Array(mapped.prefix(5))
        searchProjects = mapped
        confirmations = cards
            .filter(\.answerable)
            .map {
                Phase2UIConfirmation(
                    id: $0.confirmationRef,
                    revision: $0.revision,
                    project: $0.project?.displayLabel
                        ?? ($0.app.isEmpty ? "未归类项目" : $0.app),
                    title: $0.title,
                    question: $0.question,
                    evidenceLabel: $0.evidence,
                    answerOptions: Set($0.answerOptions)
                )
            }
        actionsEnabled = snapshot.actionsEnabled
            && snapshot.capabilities?.ownerAction == true
        feedbackCapabilityEnabled = snapshot.actionsEnabled
            && snapshot.capabilities?.productFeedback == true
        updateFeedbackAvailability()
        lastUpdatedAt = snapshot.observedAt
        message = ""
    }

    private func mapProjects(
        _ projects: [ContextProjectSnapshotV1],
        legacy: [LegacyConfirmedSummaryV1]
    ) -> [Phase2UIProject] {
        var results = projects.map { project in
            let verifiedItems = project.verifiedItems.map {
                Phase2UIContextItem(
                    id: $0.contextRef,
                    kind: verifiedStateLabel($0.state),
                    safeLabel: $0.safeLabel,
                    sourceLabel: sourceDisplayLabel($0.sourceLabel, verified: true),
                    occurredAt: $0.occurredAt
                )
            }
            let compatibilityItems = project.compatibilityItems.map {
                Phase2UIContextItem(
                    id: $0.contextRef,
                    kind: "已确认",
                    safeLabel: $0.safeLabel,
                    sourceLabel: sourceDisplayLabel($0.sourceLabel, verified: true),
                    occurredAt: $0.occurredAt
                )
            }
            return Phase2UIProject(
                id: project.project.ref,
                title: project.project.displayLabel,
                verifiedItems: verifiedItems,
                compatibilityItems: compatibilityItems,
                clues: project.clues.map {
                    Phase2UIClue(
                        id: $0.sourceRef,
                        safeLabel: $0.safeLabel,
                        sourceLabel: sourceDisplayLabel($0.sourceLabel, verified: false)
                    )
                },
                nextStep: project.nextStep?.safeLabel,
                handoffEligible: !project.verifiedItems.isEmpty,
                ownerActionEligible: !project.verifiedItems.isEmpty || !project.clues.isEmpty
            )
        }

        // Compatibility rows may also be returned at the top level for older
        // readers. Only add rows that were not already grouped by the Core.
        for (projectRef, entries) in Dictionary(grouping: legacy, by: \.project.ref) {
            let existingIDs = Set(
                results.first(where: { $0.id == projectRef })?.compatibilityItems.map(\.id) ?? []
            )
            let items = entries.filter { !existingIDs.contains($0.contextRef) }.map {
                Phase2UIContextItem(
                    id: $0.contextRef,
                    kind: "已确认",
                    safeLabel: $0.safeLabel,
                    sourceLabel: sourceDisplayLabel($0.sourceLabel, verified: true),
                    occurredAt: $0.occurredAt
                )
            }
            if let index = results.firstIndex(where: { $0.id == projectRef }) {
                let current = results[index]
                results[index] = Phase2UIProject(
                    id: current.id,
                    title: current.title,
                    verifiedItems: current.verifiedItems,
                    compatibilityItems: current.compatibilityItems + items,
                    clues: current.clues,
                    nextStep: current.nextStep,
                    handoffEligible: current.handoffEligible,
                    ownerActionEligible: current.ownerActionEligible
                )
            } else if let first = entries.first {
                results.append(
                    Phase2UIProject(
                        id: projectRef,
                        title: first.project.displayLabel,
                        verifiedItems: [],
                        compatibilityItems: items,
                        clues: [],
                        nextStep: nil,
                        handoffEligible: false,
                        ownerActionEligible: false
                    )
                )
            }
        }
        return results.sorted {
            if $0.verifiedItems.count == $1.verifiedItems.count {
                if $0.compatibilityItems.count == $1.compatibilityItems.count {
                    return $0.title.localizedStandardCompare($1.title) == .orderedAscending
                }
                return $0.compatibilityItems.count > $1.compatibilityItems.count
            }
            return $0.verifiedItems.count > $1.verifiedItems.count
        }
    }

    private func clearTrustedContent() {
        todayProjects = []
        searchProjects = []
        confirmations = []
        deliveries = []
        memories = []
        handoff = nil
        handoffMessage = ""
        selectedProjectID = nil
        actionsEnabled = false
        feedbackCapabilityEnabled = false
        experimentFeedbackBinding.loseTrustPreservingSubmission()
        resumeTimer.cancel()
        searchResultSelection.cancel()
        handoffRenderCheckpoint.cancel()
        invalidateFeedbackActionAuthorization()
        feedbackEnabled = false
        feedbackSubmitting = experimentFeedbackBinding.submissionInFlight
        searching = false
        buildingHandoff = false
        submittingConfirmationIDs = []
        feedbackMessage = ""
        confirmationMessage = ""
        message = ""
        searchProjectRef = ""
    }

    private func updateFeedbackAvailability() {
        feedbackEnabled = feedbackCapabilityEnabled
            && handoff != nil
            && feedbackActionAuthorization != nil
            && feedbackActionAuthorizationState.current == feedbackActionAuthorization
            && feedbackActionAuthorization?.matches(
                sourceRef: handoff?.sourceRef ?? "",
                projectRef: handoff?.projectRef ?? "",
                timerGeneration: resumeTimer.generation
            ) == true
            && experimentFeedbackBinding.canSubmit(sourceRef: handoff?.sourceRef)
            && resumeTimer.measurement?.sourceRef == handoff?.sourceRef
            && resumeTimer.measurement?.projectRef == handoff?.projectRef
    }

    private func bindFeedbackActionAuthorization(
        sourceRef: String,
        projectRef: String,
        timerGeneration: UInt64
    ) {
        feedbackActionAuthorization = feedbackActionAuthorizationState.bind(
            sourceRef: sourceRef,
            projectRef: projectRef,
            timerGeneration: timerGeneration
        )
    }

    private func invalidateFeedbackActionAuthorization() {
        feedbackActionAuthorizationState.invalidate()
        feedbackActionAuthorization = nil
    }

    private func readPresentationState(
        _ state: Phase2ReadState,
        observedAt: String
    ) -> Phase2UIPresentationState {
        switch state {
        case .available:
            return .available
        case .disabled:
            return .disabled
        case .unknown, .busy, .unavailable, .blocked:
            return .unavailable(
                reason: Phase2PresentationCopy.unavailableMessage(for: state),
                observedAt: observedAt
            )
        }
    }

    private func verifiedStateLabel(_ state: String) -> String {
        switch state {
        case "intent_observed": return "目标"
        case "in_progress": return "进展"
        case "draft_generated": return "草稿"
        case "artifact_saved", "artifact_committed": return "结果"
        case "external_delivered": return "已送达"
        case "completed_confirmed": return "已完成"
        case "blocked": return "阻塞"
        default: return "可信"
        }
    }

    private func memoryStateLabel(_ item: MemoryCandidateSummaryV1) -> String {
        switch item.state {
        case "candidate_only", "pending", "pending_review": return "待复核"
        case "active", "current": return "继续有效"
        case "historical", "superseded": return "历史记录"
        case "expired": return "已过期"
        case "wrong_subject", "quarantined", "wrong_subject_quarantined":
            return "错误主体隔离"
        case "withdrawn", "revoked":
            return "已撤回"
        default:
            return item.revoked ? "已撤回" : "待复核"
        }
    }

    private func sourceDisplayLabel(_ raw: String, verified: Bool) -> String {
        switch raw.lowercased() {
        case "user_confirmation", "legacy_user_confirmation", "manual_user_entry":
            return "本人确认"
        case "file_saved", "file_adapter":
            return "文件保存回执"
        case "git_commit", "git_adapter":
            return "Git 提交回执"
        case "ai_lifecycle":
            return "结构化 AI 事件"
        case "external_receipt", "external_adapter":
            return "外部完成回执"
        case "screen", "ocr", "screen_signal":
            return "屏幕补盲线索"
        default:
            if raw.unicodeScalars.contains(where: {
                (0x4E00...0x9FFF).contains(Int($0.value))
            }) {
                return raw
            }
            return verified ? "本机可信来源" : "待核实来源"
        }
    }

    private func deliveryStage(_ stage: DeliveryStage) -> Phase2UIDeliveryStage {
        switch stage {
        case .needsInput: return .needsInput
        case .prepared: return .prepared
        case .consumed: return .consumed
        case .delivered: return .delivered
        case .failed: return .failed
        }
    }

    static func makeIdempotencyKey(_ seed: String) -> String {
        "idem_" + SHA256.hash(data: Data(seed.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
    }

    private static func uptimeNanoseconds() -> UInt64 {
        DispatchTime.now().uptimeNanoseconds
    }

    private static func isoTimestamp(_ date: Date = Date()) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }
}

// MARK: - Main Phase 2 shell

struct Phase2ContextConsoleRoot: View {
    @ObservedObject var runner: CoreRunner
    @ObservedObject var windowState: AppWindowState
    @StateObject private var coordinator = Phase2ConsoleCoordinator()
    @State private var selection: Phase2Destination = .today
    @State private var quickCapturePresented = false
    @State private var expandedConfirmationID: String?
    @State private var findFocusGeneration = 0

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            detail
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(minWidth: 860, minHeight: 600)
        .background(Color(nsColor: .windowBackgroundColor))
        .task {
            LocalNotificationCoordinator.shared.activatePhase2()
            await coordinator.refresh(runner: runner)
            let requested = windowState.requestedPhase2Destination
            setDestination(
                requested,
                restartFind: false,
                requestFindFocus: requested == .find
            )
        }
        .onChange(of: windowState.quickCaptureRequestID) { _, _ in
            quickCapturePresented = true
        }
        .onChange(of: windowState.phase2DestinationRequestID) { _, _ in
            let requested = windowState.requestedPhase2Destination
            setDestination(
                requested,
                restartFind: requested == .find,
                requestFindFocus: requested == .find
            )
        }
        .onChange(of: runner.statusSnapshot.displayStateUnknown) { _, isUnknown in
            if isUnknown {
                coordinator.runtimeStatusBecameUnknown(
                    observedAt: runner.statusSnapshot.observedAt
                )
            }
        }
        .sheet(isPresented: $quickCapturePresented) {
            Phase2QuickCaptureSheet(runner: runner, coordinator: coordinator)
        }
        .background {
            Button("") {
                setDestination(
                    .find,
                    restartFind: true,
                    requestFindFocus: true
                )
            }
            .keyboardShortcut("k", modifiers: .command)
            .opacity(0)
            .accessibilityHidden(true)
        }
    }

    /// The only mutable route setter for the Phase 2 shell. In particular,
    /// leaving Find synchronously revokes its presentation and feedback
    /// capabilities before SwiftUI can publish the replacement destination.
    private func setDestination(
        _ destination: Phase2Destination,
        restartFind: Bool = false,
        requestFindFocus: Bool = false
    ) {
        let transition = Phase2DestinationTransition(
            current: selection,
            requested: destination,
            restartFind: restartFind
        )
        if transition.shouldCancelFindBeforeSelection {
            coordinator.cancelSearch()
        }
        if transition.shouldBeginFindFlow {
            coordinator.beginFindFlow()
        }
        selection = transition.destination
        if transition.shouldBeginFindFlow, requestFindFocus {
            findFocusGeneration += 1
        }
    }

    private var destinationBinding: Binding<Phase2Destination> {
        Binding(
            get: { selection },
            set: { destination in
                setDestination(destination)
            }
        )
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                LogoMark(size: 34)
                VStack(alignment: .leading, spacing: 1) {
                    Text("AGI")
                        .font(.headline.weight(.bold))
                    Text("个人上下文控制台")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 15)
            .padding(.vertical, 16)

            VStack(spacing: 4) {
                ForEach([Phase2Destination.today, .find, .memory]) { item in
                    navigationButton(item)
                }
            }
            .padding(.horizontal, 8)

            Spacer()

            healthSummary
                .padding(.horizontal, 12)
                .padding(.bottom, 10)

            Divider()

            navigationButton(.settings)
                .padding(8)
        }
        .frame(width: 196)
        .background(.ultraThinMaterial)
    }

    private func navigationButton(_ item: Phase2Destination) -> some View {
        Button {
            setDestination(
                item,
                restartFind: item == .find,
                requestFindFocus: item == .find
            )
        } label: {
            HStack(spacing: 9) {
                Image(systemName: item.systemImage)
                    .font(.system(size: 13, weight: .semibold))
                    .frame(width: 18)
                Text(item.title)
                    .font(.callout.weight(selection == item ? .semibold : .regular))
                Spacer(minLength: 4)
                if item == .today, !coordinator.confirmations.isEmpty {
                    Text("\(min(3, coordinator.confirmations.count))")
                        .font(.caption2.monospacedDigit().weight(.bold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Color.orange)
                        .clipShape(Capsule())
                }
            }
            .padding(.horizontal, 10)
            .frame(height: 34)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(selection == item ? Color.primary : Color.secondary)
        .background {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(selection == item ? Color.accentColor.opacity(0.13) : Color.clear)
        }
        .accessibilityValue(selection == item ? "已选中" : "")
    }

    @ViewBuilder
    private var healthSummary: some View {
        switch coordinator.presentationState {
        case .loading:
            Phase2HealthStrip(
                title: "正在读取",
                detail: "只读取本机安全上下文",
                tone: .muted,
                systemImage: "arrow.triangle.2.circlepath"
            )
        case .available:
            switch Phase2MenuHealth.resolve(runner.statusSnapshot) {
            case .normal:
                Phase2HealthStrip(
                    title: "正常",
                    detail: coordinator.lastUpdatedAt,
                    tone: .success,
                    systemImage: "checkmark.circle.fill"
                )
            case .paused:
                Phase2HealthStrip(
                    title: "暂停",
                    detail: coordinator.lastUpdatedAt,
                    tone: .attention,
                    systemImage: "pause.circle.fill"
                )
            case .error:
                Phase2HealthStrip(
                    title: "异常",
                    detail: coordinator.lastUpdatedAt,
                    tone: .danger,
                    systemImage: "exclamationmark.triangle.fill"
                )
            }
        case .disabled:
            Phase2HealthStrip(
                title: "未启用",
                detail: "可信上下文主链保持关闭",
                tone: .muted,
                systemImage: "minus.circle"
            )
        case let .unavailable(_, observedAt):
            Phase2HealthStrip(
                title: "异常",
                detail: observedAt,
                tone: .danger,
                systemImage: "exclamationmark.triangle.fill"
            )
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch selection {
        case .today:
            Phase2TodayView(
                runner: runner,
                coordinator: coordinator,
                expandedConfirmationID: $expandedConfirmationID,
                quickCapturePresented: $quickCapturePresented
            )
        case .find:
            Phase2FindView(
                runner: runner,
                coordinator: coordinator,
                focusGeneration: findFocusGeneration
            )
        case .memory:
            Phase2MemoryView(coordinator: coordinator)
        case .settings:
            Phase2UnifiedSettingsView(
                runner: runner,
                selection: destinationBinding
            )
        case .legacy:
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    Label("旧版界面 · 一个回滚周期内保留", systemImage: "clock.arrow.circlepath")
                        .font(.callout.weight(.semibold))
                    Spacer()
                    Button("返回新控制台") { setDestination(.today) }
                }
                .padding(.horizontal, 16)
                .frame(height: 42)
                .background(Color.orange.opacity(0.10))
                Divider()
                ContentView(runner: runner, windowState: windowState)
            }
        }
    }
}

// MARK: - Today

struct Phase2TodayView: View {
    @ObservedObject var runner: CoreRunner
    @ObservedObject var coordinator: Phase2ConsoleCoordinator
    @Binding var expandedConfirmationID: String?
    @Binding var quickCapturePresented: Bool
    @State private var inboxExpanded = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Phase2PageHeader(
                    eyebrow: "TODAY / VERIFIED CONTEXT",
                    title: "今天真正推进了什么",
                    subtitle: "只把有来源、可撤销的内容放进可信区。"
                ) {
                    Button {
                        quickCapturePresented = true
                    } label: {
                        Label("记一下", systemImage: "square.and.pencil")
                    }
                    .buttonStyle(.bordered)

                    Button {
                        Task { await coordinator.refresh(runner: runner) }
                    } label: {
                        Label("刷新", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(.borderedProminent)
                }

                stateNotice

                if coordinator.presentationState.isAvailable {
                    projectSection
                    confirmationSection
                    deliverySection
                }
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
    }

    @ViewBuilder
    private var stateNotice: some View {
        switch coordinator.presentationState {
        case .loading:
            Phase2EmptyPanel(
                systemImage: "arrow.triangle.2.circlepath",
                title: "正在读取可信上下文",
                detail: "读取期间不保留上一轮可信内容。"
            )
        case .disabled:
            Phase2EmptyPanel(
                systemImage: "lock.shield",
                title: "上下文主链尚未启用",
                detail: "功能开关保持关闭；旧界面仍可从设置的高级区进入。"
            )
        case let .unavailable(reason, observedAt):
            Phase2EmptyPanel(
                systemImage: "exclamationmark.triangle",
                title: "状态未知",
                detail: "\(reason) \(observedAt)"
            )
        case .available:
            EmptyView()
        }
    }

    private var projectSection: some View {
        Phase2Panel(title: "项目进展", count: coordinator.todayProjects.count) {
            if coordinator.todayProjects.isEmpty {
                Phase2InlineEmpty(
                    title: "还没有可展示的可信项目进展",
                    detail: "未验证线索不会被用来填满这里。"
                )
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(coordinator.todayProjects.prefix(5)).indices, id: \.self) { index in
                        Phase2ProjectRow(project: coordinator.todayProjects[index])
                        if index < min(5, coordinator.todayProjects.count) - 1 {
                            Divider().padding(.leading, 40)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var confirmationSection: some View {
        Phase2Panel(title: "需要你确认", count: coordinator.confirmations.count) {
            if coordinator.confirmations.isEmpty {
                Phase2InlineEmpty(
                    title: "目前没有高价值确认问题",
                    detail: "信息不足的候选留在内部质量指标，不会出现在这里。"
                )
            } else {
                VStack(spacing: 10) {
                    ForEach(coordinator.confirmations.prefix(3)) { confirmation in
                        confirmationCard(confirmation)
                    }
                    if coordinator.confirmations.count > 3 {
                        DisclosureGroup(isExpanded: $inboxExpanded) {
                            VStack(spacing: 10) {
                                ForEach(
                                    Array(coordinator.confirmations.dropFirst(3))
                                ) { confirmation in
                                    confirmationCard(confirmation)
                                }
                            }
                            .padding(.top, 8)
                        } label: {
                            Label(
                                "收件箱 · \(coordinator.confirmations.count - 3)",
                                systemImage: "tray"
                            )
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                        }
                        .padding(.horizontal, 2)
                    }
                }
                .padding(10)
            }
            if !coordinator.confirmationMessage.isEmpty {
                Text(coordinator.confirmationMessage)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
            }
        }
    }

    private func confirmationCard(
        _ confirmation: Phase2UIConfirmation
    ) -> some View {
        Phase2CompactConfirmationCard(
            confirmation: confirmation,
            isExpanded: Binding(
                get: { expandedConfirmationID == confirmation.id },
                set: { expandedConfirmationID = $0 ? confirmation.id : nil }
            ),
            isSubmitting: coordinator.submittingConfirmationIDs
                .contains(confirmation.id),
            actionsEnabled: coordinator.actionsEnabled,
            answer: { answer in
                Task {
                    await coordinator.submitConfirmation(
                        confirmation,
                        answer: answer,
                        runner: runner
                    )
                }
            }
        )
    }

    private var deliverySection: some View {
        Phase2Panel(title: "日报链路", count: coordinator.deliveries.count) {
            if coordinator.deliveries.isEmpty {
                Phase2InlineEmpty(
                    title: "暂无可核验送达状态",
                    detail: "没有回执时不会显示 Hermes 已消费或飞书已送达。"
                )
            } else {
                VStack(spacing: 0) {
                    ForEach(coordinator.deliveries) { delivery in
                        HStack(spacing: 11) {
                            Phase2ToneDot(tone: delivery.stage.tone)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(delivery.project)
                                    .font(.callout.weight(.semibold))
                                Text(delivery.updatedAt)
                                    .font(.caption2.monospacedDigit())
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Label(delivery.stage.title, systemImage: delivery.stage.systemImage)
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(delivery.stage.tone.color)
                        }
                        .padding(.horizontal, 12)
                        .frame(minHeight: 50)
                    }
                }
            }
        }
    }

}

struct Phase2ProjectRow: View {
    let project: Phase2UIProject

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            ZStack {
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(Color.accentColor.opacity(0.11))
                Image(systemName: "folder.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Color.accentColor)
            }
            .frame(width: 30, height: 30)

            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline) {
                    Text(project.title)
                        .font(.callout.weight(.bold))
                    Spacer(minLength: 8)
                    Text(
                        project.compatibilityItems.isEmpty
                            ? "\(project.verifiedItems.count) 条可信"
                            : "\(project.verifiedItems.count) 条可信 · \(project.compatibilityItems.count) 条历史确认"
                    )
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                }

                ForEach(project.verifiedItems.prefix(2)) { item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(item.kind)
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: 42, alignment: .leading)
                        Text(item.safeLabel)
                            .font(.caption)
                            .lineLimit(2)
                        Spacer(minLength: 6)
                        Text(item.sourceLabel)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }

                if let item = project.compatibilityItems.first {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("历史")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: 42, alignment: .leading)
                        Text(item.safeLabel)
                            .font(.caption)
                            .lineLimit(2)
                        Spacer(minLength: 6)
                        Text("仅本机兼容")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }

                HStack(spacing: 6) {
                    Text("下一步")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Color.accentColor)
                    Text(project.nextStep ?? "缺少可信下一步")
                        .font(.caption)
                        .foregroundStyle(project.nextStep == nil ? .secondary : .primary)
                }
            }
        }
        .padding(11)
    }
}

struct Phase2CompactConfirmationCard: View {
    let confirmation: Phase2UIConfirmation
    @Binding var isExpanded: Bool
    let isSubmitting: Bool
    let actionsEnabled: Bool
    let answer: (OwnerConfirmationAnswer) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .top, spacing: 10) {
                Phase2ToneDot(tone: .attention)
                    .padding(.top, 4)
                VStack(alignment: .leading, spacing: 4) {
                    Text(confirmation.title.isEmpty ? confirmation.project : confirmation.title)
                        .font(.callout.weight(.bold))
                    Text(confirmation.question)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) {
                        Label(
                            "待本人确认",
                            systemImage: "checkmark.shield"
                        )
                        if !confirmation.project.isEmpty {
                            Text(confirmation.project)
                        }
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }
                Spacer(minLength: 10)
                Button(isExpanded ? "收起" : "回答") {
                    isExpanded.toggle()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }

            if isExpanded {
                Divider()
                if !confirmation.evidenceLabel.isEmpty {
                    Text(confirmation.evidenceLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 8) {
                    confirmationButton(
                        "是，已发生",
                        systemImage: "checkmark",
                        answer: .occurred,
                        prominent: true
                    )
                    confirmationButton(
                        "未发生",
                        systemImage: "xmark",
                        answer: .notOccurred
                    )
                    confirmationButton(
                        "稍后",
                        systemImage: "clock",
                        answer: .deferred
                    )
                    Spacer()
                    confirmationButton(
                        "忽略",
                        systemImage: "eye.slash",
                        answer: .ignored
                    )
                }
            }
        }
        .padding(11)
        .background(Color(nsColor: .controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Color.primary.opacity(0.07))
        }
    }

    @ViewBuilder
    private func confirmationButton(
        _ title: String,
        systemImage: String,
        answer selectedAnswer: OwnerConfirmationAnswer,
        prominent: Bool = false
    ) -> some View {
        if prominent {
            confirmationButtonBase(
                title,
                systemImage: systemImage,
                answer: selectedAnswer
            )
            .buttonStyle(.borderedProminent)
        } else {
            confirmationButtonBase(
                title,
                systemImage: systemImage,
                answer: selectedAnswer
            )
            .buttonStyle(.bordered)
        }
    }

    private func confirmationButtonBase(
        _ title: String,
        systemImage: String,
        answer selectedAnswer: OwnerConfirmationAnswer
    ) -> some View {
        Button {
            answer(selectedAnswer)
        } label: {
            if isSubmitting {
                ProgressView().controlSize(.small)
            } else {
                Label(title, systemImage: systemImage)
            }
        }
        .controlSize(.small)
        .disabled(
            isSubmitting
                || !actionsEnabled
                || !confirmation.answerOptions.contains(selectedAnswer.rawValue)
        )
    }
}

// MARK: - Find

struct Phase2FindView: View {
    @ObservedObject var runner: CoreRunner
    @ObservedObject var coordinator: Phase2ConsoleCoordinator
    let focusGeneration: Int
    @FocusState private var queryFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 12) {
                Phase2PageHeader(
                    eyebrow: "FIND / LOCAL ONLY",
                    title: "找回项目上下文",
                    subtitle: "本地全文检索 · 项目聚合 · 可信与线索分区"
                ) {
                    EmptyView()
                }

                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(.secondary)
                    TextField("项目、结果、下一步…", text: $coordinator.searchQuery)
                        .textFieldStyle(.plain)
                        .focused($queryFocused)
                        .onSubmit {
                            Task { await coordinator.search(runner: runner) }
                        }
                    Text("⌘K")
                        .font(.caption2.monospaced().weight(.semibold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 3)
                        .background(Color.primary.opacity(0.05))
                        .clipShape(RoundedRectangle(cornerRadius: 4))
                    Button {
                        Task { await coordinator.search(runner: runner) }
                    } label: {
                        if coordinator.searching {
                            ProgressView()
                                .controlSize(.small)
                        } else {
                            Text("搜索")
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!coordinator.presentationState.isAvailable || coordinator.searching)
                }
                .padding(.horizontal, 11)
                .frame(height: 40)
                .background(Color(nsColor: .textBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 9, style: .continuous)
                        .stroke(queryFocused ? Color.accentColor.opacity(0.7) : Color.primary.opacity(0.08))
                }

                HStack(spacing: 10) {
                    Picker("项目", selection: $coordinator.searchProjectRef) {
                        Text("全部项目").tag("")
                        ForEach(searchFilterProjects) { project in
                            Text(project.title).tag(project.id)
                        }
                    }
                    .frame(maxWidth: 280)

                    Picker("时间", selection: $coordinator.searchTimePreset) {
                        ForEach(Phase2FindTimePreset.allCases) { preset in
                            Text(preset.title).tag(preset)
                        }
                    }
                    .frame(width: 170)

                    Spacer()
                    Text("筛选条件与正文仅通过 JSON stdin 发送")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(18)

            Divider()

            HSplitView {
                resultList
                    .frame(minWidth: 300, idealWidth: 390)
                handoffPreview
                    .frame(minWidth: 360, idealWidth: 520)
            }
        }
        .onAppear { queryFocused = true }
        .onChange(of: focusGeneration) { _, _ in
            DispatchQueue.main.async { queryFocused = true }
        }
        .onDisappear {
            coordinator.cancelSearch()
            queryFocused = false
        }
    }

    private var searchFilterProjects: [Phase2UIProject] {
        var projectsByID: [String: Phase2UIProject] = [:]
        for project in coordinator.todayProjects + coordinator.searchProjects
        where project.ownerActionEligible || !project.compatibilityItems.isEmpty {
            projectsByID[project.id] = project
        }
        return projectsByID.values.sorted {
            $0.title.localizedStandardCompare($1.title) == .orderedAscending
        }
    }

    private var resultList: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 8) {
                if !coordinator.message.isEmpty {
                    Phase2EmptyPanel(
                        systemImage: "lock.shield",
                        title: "没有使用不安全回退",
                        detail: coordinator.message
                    )
                } else if coordinator.searchProjects.isEmpty {
                    Phase2EmptyPanel(
                        systemImage: "magnifyingglass",
                        title: "输入关键词开始找回",
                        detail: "不会展示原始 JSON、内部 ID、重复帧或工程诊断字段。"
                    )
                } else {
                    ForEach(coordinator.searchProjects) { project in
                        Button {
                            Task { await coordinator.selectProject(project) }
                        } label: {
                            Phase2FindProjectCard(
                                project: project,
                                selected: coordinator.selectedProjectID == project.id
                            )
                        }
                        .buttonStyle(.plain)
                        .disabled(coordinator.searching)
                        .help(
                            project.handoffEligible
                                ? "生成只含可信内容的续接包"
                                : "查看本机历史确认或待核实线索；不能生成可信续接包"
                        )
                    }
                }
            }
            .padding(12)
        }
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.45))
    }

    private var handoffPreview: some View {
        ScrollView {
            if let handoff = coordinator.handoff {
                Phase2HandoffPreview(
                    handoff: handoff,
                    feedbackMessage: coordinator.feedbackMessage,
                    feedbackEnabled: coordinator.feedbackEnabled && !coordinator.feedbackSubmitting,
                    feedbackActionAuthorization: coordinator.feedbackActionAuthorization,
                    markUseful: { expectedAuthorization in
                        Task {
                            await coordinator.submitHandoffFeedback(
                                choice: .useful,
                                expectedAuthorization: expectedAuthorization
                            )
                        }
                    },
                    markNotUseful: { expectedAuthorization in
                        Task {
                            await coordinator.submitHandoffFeedback(
                                choice: .notUseful,
                                expectedAuthorization: expectedAuthorization
                            )
                        }
                    },
                    becameVisible: {
                        guard let renderToken = handoff.renderToken else { return }
                        coordinator.handoffBecameVisible(renderToken: renderToken)
                    }
                )
                    .id(handoff.renderIdentity)
                    .padding(16)
            } else if coordinator.buildingHandoff {
                Phase2EmptyPanel(
                    systemImage: "arrow.triangle.2.circlepath",
                    title: "正在生成续接包",
                    detail: "只读取这个项目仍然有效的可信上下文。"
                )
                .padding(16)
            } else if !coordinator.handoffMessage.isEmpty {
                Phase2EmptyPanel(
                    systemImage: "clock.arrow.circlepath",
                    title: "当前不能生成续接包",
                    detail: coordinator.handoffMessage
                )
                .padding(16)
            } else {
                Phase2EmptyPanel(
                    systemImage: "arrow.right.doc.on.clipboard",
                    title: "选择一个项目生成续接包",
                    detail: "续接包只采用可信内容；未验证线索仅作为未决问题。"
                )
                .padding(16)
            }
        }
    }
}

struct Phase2FindProjectCard: View {
    let project: Phase2UIProject
    let selected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(project.title)
                    .font(.callout.weight(.bold))
                Spacer()
                Text("\(project.verifiedItems.count)")
                    .font(.caption.monospacedDigit().weight(.semibold))
                    .foregroundStyle(.secondary)
            }

            if let first = project.verifiedItems.first {
                Label(first.safeLabel, systemImage: "checkmark.seal.fill")
                    .font(.caption)
                    .foregroundStyle(.primary)
                    .lineLimit(2)
            } else if let first = project.compatibilityItems.first {
                Label(first.safeLabel, systemImage: "clock.arrow.circlepath")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }

            if let clue = project.clues.first {
                VStack(alignment: .leading, spacing: 3) {
                    Label("待核实线索", systemImage: "questionmark.diamond.fill")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Color.orange)
                    Text(clue.safeLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    Text(clue.sourceLabel)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .padding(7)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.orange.opacity(0.07))
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            }

            HStack(spacing: 10) {
                Label(
                    "\(project.verifiedItems.count) 可信",
                    systemImage: "checkmark.shield"
                )
                .foregroundStyle(project.verifiedItems.isEmpty ? Color.secondary : Color.green)
                if !project.compatibilityItems.isEmpty {
                    Label(
                        "\(project.compatibilityItems.count) 历史确认（仅本机兼容）",
                        systemImage: "clock.arrow.circlepath"
                    )
                    .foregroundStyle(Color.secondary)
                }
                if !project.clues.isEmpty {
                    Label("\(project.clues.count) 线索", systemImage: "questionmark.diamond")
                        .foregroundStyle(Color.orange)
                }
            }
            .font(.caption2.weight(.semibold))
        }
        .padding(11)
        .background(selected ? Color.accentColor.opacity(0.12) : Color(nsColor: .textBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .stroke(selected ? Color.accentColor.opacity(0.55) : Color.primary.opacity(0.06))
        }
    }
}

struct Phase2HandoffPreview: View {
    let handoff: Phase2UIHandoff
    let feedbackMessage: String
    let feedbackEnabled: Bool
    let feedbackActionAuthorization: Phase2FeedbackActionAuthorization?
    let markUseful: (Phase2FeedbackActionAuthorization) -> Void
    let markNotUseful: (Phase2FeedbackActionAuthorization) -> Void
    let becameVisible: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("续接包")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(Color.accentColor)
                    Text(handoff.project)
                        .font(.title3.weight(.bold))
                }
                Spacer()
                Button {
                    let text = renderedText
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(text, forType: .string)
                } label: {
                    Label("复制", systemImage: "doc.on.doc")
                }
                .buttonStyle(.bordered)
                Button(Phase2ProductFeedbackChoice.notUseful.title) {
                    guard let feedbackActionAuthorization else { return }
                    markNotUseful(feedbackActionAuthorization)
                }
                    .buttonStyle(.bordered)
                    .disabled(!feedbackEnabled || feedbackActionAuthorization == nil)
                Button(Phase2ProductFeedbackChoice.useful.title) {
                    guard let feedbackActionAuthorization else { return }
                    markUseful(feedbackActionAuthorization)
                }
                    .buttonStyle(.borderedProminent)
                    .disabled(!feedbackEnabled || feedbackActionAuthorization == nil)
            }

            Phase2HandoffSection(title: "目标", items: [handoff.objective], tone: .accent)
            Phase2HandoffSection(title: "最近已验证结果", items: handoff.results, tone: .success)
            Phase2HandoffSection(title: "阻塞", items: handoff.blockers, tone: .danger)
            Phase2HandoffSection(title: "下一步", items: handoff.nextSteps, tone: .attention)
            Phase2HandoffSection(title: "来源", items: handoff.sources, tone: .muted)
            Phase2HandoffSection(title: "未决问题", items: handoff.unresolvedQuestions, tone: .muted)

            Text("仅复制到本机剪贴板，不会自动发送给 AI。")
                .font(.caption2)
                .foregroundStyle(.tertiary)
            if !feedbackMessage.isEmpty {
                Label(feedbackMessage, systemImage: "checkmark.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .onAppear(perform: becameVisible)
    }

    private var renderedText: String {
        var lines = ["# \(handoff.project)", "", "目标：\(handoff.objective)"]
        func append(_ title: String, _ items: [String]) {
            guard !items.isEmpty else { return }
            lines += ["", "## \(title)"]
            lines += items.map { "- \($0)" }
        }
        append("最近已验证结果", handoff.results)
        append("阻塞", handoff.blockers)
        append("下一步", handoff.nextSteps)
        append("来源", handoff.sources)
        append("未决问题", handoff.unresolvedQuestions)
        return lines.joined(separator: "\n")
    }
}

struct Phase2HandoffSection: View {
    let title: String
    let items: [String]
    let tone: Phase2Tone

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Phase2ToneDot(tone: tone)
                Text(title)
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
            }
            if items.isEmpty {
                Text("无可信内容")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    Text(item)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.color.opacity(0.055))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

// MARK: - Memory

struct Phase2MemoryView: View {
    @ObservedObject var coordinator: Phase2ConsoleCoordinator

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Phase2PageHeader(
                    eyebrow: "MEMORY / GOVERNED CANDIDATES",
                    title: "哪些内容以后还可以让 AI 相信",
                    subtitle: "安全摘要优先；PAM 只生成候选，不直接写长期记忆。"
                ) {
                    EmptyView()
                }

                Phase2Panel(title: "记忆候选", count: coordinator.memories.count) {
                    if coordinator.memories.isEmpty {
                        Phase2InlineEmpty(
                            title: "没有可展示的记忆候选",
                            detail: "指纹和原因码不会代替安全摘要成为标题。"
                        )
                    } else {
                        VStack(spacing: 0) {
                            ForEach(Array(coordinator.memories.enumerated()), id: \.element.id) { index, memory in
                                Phase2MemoryRow(memory: memory)
                                if index < coordinator.memories.count - 1 {
                                    Divider().padding(.leading, 42)
                                }
                            }
                        }
                    }
                }

                Phase2Panel(title: "治理边界") {
                    Phase2BoundaryRow(
                        systemImage: "lock.shield.fill",
                        title: "Memory Gateway 是唯一写入口",
                        detail: "写入、替代、删除和撤回都必须返回可核验回执。",
                        tone: .success
                    )
                    Divider().padding(.leading, 42)
                    Phase2BoundaryRow(
                        systemImage: "nosign",
                        title: "自动长期记忆写入保持关闭",
                        detail: "候选未复核、已过期或被撤回时，不提供直接写入操作。",
                        tone: .attention
                    )
                }
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
    }
}

struct Phase2MemoryRow: View {
    let memory: Phase2UIMemory
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 11) {
                Image(systemName: "brain.head.profile")
                    .foregroundStyle(Color.accentColor)
                    .frame(width: 30, height: 30)
                    .background(Color.accentColor.opacity(0.09))
                    .clipShape(RoundedRectangle(cornerRadius: 7))
                VStack(alignment: .leading, spacing: 4) {
                    Text(memory.safeSummary)
                        .font(.callout.weight(.semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) {
                        Text(memory.project)
                        Text(memory.source)
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }
                Spacer()
                Phase2Badge(text: memory.state, tone: memoryTone(memory.state))
                Button {
                    expanded.toggle()
                } label: {
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                }
                .buttonStyle(.plain)
                .accessibilityLabel(expanded ? "收起详情" : "展开详情")
            }

            if expanded {
                HStack(spacing: 18) {
                    Label(
                        memory.expiresAt.isEmpty ? "有效期未返回" : "有效至 \(memory.expiresAt)",
                        systemImage: "calendar.badge.clock"
                    )
                    Label("只读候选", systemImage: "lock")
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.leading, 41)
            }
        }
        .padding(11)
    }

    private func memoryTone(_ state: String) -> Phase2Tone {
        switch state {
        case "继续有效": return .success
        case "待复核": return .attention
        case "已撤回", "错误主体隔离": return .danger
        default: return .muted
        }
    }
}

// MARK: - Unified settings

struct Phase2UnifiedSettingsView: View {
    @ObservedObject var runner: CoreRunner
    @Binding var selection: Phase2Destination
    @State private var advancedExpanded = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Phase2PageHeader(
                    eyebrow: "SETTINGS / LOCAL-FIRST",
                    title: "设置与健康状态",
                    subtitle: "隐私、数据、健康和普通配置集中在这里。"
                ) {
                    EmptyView()
                }

                Phase2Panel(title: "上下文控制台") {
                    HStack(spacing: 11) {
                        Image(systemName: "rectangle.3.group")
                            .foregroundStyle(Color.accentColor)
                            .frame(width: 30)
                        VStack(alignment: .leading, spacing: 3) {
                            Text("使用“今天、找回、记忆”新界面")
                                .font(.callout.weight(.semibold))
                            Text("本轮稳定维护不开放启用；数据与旧界面保持不变。")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text("v0.5.1 维护锁定")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                    }
                    .padding(11)
                }

                Phase2Panel(title: "隐私与数据") {
                    Phase2BoundaryRow(
                        systemImage: "lock.shield",
                        title: "本地优先",
                        detail: "原始截图、OCR 和聊天正文不外发；屏幕只作补盲线索。",
                        tone: .success
                    )
                    Divider().padding(.leading, 42)
                    Phase2BoundaryRow(
                        systemImage: "checkmark.seal",
                        title: "可信与线索分区",
                        detail: "未验证内容不进入续接包、日报可信区或长期记忆。",
                        tone: .accent
                    )
                }

                Phase2Panel(title: "系统健康") {
                    systemHealthRow
                    Divider().padding(.leading, 42)
                    HStack {
                        Button {
                            Task { await runner.refreshStatus() }
                        } label: {
                            Label("刷新状态", systemImage: "arrow.clockwise")
                        }
                        .buttonStyle(.bordered)
                        Spacer()
                        Text("异常提醒会去重；普通确认和日报不通知。")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    .padding(11)
                }

                Phase2Panel(title: "高级") {
                    DisclosureGroup(isExpanded: $advancedExpanded) {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("旧漏斗、校准和治理审计只在旧版界面中保留一个回滚周期。")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Button {
                                selection = .legacy
                            } label: {
                                Label("打开旧版界面", systemImage: "clock.arrow.circlepath")
                            }
                            .buttonStyle(.bordered)
                        }
                        .padding(.top, 10)
                    } label: {
                        Label("诊断与旧版界面", systemImage: "wrench.and.screwdriver")
                            .font(.callout.weight(.semibold))
                    }
                    .padding(11)
                }
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
    }

    @ViewBuilder
    private var systemHealthRow: some View {
        if runner.statusSnapshot.displayStateUnknown {
            Phase2BoundaryRow(
                systemImage: "exclamationmark.triangle.fill",
                title: "状态未知",
                detail: runner.statusSnapshot.observationTimeLabel,
                tone: .danger
            )
        } else {
            switch Phase2MenuHealth.resolve(runner.statusSnapshot) {
            case .normal:
                Phase2BoundaryRow(
                    systemImage: "checkmark.circle.fill",
                    title: runner.statusSnapshot.statusLabelCN,
                    detail: runner.statusUpdatedLabel,
                    tone: .success
                )
            case .paused:
                Phase2BoundaryRow(
                    systemImage: "pause.circle.fill",
                    title: "暂停",
                    detail: runner.statusUpdatedLabel,
                    tone: .attention
                )
            case .error:
                Phase2BoundaryRow(
                    systemImage: "exclamationmark.triangle.fill",
                    title: "异常 · \(runner.statusSnapshot.statusLabelCN)",
                    detail: runner.statusUpdatedLabel,
                    tone: .danger
                )
            }
        }
    }
}

// MARK: - Quick capture

struct Phase2QuickCaptureSheet: View {
    @ObservedObject var runner: CoreRunner
    @ObservedObject var coordinator: Phase2ConsoleCoordinator
    @Environment(\.dismiss) private var dismiss
    @State private var summary = ""
    @State private var selectedProjectID = ""
    @State private var outcome = ""
    @State private var explicitlyConfirmedOwner = false
    @State private var saving = false
    @State private var errorMessage = ""
    @State private var idempotencyKey = Phase2ConsoleCoordinator.makeIdempotencyKey(
        "quick|\(UUID().uuidString)"
    )
    @FocusState private var summaryFocused: Bool

    private var eligibleProjects: [Phase2UIProject] {
        coordinator.todayProjects.filter(\.ownerActionEligible)
    }

    private var selectedProject: Phase2UIProject? {
        eligibleProjects.first { $0.id == selectedProjectID }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("记一下")
                        .font(.title2.weight(.bold))
                    Text("只记录你明确陈述的本机事实；可直接使用 macOS 听写。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("关闭")
            }

            TextEditor(text: $summary)
                .font(.body)
                .scrollContentBackground(.hidden)
                .padding(8)
                .frame(minHeight: 92)
                .background(Color(nsColor: .textBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 9, style: .continuous)
                        .stroke(Color.primary.opacity(0.10))
                }
                .focused($summaryFocused)
                .onChange(of: summary) { _, value in
                    summary = String(value.prefix(QuickCaptureDraft.maximumTextLength))
                    errorMessage = ""
                }
                .accessibilityLabel("补记内容")

            HStack(spacing: 10) {
                Picker("项目", selection: $selectedProjectID) {
                    Text("未归类（本机）").tag("")
                    ForEach(eligibleProjects) { project in
                        Text(project.title).tag(project.id)
                    }
                }
                .frame(maxWidth: .infinity)
                Picker("结果", selection: $outcome) {
                    Text("选择结果").tag("")
                    ForEach(ActivityConfirmationBusinessOutcome.allCases) { item in
                        Text(item.label).tag(item.rawValue)
                    }
                }
                .frame(width: 190)
            }

            if eligibleProjects.isEmpty {
                Label(
                    "当前没有已有项目；这条补记会进入“未归类”，之后可复核归属。",
                    systemImage: "tray"
                )
                .font(.caption)
                .foregroundStyle(.orange)
            }

            Toggle(isOn: $explicitlyConfirmedOwner) {
                Text("这是我本人明确确认的事实")
                    .font(.callout.weight(.semibold))
            }

            Text("普通补记默认不具备长期记忆写入权限。")
                .font(.caption2)
                .foregroundStyle(.secondary)

            if !errorMessage.isEmpty {
                Label(errorMessage, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            } else if !coordinator.quickCaptureMessage.isEmpty {
                Text(coordinator.quickCaptureMessage)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            HStack {
                Button("取消") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button {
                    save()
                } label: {
                    if saving {
                        ProgressView().controlSize(.small)
                    } else {
                        Text("保存为已确认事实")
                    }
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!submissionReady || saving)
            }
        }
        .padding(20)
        .frame(width: 540)
        .onAppear {
            if selectedProjectID.isEmpty {
                selectedProjectID = eligibleProjects.first?.id ?? ""
            }
            summaryFocused = true
        }
    }

    private var submissionReady: Bool {
        QuickCaptureDraft(
            text: summary,
            explicitlyConfirmedOwner: explicitlyConfirmedOwner
        ).isSubmissionReady
            && !outcome.isEmpty
            && coordinator.actionsEnabled
    }

    private func save() {
        guard submissionReady else {
            errorMessage = "请填写内容和结果，并确认这是你本人明确陈述的事实。"
            return
        }
        saving = true
        Task {
            let succeeded = await coordinator.submitQuickCapture(
                idempotencyKey: idempotencyKey,
                summary: summary.trimmingCharacters(in: .whitespacesAndNewlines),
                outcome: outcome,
                project: selectedProject
            )
            saving = false
            if succeeded {
                await coordinator.refresh(runner: runner)
                dismiss()
            } else {
                errorMessage = coordinator.quickCaptureMessage
            }
        }
    }
}

// MARK: - Shared visual language

enum Phase2Tone {
    case accent
    case success
    case attention
    case danger
    case muted

    var color: Color {
        switch self {
        case .accent: return .cyan
        case .success: return .green
        case .attention: return .orange
        case .danger: return .red
        case .muted: return .secondary
        }
    }
}

struct Phase2PageHeader<Actions: View>: View {
    let eyebrow: String
    let title: String
    let subtitle: String
    @ViewBuilder let actions: Actions

    init(
        eyebrow: String,
        title: String,
        subtitle: String,
        @ViewBuilder actions: () -> Actions
    ) {
        self.eyebrow = eyebrow
        self.title = title
        self.subtitle = subtitle
        self.actions = actions()
    }

    var body: some View {
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text(eyebrow)
                    .font(.caption2.monospaced().weight(.bold))
                    .tracking(0.7)
                    .foregroundStyle(Color.accentColor)
                Text(title)
                    .font(.system(.title2, design: .rounded).weight(.bold))
                Text(subtitle)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 12)
            HStack(spacing: 8) {
                actions
            }
        }
        .accessibilityElement(children: .contain)
    }
}

struct Phase2Panel<Content: View>: View {
    let title: String
    let count: Int?
    @ViewBuilder let content: Content

    init(
        title: String,
        count: Int? = nil,
        @ViewBuilder content: () -> Content
    ) {
        self.title = title
        self.count = count
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(title)
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
                Spacer()
                if let count {
                    Text("\(count)")
                        .font(.caption2.monospacedDigit().weight(.semibold))
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 12)
            .frame(height: 34)
            .background(Color.primary.opacity(0.025))

            Divider()
            content
        }
        .background(Color(nsColor: .textBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Color.primary.opacity(0.075))
        }
    }
}

struct Phase2HealthStrip: View {
    let title: String
    let detail: String
    let tone: Phase2Tone
    let systemImage: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: systemImage)
                .foregroundStyle(tone.color)
            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                    .font(.caption.weight(.semibold))
                Text(detail)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(9)
        .background(tone.color.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

struct Phase2EmptyPanel: View {
    let systemImage: String
    let title: String
    let detail: String

    var body: some View {
        VStack(spacing: 9) {
            Image(systemName: systemImage)
                .font(.system(size: 24, weight: .medium))
                .foregroundStyle(.secondary)
            Text(title)
                .font(.headline)
            Text(detail)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity)
        .padding(26)
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.65))
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

struct Phase2InlineEmpty: View {
    let title: String
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(.callout.weight(.semibold))
            Text(detail)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct Phase2BoundaryRow: View {
    let systemImage: String
    let title: String
    let detail: String
    let tone: Phase2Tone

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: systemImage)
                .foregroundStyle(tone.color)
                .frame(width: 30, height: 30)
                .background(tone.color.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 7))
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.callout.weight(.semibold))
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
        }
        .padding(11)
    }
}

struct Phase2Badge: View {
    let text: String
    let tone: Phase2Tone

    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(tone.color)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(tone.color.opacity(0.10))
            .clipShape(Capsule())
    }
}

struct Phase2ToneDot: View {
    let tone: Phase2Tone

    var body: some View {
        Circle()
            .fill(tone.color)
            .frame(width: 8, height: 8)
            .overlay {
                Circle().stroke(tone.color.opacity(0.25), lineWidth: 4)
            }
            .accessibilityHidden(true)
    }
}
