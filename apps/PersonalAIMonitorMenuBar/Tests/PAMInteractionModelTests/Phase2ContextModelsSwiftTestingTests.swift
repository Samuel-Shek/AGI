import Foundation

#if canImport(Testing)
import Testing
@testable import PAMInteractionModel

private let phase2RefA = String(repeating: "a", count: 32)
private let phase2RefB = String(repeating: "b", count: 32)
private let phase2RefC = String(repeating: "c", count: 32)

@Test("Pending-card pagination rejects a page after any refresh or mutation generation change")
func activityConfirmationGenerationFenceRejectsStalePage() {
    let fence = ActivityConfirmationGenerationFence(
        refreshGeneration: 12,
        mutationGeneration: 30
    )

    #expect(fence.allows(currentRefreshGeneration: 12, currentMutationGeneration: 30))
    #expect(fence.allows(currentRefreshGeneration: 13, currentMutationGeneration: 30) == false)
    #expect(fence.allows(currentRefreshGeneration: 12, currentMutationGeneration: 31) == false)
}

@Test("Phase 2 UI flag is off by default and Command-K routes to Find")
func phase2FlagAndRoute() {
    let suite = "PAMInteractionModelSwiftTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defer { defaults.removePersistentDomain(forName: suite) }
    #expect(ContextConsolePhase2FeatureFlag.isEnabled(in: defaults) == false)

    var route = Phase2RouteState(destination: .today)
    route.focusFind()
    #expect(route.destination == .find)
    #expect(route.searchFocusGeneration == 1)
    route.select(.legacy)
    #expect(route.destination == .legacy)
    route.select(.today)
    #expect(route.destination == .today)

    let enterFind = Phase2DestinationTransition(
        current: .today,
        requested: .find
    )
    #expect(enterFind.shouldBeginFindFlow)
    #expect(enterFind.shouldCancelFindBeforeSelection == false)
    let stayInFind = Phase2DestinationTransition(
        current: .find,
        requested: .find
    )
    #expect(stayInFind.shouldBeginFindFlow == false)
    #expect(stayInFind.shouldCancelFindBeforeSelection == false)
    let restartFind = Phase2DestinationTransition(
        current: .find,
        requested: .find,
        restartFind: true
    )
    #expect(restartFind.shouldBeginFindFlow)
    #expect(restartFind.shouldCancelFindBeforeSelection == false)
    for destination in [
        Phase2Destination.today,
        .memory,
        .settings,
        .legacy,
    ] {
        let leaveFind = Phase2DestinationTransition(
            current: .find,
            requested: destination
        )
        #expect(leaveFind.shouldCancelFindBeforeSelection)
        #expect(leaveFind.shouldBeginFindFlow == false)
        #expect(leaveFind.destination == destination)
    }
}

@Test("Quick capture requires owner confirmation and rejects private residue")
func phase2QuickCaptureSafety() {
    #expect(
        QuickCaptureDraft(
            text: "完成 Phase 2",
            explicitlyConfirmedOwner: true
        ).isSubmissionReady
    )
    #expect(
        QuickCaptureDraft(
            text: "token=abcdefghijklmnopqrstuvwxyz",
            explicitlyConfirmedOwner: true
        ).isSubmissionReady == false
    )
    #expect(
        QuickCaptureDraft(
            text: "查看 /" + "Users/example/private.txt",
            explicitlyConfirmedOwner: true
        ).isSubmissionReady == false
    )
    #expect(
        QuickCaptureDraft(
            text: String(repeating: "字", count: 181),
            explicitlyConfirmedOwner: true
        ).isSubmissionReady == false
    )
}

@Test("Core JSON boundary parses stdout only")
func phase2StdoutOnlyBoundary() throws {
    let stdout = try phase2JSON(["status": "available"])
    let result = CoreCommandResult(
        exitCode: 0,
        standardOutput: stdout,
        standardError: Data(#"{"status":"private"}"#.utf8)
    )
    let decoded = try CoreCommandJSONBoundary.decodeObject(from: result)
    #expect(decoded["status"] as? String == "available")
    #expect(throws: CoreCommandBoundaryError.self) {
        try CoreCommandJSONBoundary.decodeObject(
            from: CoreCommandResult(
                exitCode: 1,
                standardOutput: stdout,
                standardError: Data()
            )
        )
    }
}

@Test("Find query stays in bounded stdin and never enters argv")
func phase2SearchUsesStdin() throws {
    let query = try ContextSnapshotQuery(query: #"PAM "%_ 找回"#, limit: 25)
    let command = try ContextConsoleCoreCommands.snapshot(query)
    #expect(command.arguments == ["context-console", "snapshot", "--payload-stdin"])
    #expect(command.arguments.joined().contains("PAM") == false)
    let input = try #require(command.standardInput)
    let rawPayload = try JSONSerialization.jsonObject(with: input)
    let payload = try #require(rawPayload as? [String: Any])
    #expect(payload["query"] as? String == #"PAM "%_ 找回"#)
    #expect(throws: Phase2ContractError.self) {
        _ = try ContextSnapshotQuery(query: "bad\u{200B}query")
    }
}

@Test("Find project and 90-day filters use an explicit stdin range")
func phase2FindFiltersUseExplicitStdinRange() throws {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = try #require(TimeZone(secondsFromGMT: 0))
    let formatter = ISO8601DateFormatter()
    let now = try #require(formatter.date(from: "2026-07-30T12:00:00Z"))
    let range = Phase2FindTimePreset.retentionWindow.utcRange(
        now: now,
        calendar: calendar
    )
    #expect(range.startAt == "2026-05-02T00:00:00Z")
    #expect(range.endAt == "2026-07-31T00:00:00Z")

    let projectRef = "project_" + String(repeating: "a", count: 32)
    let query = try ContextSnapshotQuery(
        projectRef: projectRef,
        startAt: range.startAt,
        endAt: range.endAt,
        limit: 100
    )
    let command = try ContextConsoleCoreCommands.snapshot(query)
    let input = try #require(command.standardInput)
    let rawPayload = try JSONSerialization.jsonObject(with: input)
    let payload = try #require(rawPayload as? [String: Any])
    #expect(payload["project_ref"] as? String == projectRef)
    #expect(payload["start_at"] as? String == range.startAt)
    #expect(payload["end_at"] as? String == range.endAt)
    #expect(command.arguments.joined().contains(projectRef) == false)
}

@Test("Snapshot decoder separates verified context and clues")
func phase2SnapshotPartitions() throws {
    let decoded = try ContextSnapshotV1(data: try phase2JSON(phase2AvailableSnapshot()))
    #expect(decoded.readState == .available)
    #expect(decoded.projects?.count == 1)
    #expect(decoded.projects?.first?.verifiedItems.first?.safeLabel == "Phase 2")
    #expect(decoded.projects?.first?.compatibilityItems.first?.safeLabel == "旧确认安全摘要")
    #expect(decoded.projects?.first?.clues.first?.safeLabel == "待核实屏幕线索")
    #expect(decoded.bundle?.verifiedContexts.count == 1)
    #expect(decoded.bundle?.unverifiedClues.count == 1)
    #expect(decoded.confirmationCards?.count == 1)
    #expect(decoded.confirmationCards?.first?.revision == "rev_" + String(repeating: "a", count: 24))
    #expect(decoded.confirmationCards?.first?.question.contains("information_insufficient") == false)

    var envelopeOff = phase2AvailableSnapshot()
    envelopeOff["actions_enabled"] = false
    #expect(
        try ContextSnapshotV1(data: phase2JSON(envelopeOff)).actionsEnabled == false
    )
}

@Test("Snapshot rejects raw access and internal IDs")
func phase2SnapshotRejectsPrivateResidue() throws {
    var raw = phase2AvailableSnapshot()
    raw["raw_access"] = true
    #expect(throws: Phase2ContractError.self) {
        _ = try ContextSnapshotV1(data: phase2JSON(raw))
    }

    var internalID = phase2AvailableSnapshot()
    internalID["verified_ref"] = "verified_" + phase2RefA
    #expect(throws: Phase2ContractError.self) {
        _ = try ContextSnapshotV1(data: phase2JSON(internalID))
    }

    var diagnosticLeak = phase2AvailableSnapshot()
    diagnosticLeak["reason_code"] = "database_busy"
    #expect(throws: Phase2ContractError.self) {
        _ = try ContextSnapshotV1(data: phase2JSON(diagnosticLeak))
    }

    for unsafeValue in [
        "file:///" + "Users/example/private.txt",
        "/" + "Users/example/private.txt",
        "token=abcdefghijklmnopqrstuvwxyz",
        #"{"raw":"json"}"#,
        #"[1,2,3]"#,
    ] {
        var unsafe = phase2AvailableSnapshot()
        unsafe["note"] = unsafeValue
        #expect(throws: Phase2ContractError.self) {
            _ = try ContextSnapshotV1(data: phase2JSON(unsafe))
        }
    }
}

@Test("Unreadable state clears all trusted surfaces and stale responses are ignored")
func phase2GenerationAndClear() {
    let readable = ContextSnapshotV1(
        readState: .available,
        observedAt: "2026-07-30T01:00:00Z",
        lastSuccessfulReadAt: "2026-07-30T01:00:00Z",
        actionsEnabled: true,
        counts: nil,
        bundle: nil,
        projects: [],
        legacyConfirmed: [],
        confirmationCards: [],
        capabilities: nil
    )
    let busy = ContextSnapshotV1(
        readState: .busy,
        observedAt: "2026-07-30T01:01:00Z",
        lastSuccessfulReadAt: "2026-07-30T01:00:00Z",
        actionsEnabled: false,
        counts: nil,
        bundle: nil,
        projects: nil,
        legacyConfirmed: nil,
        confirmationCards: nil,
        capabilities: nil
    )
    var state = Phase2ConsoleViewState()
    let old = state.beginRequest(for: .search)
    let current = state.beginRequest(for: .search)
    let oldApplied = state.apply(snapshot: readable, for: .search, token: old)
    let currentApplied = state.apply(snapshot: readable, for: .search, token: current)
    #expect(oldApplied == false)
    #expect(currentApplied)
    #expect(state.searchSnapshot != nil)

    let refresh = state.beginRequest(for: .today)
    let busyApplied = state.apply(snapshot: busy, for: .today, token: refresh)
    #expect(busyApplied)
    #expect(state.readState == .busy)
    #expect(state.actionsEnabled == false)
    #expect(state.snapshot == nil)
    #expect(state.searchSnapshot == nil)
    #expect(state.memoryCandidates == nil)
    #expect(state.deliveryStatus == nil)
    #expect(state.localHandoff == nil)
}

@Test("Cross-channel invalidation blocks old search and handoff responses")
func phase2CrossChannelInvalidation() throws {
    let readable = try ContextSnapshotV1(
        data: try phase2JSON(phase2AvailableSnapshot())
    )
    let busy = ContextSnapshotV1(
        readState: .busy,
        observedAt: "2026-07-30T01:01:00Z",
        lastSuccessfulReadAt: "2026-07-30T01:00:00Z",
        actionsEnabled: false,
        counts: nil,
        bundle: nil,
        projects: nil,
        legacyConfirmed: nil,
        confirmationCards: nil,
        capabilities: nil
    )

    var state = Phase2ConsoleViewState()
    let oldSearch = state.beginRequest(for: .search)
    state.invalidateAllRequests()
    let refresh = state.beginRequest(for: .today)
    let busyApplied = state.apply(snapshot: busy, for: .today, token: refresh)
    let staleSearchApplied = state.apply(
        snapshot: readable,
        for: .search,
        token: oldSearch
    )
    #expect(busyApplied)
    #expect(staleSearchApplied == false)
    #expect(state.readState == .busy)
    #expect(state.searchSnapshot == nil)
}

@Test("System health resolves normal paused error and unknown consistently")
func phase2SystemHealthResolution() {
    func resolve(
        unknown: Bool = false,
        status: String = "running",
        danger: Bool = false,
        stale: Bool = false,
        privacyPause: Bool = false
    ) -> Phase2SystemHealth {
        Phase2SystemHealth.resolve(
            displayStateUnknown: unknown,
            status: status,
            statusIsDanger: danger,
            hasKnownCaptureError: false,
            launchAgentLoaded: true,
            launchAgentRunning: true,
            captureFreshnessIsStale: stale,
            captureBlockedBySession: privacyPause
        )
    }

    #expect(resolve() == .normal)
    #expect(resolve(unknown: true) == .error)
    #expect(resolve(danger: true) == .error)
    #expect(resolve(status: "paused") == .paused)
    #expect(resolve(status: "locked") == .paused)
    #expect(resolve(privacyPause: true) == .paused)
    #expect(
        Phase2SystemHealth.resolve(
            displayStateUnknown: false,
            status: "stopped",
            statusIsDanger: false,
            hasKnownCaptureError: false,
            launchAgentLoaded: false,
            launchAgentRunning: false,
            captureFreshnessIsStale: false,
            captureBlockedBySession: false
        ) == .paused
    )
    #expect(
        Phase2SystemHealth.resolve(
            displayStateUnknown: false,
            status: "capturing",
            statusIsDanger: false,
            hasKnownCaptureError: false,
            launchAgentLoaded: false,
            launchAgentRunning: false,
            captureFreshnessIsStale: false,
            captureBlockedBySession: false
        ) == .normal
    )
    #expect(resolve(status: "error") == .error)
    #expect(resolve(status: "session_state_unknown") == .error)
    #expect(resolve(status: "stopped") == .error)
    #expect(resolve(status: "unexpected_status") == .error)
    #expect(resolve(status: "paused", danger: true) == .error)
    #expect(resolve(status: "locked", stale: true) == .paused)
    #expect(resolve(danger: true, privacyPause: true) == .error)
}

@Test("Presentation copy never exposes Core reason codes")
func phase2PresentationCopyIsClosed() {
    let diagnostic = "private_internal_reason_42"
    for state in [
        Phase2ReadState.busy,
        .unknown,
        .unavailable,
        .blocked,
    ] {
        let copy = Phase2PresentationCopy.unavailableMessage(for: state)
        #expect(copy.contains(diagnostic) == false)
        #expect(copy.contains("reason") == false)
        #expect(copy.contains("database_busy") == false)
        #expect(copy.localizedCaseInsensitiveContains("busy") == false)
        #expect(copy.contains("数据库") == false)
        #expect(copy.isEmpty == false)
    }
    #expect(Phase2PresentationCopy.unavailableMessage(for: .available).isEmpty)
}

@Test("Semantic handoff rejection stays local to preview")
func phase2HandoffRejectionIsolation() throws {
    let readable = try ContextSnapshotV1(
        data: try phase2JSON(phase2AvailableSnapshot())
    )
    let rejected: [String: Any] = [
        "contract_version": "LocalHandoffV1",
        "status": "rejected",
        "observed_at": "2026-07-30T01:02:00Z",
        "last_successful_read_at": "2026-07-30T01:02:00Z",
        "handoff_ref": NSNull(),
        "bundle_hash": NSNull(),
        "project": NSNull(),
        "verified_contexts": NSNull(),
        "unresolved_questions": NSNull(),
        "expires_at": NSNull(),
        "revocation_state": NSNull(),
        "raw_access": false,
    ]
    let localRejection = try LocalHandoffV1(data: try phase2JSON(rejected))
    #expect(localRejection.readState == .blocked)

    var state = Phase2ConsoleViewState()
    let today = state.beginRequest(for: .today)
    let todayApplied = state.apply(snapshot: readable, for: .today, token: today)
    #expect(todayApplied)
    let handoff = state.beginRequest(for: .handoff)
    let handoffApplied = state.apply(localHandoff: localRejection, token: handoff)
    #expect(handoffApplied)
    #expect(state.readState == .available)
    #expect(state.snapshot != nil)
    #expect(state.localHandoff == nil)
}

@Test("Owner action and feedback content stay in stdin")
func phase2WriteContractsUseStdin() throws {
    let action = OwnerContextActionV1(
        action: .manualNote,
        idempotencyKey: "idem_" + phase2RefA,
        explicitlyConfirmedOwner: true,
        safeLabel: "完成 Phase 2 模型",
        outcome: "completed",
        startedAt: "2026-07-30T01:00:00Z",
        endedAt: "2026-07-30T01:05:00Z",
        projectRef: "project_" + phase2RefA,
        projectLabel: "PAM"
    )
    let command = try action.coreCommandRequest()
    #expect(command.arguments == ["context-console", "owner-action", "--payload-stdin"])
    #expect(command.arguments.joined().contains("完成 Phase 2") == false)
    #expect(command.standardInput != nil)
    let manualInput = try #require(command.standardInput)
    let rawManualPayload = try JSONSerialization.jsonObject(with: manualInput)
    let manualPayload = try #require(rawManualPayload as? [String: Any])
    #expect(manualPayload["project_label"] as? String == "PAM")
    #expect(manualPayload["context_state"] == nil)

    let confirmationAction = OwnerContextActionV1(
        action: .confirmCard,
        idempotencyKey: "idem_" + phase2RefB,
        explicitlyConfirmedOwner: true,
        confirmationRef: "confirmation_" + phase2RefA,
        expectedRevision: "rev_" + String(repeating: "a", count: 24),
        answer: .occurred,
        reason: "correct"
    )
    let confirmationCommand = try confirmationAction.coreCommandRequest()
    let confirmationInput = try #require(confirmationCommand.standardInput)
    let rawConfirmationPayload = try JSONSerialization.jsonObject(with: confirmationInput)
    let confirmationPayload = try #require(rawConfirmationPayload as? [String: Any])
    #expect(
        confirmationPayload["expected_revision"] as? String
            == "rev_" + String(repeating: "a", count: 24)
    )
    #expect(confirmationPayload["project_ref"] == nil)

    let unclassified = OwnerContextActionV1(
        action: .manualNote,
        idempotencyKey: "idem_" + String(repeating: "c", count: 32),
        explicitlyConfirmedOwner: true,
        safeLabel: "空库补记",
        outcome: "unknown",
        startedAt: "2026-07-30T01:00:00Z",
        endedAt: "2026-07-30T01:01:00Z",
        projectLabel: "未归类"
    )
    let unclassifiedCommand = try unclassified.coreCommandRequest()
    let unclassifiedInput = try #require(unclassifiedCommand.standardInput)
    let rawUnclassifiedPayload = try JSONSerialization.jsonObject(with: unclassifiedInput)
    let unclassifiedPayload = try #require(rawUnclassifiedPayload as? [String: Any])
    #expect(unclassifiedPayload["project_ref"] == nil)
    #expect(unclassifiedPayload["project_label"] as? String == "未归类")

    let feedback = ProductFeedbackRequestV1(
        idempotencyKey: "idem_" + phase2RefB,
        sourceKind: .bundle,
        sourceRef: "bundle_" + phase2RefA,
        projectRef: "project_" + phase2RefA,
        useful: false,
        resumeSeconds: 12
    )
    let feedbackCommand = try ContextConsoleCoreCommands.productFeedback(feedback)
    #expect(feedbackCommand.arguments == ["context-console", "product-feedback", "--payload-stdin"])
    #expect(feedbackCommand.standardInput != nil)
}

@Test("Queued Preview A feedback makes zero calls after Preview B is bound")
func phase2FeedbackActionTimeAuthorization() throws {
    let sourceA = "handoff_" + phase2RefA
    let projectA = "project_" + phase2RefA
    let sourceB = "handoff_" + phase2RefB
    let projectB = "project_" + phase2RefB
    var authorization = Phase2FeedbackActionAuthorizationState()

    let previewACandidate = authorization.bind(
        sourceRef: sourceA,
        projectRef: projectA,
        timerGeneration: 71
    )
    let previewA = try #require(previewACandidate)
    authorization.invalidate()
    let previewBCandidate = authorization.bind(
        sourceRef: sourceB,
        projectRef: projectB,
        timerGeneration: 72
    )
    let previewB = try #require(previewBCandidate)

    var serviceCalls = 0
    func attempt(
        _ expected: Phase2FeedbackActionAuthorization,
        sourceRef: String,
        projectRef: String,
        timerGeneration: UInt64
    ) {
        guard authorization.authorizes(
            expected,
            currentSourceRef: sourceRef,
            currentProjectRef: projectRef,
            currentTimerGeneration: timerGeneration
        ) else { return }
        serviceCalls += 1
    }

    attempt(
        previewA,
        sourceRef: sourceB,
        projectRef: projectB,
        timerGeneration: 72
    )
    #expect(serviceCalls == 0)

    attempt(
        previewB,
        sourceRef: sourceB,
        projectRef: projectB,
        timerGeneration: 72
    )
    #expect(serviceCalls == 1)

    let replacementCandidate = authorization.bind(
        sourceRef: sourceB,
        projectRef: projectB,
        timerGeneration: 73
    )
    let replacement = try #require(replacementCandidate)
    attempt(
        previewB,
        sourceRef: sourceB,
        projectRef: projectB,
        timerGeneration: 73
    )
    #expect(serviceCalls == 1)
    #expect(replacement != previewB)

    authorization.invalidate()
    attempt(
        replacement,
        sourceRef: sourceB,
        projectRef: projectB,
        timerGeneration: 73
    )
    #expect(serviceCalls == 1)
}

@Test("Feedback submission checkpoint survives navigation and stale presentation tokens")
func phase2FeedbackSubmissionCheckpointSurvivesNavigation() throws {
    let runningObject: [String: Any] = [
        "contract_version": "ProductExperimentStatusV1",
        "read_state": "available",
        "observed_at": "2026-08-01T06:00:00Z",
        "status": "collecting",
        "experiment_started": true,
        "experiment_ref": "experiment_" + phase2RefC,
        "attempt_count": 3,
        "complete_attempt_count": 3,
        "useful_rate": 2.0 / 3.0,
        "median_resume_seconds": 12.5,
        "raw_access": false,
    ]
    let running = try ProductExperimentStatusV1(data: try phase2JSON(runningObject))
    let first = ProductFeedbackRequestV1(
        idempotencyKey: "idem_" + phase2RefA,
        sourceKind: .localHandoff,
        sourceRef: "handoff_" + phase2RefA,
        projectRef: "project_" + phase2RefA,
        useful: true,
        resumeSeconds: 8,
        experimentRef: "experiment_" + phase2RefC
    )
    let replacement = ProductFeedbackRequestV1(
        idempotencyKey: "idem_" + phase2RefB,
        sourceKind: .localHandoff,
        sourceRef: "handoff_" + phase2RefB,
        projectRef: "project_" + phase2RefB,
        useful: false,
        resumeSeconds: 9,
        experimentRef: "experiment_" + phase2RefC
    )

    var binding = Phase2ExperimentFeedbackBindingState()
    let opened = binding.applyExperimentStatus(running)
    #expect(opened)
    let staged = binding.requestForSubmission(first, choice: .useful)
    #expect(staged == first)
    #expect(binding.submissionInFlight)

    var viewState = Phase2ConsoleViewState()
    let oldFeedbackToken = viewState.beginRequest(for: .feedback)
    viewState.invalidateRequests([.feedback, .handoff])
    let oldPresentationAccepted = viewState.cancel(oldFeedbackToken)
    #expect(oldPresentationAccepted == false)
    #expect(binding.submissionInFlight)
    #expect(binding.canSubmit(sourceRef: replacement.sourceRef) == false)
    let replacementWhileSubmitting = binding.requestForSubmission(
        replacement,
        choice: .notUseful
    )
    #expect(replacementWhileSubmitting == nil)

    let recorded = binding.markRecordedAwaitingStatusRefresh(request: first)
    #expect(recorded)
    #expect(binding.statusRefreshPending)
    let statusToken = viewState.beginRequest(for: .experiment)
    let statusAccepted = viewState.apply(
        productExperimentStatus: running,
        token: statusToken
    )
    #expect(statusAccepted)
    if statusAccepted {
        let reopened = binding.applyExperimentStatus(running)
        #expect(reopened)
    }
    #expect(binding.statusRefreshPending == false)
    #expect(binding.canSubmit(sourceRef: replacement.sourceRef))

    var retry = Phase2ExperimentFeedbackBindingState()
    let retryOpened = retry.applyExperimentStatus(running)
    #expect(retryOpened)
    let retryStaged = retry.requestForSubmission(first, choice: .useful)
    #expect(retryStaged == first)
    let becameUnknown = retry.markTransportUnknown(request: first)
    #expect(becameUnknown)
    #expect(retry.submissionCheckpoint == .transportUnknown)
    #expect(retry.canSubmit(sourceRef: replacement.sourceRef) == false)
    let differentSource = retry.requestForSubmission(replacement, choice: .notUseful)
    #expect(differentSource == nil)
    let remeasuredSameIntent = ProductFeedbackRequestV1(
        idempotencyKey: first.idempotencyKey,
        sourceKind: first.sourceKind,
        sourceRef: first.sourceRef,
        projectRef: first.projectRef,
        useful: first.useful,
        resumeSeconds: 99,
        experimentRef: first.experimentRef
    )
    let exactRetry = retry.requestForSubmission(remeasuredSameIntent, choice: .useful)
    #expect(exactRetry == first)
}

@Test("Old same-ref handoff render cannot consume a newer timer generation")
func phase2HandoffRenderGeneration() throws {
    let source = "handoff_" + phase2RefA
    let project = "project_" + phase2RefA
    var render = Phase2HandoffRenderCheckpointState()
    let oldCandidate = render.stage(
        sourceRef: source,
        projectRef: project,
        timerGeneration: 81
    )
    let old = try #require(oldCandidate)
    let currentCandidate = render.stage(
        sourceRef: source,
        projectRef: project,
        timerGeneration: 82
    )
    let current = try #require(currentCandidate)

    let oldConsumed = render.consume(
        old,
        currentSourceRef: source,
        currentProjectRef: project,
        currentTimerGeneration: 82
    )
    #expect(oldConsumed == false)
    #expect(render.pending == current)
    let wrongGenerationConsumed = render.consume(
        current,
        currentSourceRef: source,
        currentProjectRef: project,
        currentTimerGeneration: 81
    )
    #expect(wrongGenerationConsumed == false)
    #expect(render.pending == current)
    let currentConsumed = render.consume(
        current,
        currentSourceRef: source,
        currentProjectRef: project,
        currentTimerGeneration: 82
    )
    #expect(currentConsumed)
    #expect(render.pending == nil)
}

@Test("Recorded feedback stays closed until the current running status checkpoint")
func phase2FeedbackStatusRefreshPending() throws {
    let runningObject: [String: Any] = [
        "contract_version": "ProductExperimentStatusV1",
        "read_state": "available",
        "observed_at": "2026-08-01T06:00:00Z",
        "status": "collecting",
        "experiment_started": true,
        "experiment_ref": "experiment_" + phase2RefC,
        "attempt_count": 3,
        "complete_attempt_count": 3,
        "useful_rate": 2.0 / 3.0,
        "median_resume_seconds": 12.5,
        "raw_access": false,
    ]
    let running = try ProductExperimentStatusV1(data: try phase2JSON(runningObject))
    var binding = Phase2ExperimentFeedbackBindingState()
    let initiallyOpened = binding.applyExperimentStatus(running)
    #expect(initiallyOpened)

    let first = ProductFeedbackRequestV1(
        idempotencyKey: "idem_" + phase2RefA,
        sourceKind: .localHandoff,
        sourceRef: "handoff_" + phase2RefA,
        projectRef: "project_" + phase2RefA,
        useful: true,
        resumeSeconds: 8,
        experimentRef: "experiment_" + phase2RefC
    )
    let stagedFirst = binding.requestForSubmission(first, choice: .useful)
    #expect(stagedFirst == first)
    let firstRecorded = binding.markRecordedAwaitingStatusRefresh(request: first)
    #expect(firstRecorded)
    #expect(binding.statusRefreshPending)
    #expect(binding.canSubmit(sourceRef: "handoff_" + phase2RefB) == false)

    let second = ProductFeedbackRequestV1(
        idempotencyKey: "idem_" + phase2RefB,
        sourceKind: .localHandoff,
        sourceRef: "handoff_" + phase2RefB,
        projectRef: "project_" + phase2RefB,
        useful: false,
        resumeSeconds: 9,
        experimentRef: "experiment_" + phase2RefC
    )
    let blockedWhilePending = binding.requestForSubmission(second, choice: .notUseful)
    #expect(blockedWhilePending == nil)

    var viewState = Phase2ConsoleViewState()
    let staleToken = viewState.beginRequest(for: .experiment)
    let currentToken = viewState.beginRequest(for: .experiment)
    let staleAccepted = viewState.apply(
        productExperimentStatus: running,
        token: staleToken
    )
    #expect(staleAccepted == false)
    if staleAccepted {
        _ = binding.applyExperimentStatus(running)
    }
    #expect(binding.statusRefreshPending)

    let currentAccepted = viewState.apply(
        productExperimentStatus: running,
        token: currentToken
    )
    #expect(currentAccepted)
    if currentAccepted {
        let reopened = binding.applyExperimentStatus(running)
        #expect(reopened)
    }
    #expect(binding.statusRefreshPending == false)
    #expect(binding.canSubmit(sourceRef: second.sourceRef))

    let stagedSecond = binding.requestForSubmission(second, choice: .notUseful)
    #expect(stagedSecond == second)
    let secondRecorded = binding.markRecordedAwaitingStatusRefresh(request: second)
    #expect(secondRecorded)
    #expect(binding.statusRefreshPending)

    let unavailableObject: [String: Any] = [
        "contract_version": "ProductExperimentStatusV1",
        "read_state": "unavailable",
        "observed_at": "2026-08-01T06:00:01Z",
        "status": NSNull(),
        "experiment_started": NSNull(),
        "experiment_ref": NSNull(),
        "attempt_count": NSNull(),
        "complete_attempt_count": NSNull(),
        "useful_rate": NSNull(),
        "median_resume_seconds": NSNull(),
        "raw_access": false,
    ]
    let unavailable = try ProductExperimentStatusV1(
        data: try phase2JSON(unavailableObject)
    )
    let unavailableReopened = binding.applyExperimentStatus(unavailable)
    #expect(unavailableReopened == false)
    #expect(binding.statusRefreshPending)
    #expect(binding.runningExperimentRef == nil)
    #expect(binding.canSubmit(sourceRef: first.sourceRef) == false)

    let recovered = binding.applyExperimentStatus(running)
    #expect(recovered)
    #expect(binding.statusRefreshPending == false)
}

@Test("Experiment-status refresh failure preserves every feedback submission checkpoint")
func phase2ExperimentRefreshFailurePreservesFeedbackCheckpoints() throws {
    let runningObject: [String: Any] = [
        "contract_version": "ProductExperimentStatusV1",
        "read_state": "available",
        "observed_at": "2026-08-01T06:00:00Z",
        "status": "collecting",
        "experiment_started": true,
        "experiment_ref": "experiment_" + phase2RefC,
        "attempt_count": 3,
        "complete_attempt_count": 3,
        "useful_rate": 2.0 / 3.0,
        "median_resume_seconds": 12.5,
        "raw_access": false,
    ]
    let unavailableObject: [String: Any] = [
        "contract_version": "ProductExperimentStatusV1",
        "read_state": "unavailable",
        "observed_at": "2026-08-01T06:00:01Z",
        "status": NSNull(),
        "experiment_started": NSNull(),
        "experiment_ref": NSNull(),
        "attempt_count": NSNull(),
        "complete_attempt_count": NSNull(),
        "useful_rate": NSNull(),
        "median_resume_seconds": NSNull(),
        "raw_access": false,
    ]
    let running = try ProductExperimentStatusV1(data: try phase2JSON(runningObject))
    let unavailable = try ProductExperimentStatusV1(
        data: try phase2JSON(unavailableObject)
    )
    let first = ProductFeedbackRequestV1(
        idempotencyKey: "idem_" + phase2RefA,
        sourceKind: .localHandoff,
        sourceRef: "handoff_" + phase2RefA,
        projectRef: "project_" + phase2RefA,
        useful: true,
        resumeSeconds: 8,
        experimentRef: "experiment_" + phase2RefC
    )
    let replacement = ProductFeedbackRequestV1(
        idempotencyKey: "idem_" + phase2RefB,
        sourceKind: .localHandoff,
        sourceRef: "handoff_" + phase2RefB,
        projectRef: "project_" + phase2RefB,
        useful: false,
        resumeSeconds: 9,
        experimentRef: "experiment_" + phase2RefC
    )

    var submitting = Phase2ExperimentFeedbackBindingState()
    let submittingOpened = submitting.applyExperimentStatus(running)
    #expect(submittingOpened)
    let submittingRequest = submitting.requestForSubmission(first, choice: .useful)
    #expect(submittingRequest == first)
    submitting.loseTrustPreservingSubmission()
    #expect(submitting.runningExperimentRef == nil)
    #expect(submitting.submissionInFlight)
    #expect(submitting.pendingRetry?.request == first)
    #expect(submitting.canSubmit(sourceRef: replacement.sourceRef) == false)
    let replacementWhileSubmitting = submitting.requestForSubmission(
        replacement,
        choice: .notUseful
    )
    #expect(replacementWhileSubmitting == nil)
    let delayedReceiptAccepted = submitting.markRecordedAwaitingStatusRefresh(
        request: first
    )
    #expect(delayedReceiptAccepted)
    #expect(submitting.statusRefreshPending)
    #expect(submitting.canSubmit(sourceRef: replacement.sourceRef) == false)
    let submittingRecovered = submitting.applyExperimentStatus(running)
    #expect(submittingRecovered)
    #expect(submitting.statusRefreshPending == false)
    #expect(submitting.canSubmit(sourceRef: replacement.sourceRef))

    var transportUnknown = Phase2ExperimentFeedbackBindingState()
    let transportOpened = transportUnknown.applyExperimentStatus(running)
    #expect(transportOpened)
    let transportRequest = transportUnknown.requestForSubmission(first, choice: .useful)
    #expect(transportRequest == first)
    let transportFailed = transportUnknown.markTransportUnknown(request: first)
    #expect(transportFailed)
    transportUnknown.loseTrustPreservingSubmission()
    #expect(transportUnknown.submissionCheckpoint == .transportUnknown)
    #expect(transportUnknown.runningExperimentRef == nil)
    #expect(transportUnknown.canSubmit(sourceRef: replacement.sourceRef) == false)
    let remeasuredSameIntent = ProductFeedbackRequestV1(
        idempotencyKey: "idem_" + phase2RefC,
        sourceKind: first.sourceKind,
        sourceRef: first.sourceRef,
        projectRef: first.projectRef,
        useful: first.useful,
        resumeSeconds: 99,
        experimentRef: first.experimentRef
    )
    let exactRetry = transportUnknown.requestForSubmission(
        remeasuredSameIntent,
        choice: .useful
    )
    #expect(exactRetry == first)
    let retryReceiptAccepted = transportUnknown.markRecordedAwaitingStatusRefresh(
        request: first
    )
    #expect(retryReceiptAccepted)
    #expect(transportUnknown.statusRefreshPending)
    let unavailableReopened = transportUnknown.applyExperimentStatus(unavailable)
    #expect(unavailableReopened == false)
    #expect(transportUnknown.statusRefreshPending)
    #expect(transportUnknown.runningExperimentRef == nil)
    #expect(transportUnknown.canSubmit(sourceRef: replacement.sourceRef) == false)
    let transportRecovered = transportUnknown.applyExperimentStatus(running)
    #expect(transportRecovered)
    #expect(transportUnknown.statusRefreshPending == false)
    #expect(transportUnknown.canSubmit(sourceRef: replacement.sourceRef))
}

private func phase2AvailableSnapshot() -> [String: Any] {
    let verified = phase2VerifiedContext()
    let legacy = phase2LegacyContext()
    return [
        "contract_version": "ContextSnapshotV1",
        "status": "available",
        "observed_at": "2026-07-30T01:00:00Z",
        "last_successful_read_at": "2026-07-30T01:00:00Z",
        "actions_enabled": true,
        "counts": [
            "verified_contexts": 1,
            "unverified_clues": 1,
            "legacy_confirmed": 1,
            "pending_confirmations": 1,
        ],
        "context_bundle_v1": [
            "contract_version": "ContextBundleV1",
            "status": "available",
            "bundle_hash": "sha256:" + String(repeating: "a", count: 64),
            "verified_contexts": [verified],
            "unverified_clues": [phase2UnverifiedClue()],
            "raw_access": false,
        ],
        "projects": [[
            "project": phase2Label("project", "PAM"),
            "verified_contexts": [verified],
            "compatibility_contexts": [legacy],
            "unverified_clues": [phase2UnverifiedClue()],
            "latest_occurred_at": "2026-07-30T00:55:00Z",
        ]],
        "legacy_confirmed": [legacy],
        "confirmation_questions": [[
            "confirmation_ref": "confirmation_" + phase2RefA,
            "revision": "rev_" + String(repeating: "a", count: 24),
            "title": "确认 Phase 2 进展",
            "question": "Phase 2 模型是否已经完成？",
            "evidence": "本地源码发生变化",
            "priority": "high",
            "state": "pending",
            "event_type": "activity",
            "app": "Codex",
            "time_label": "刚刚",
            "observed_start": "2026-07-30T00:40:00Z",
            "observed_end": "2026-07-30T00:50:00Z",
            "answer_options": ["occurred", "not_occurred", "ignored", "defer"],
            "verification_state": "unverified",
            "raw_access": false,
        ]],
        "capabilities": [
            "search": true,
            "owner_action": true,
            "local_handoff": true,
            "product_feedback": true,
        ],
        "raw_access": false,
    ]
}

private func phase2VerifiedContext() -> [String: Any] {
    [
        "context_ref": "context_" + phase2RefA,
        "source_ref": "source_" + phase2RefA,
        "state": "in_progress",
        "occurred_at": "2026-07-30T00:50:00Z",
        "expires_at": "2026-10-28T00:50:00Z",
        "revocation_state": "active",
        "privacy_class": "private_local",
        "project": phase2Label("project", "PAM"),
        "workstream": phase2Label("workstream", "Phase 2"),
        "object": phase2Label("object", "Phase 2"),
        "version": phase2Label("version", "v2"),
        "subject": phase2Label("subject", "Local Owner"),
        "source": ["kind": "user_confirmation", "evidence_tier": "P1"],
    ]
}

private func phase2UnverifiedClue() -> [String: Any] {
    [
        "source_ref": "source_" + phase2RefB,
        "clue_type": "screen",
        "observed_at": "2026-07-30T00:49:00Z",
        "expires_at": "2026-07-31T23:59:59Z",
        "revocation_state": "active",
        "privacy_class": "private_local",
        "project": phase2Label("project", "PAM"),
        "verification_state": "unverified",
        "safe_label": "待核实屏幕线索",
    ]
}

private func phase2LegacyContext() -> [String: Any] {
    [
        "context_ref": "context_" + phase2RefB,
        "state": "completed_confirmed",
        "occurred_at": "2026-07-29T08:00:00Z",
        "expires_at": "2026-10-27T08:00:00Z",
        "revocation_state": "active",
        "privacy_class": "private_local",
        "project": phase2Label("project", "PAM"),
        "summary": "旧确认安全摘要",
        "source": ["kind": "legacy_user_confirmation", "label": "本人确认"],
        "compatibility_source": true,
        "external_delivery_eligible": false,
        "memory_candidate_eligible": false,
        "raw_access": false,
    ]
}

private func phase2Label(_ kind: String, _ label: String) -> [String: Any] {
    ["ref": "\(kind)_" + phase2RefA, "label": label]
}

private func phase2JSON(_ dictionary: [String: Any]) throws -> Data {
    try JSONSerialization.data(withJSONObject: dictionary, options: [.sortedKeys])
}

@Test("Closure states and original responsible chat survive attention decoding")
func closureProjectionRetainsStateAndThread() throws {
    let chat = "00000000-0000-4000-8000-000000000001"
    let rows = ["pending", "running", "verification", "blocked", "decision", "resolved", "canceled", "dismissed"].map { state in
        ["id": "closure:" + state, "kind": "closure", "title": "合成闭环事项", "summary": state,
         "state": state, "thread_id": chat, "requires_check": state == "canceled", "revision": "2", "members": []] as [String: Any]
    }
    let value: [String: Any] = ["interface": "attention-center", "contract": 1, "status": "ok",
        "raw_access": false, "decision_count": 1, "decisions": [rows[4]], "systems": [], "resources": [], "closures": rows]
    let raw = String(data: try JSONSerialization.data(withJSONObject: value), encoding: .utf8)!
    let snapshot = AttentionSnapshot(jsonText: raw)
    #expect(snapshot.status == "ok")
    #expect(snapshot.decisionCount == 1)
    #expect(snapshot.closures.count == 8)
    #expect(snapshot.closures[6].closureRequiresCheck)
    #expect(snapshot.closures[4].closureState == "decision")
    #expect(snapshot.closures[4].closureThreadID == chat)
}

@Test("Unreadable closure item fails closed instead of a verified empty list")
func closureUnreadableProjectionIsNotZero() {
    let snapshot = AttentionSnapshot(jsonText: """
    {"interface":"attention-center","contract":1,"status":"ok","raw_access":false,
     "decision_count":0,"decisions":[],"systems":[],"resources":[],"closures":[{"id":"malformed"}]}
    """)
    #expect(snapshot.status == "unknown")
    #expect(snapshot.decisionCount == nil)
}


private func attentionFixture(_ count: Int?, rows: [[String: Any]], closures: [[String: Any]]) -> AttentionSnapshot {
    let value: [String: Any] = ["interface":"attention-center", "contract":1, "raw_access":false,
        "status": count == nil ? "partial" : "ok", "decision_count": count as Any? ?? NSNull(),
        "decisions":rows, "closures":closures, "systems":[], "resources":[]]
    return AttentionSnapshot(jsonText: String(data: try! JSONSerialization.data(withJSONObject:value), encoding:.utf8)!)
}
private func closure(_ id: String, _ state: String, check: Bool = false) -> [String: Any] {
    ["id":id, "kind":"closure", "title":id, "state":state, "requires_check":check, "revision":"2"]
}

@Test("Decision closures remain at the top and are excluded from follow-up and history")
func attentionPartitionsKeepAllDecisions() {
    let a = closure("a","decision"), b = closure("b","decision")
    let snapshot = attentionFixture(2, rows:[a,b], closures:[closure("pending","pending"),a,
        closure("running","running"),b,closure("blocked","blocked"),closure("closed","resolved")])
    #expect(snapshot.decisions.map(\.id) == ["a","b"])
    #expect(snapshot.badge == "2")
    #expect(snapshot.followUpClosures.map(\.id) == ["pending","running","blocked"])
    #expect(snapshot.closedClosures.map(\.id) == ["closed"])
}

@Test("Zero decisions still retains system work and cancellation with pending verification")
func attentionZeroDoesNotHideFollowUp() {
    let snapshot = attentionFixture(0, rows:[], closures:[closure("canceled","canceled",check:true),
        closure("dismissed","dismissed"),closure("verification","verification")])
    #expect(snapshot.decisions.isEmpty && snapshot.badge == nil)
    #expect(snapshot.followUpClosures.map(\.id) == ["canceled","verification"])
    #expect(snapshot.closedClosures.map(\.id) == ["dismissed"])
}

@Test("Unknown totals retain trustworthy decisions without inventing a count")
func attentionPartialKeepsReadableDecisions() {
    let a = closure("a","decision")
    let snapshot = attentionFixture(nil, rows:[a], closures:[a,closure("b","blocked")])
    #expect(snapshot.decisionCount == nil && snapshot.badge == nil)
    #expect(snapshot.decisions.map(\.id) == ["a"])
    #expect(snapshot.followUpClosures.map(\.id) == ["b"])
}

@Test("A previous opening or a different mode cannot own the current detail")
func attentionReopeningRejectsOldIdentity() {
    let old = AttentionPresentation(issueID:"a",revision:"2",mode:.inline)
    let reopened = AttentionPresentation(issueID:"a",revision:"2",mode:.inline)
    let other = AttentionPresentation(issueID:"b",revision:"2",mode:.inline)
    let sheet = AttentionPresentation(issueID:"a",revision:"2",mode:.sheet)
    #expect(old.owns(old))
    #expect(!reopened.owns(old) && !other.owns(old) && !sheet.owns(old))
}

@Test("A changed version invalidates old choices while preserving the opening identity")
func attentionVersionChangeInvalidatesOldTerms() {
    let old = AttentionPresentation(issueID:"a",revision:"2",mode:.inline)
    var fresh = old; fresh.revision = "3"
    #expect(old.id == fresh.id && !fresh.owns(old))
    let child = AttentionPresentation(issueID:"child",revision:"2",mode:.inline,anchorID:"parent")
    #expect(child.anchorID == "parent" && child.issueID == "child")
}

@Test("In-flight submissions remain blocked across collapse and preserve late receipts")
func attentionSubmissionSurvivesPresentationClose() {
    var submissions = AttentionSubmissions()
    let key = AttentionSubmissions.key("a","2")
    let attempt1 = submissions.begin(key)
    #expect(attempt1)
    let attempt2 = submissions.begin(key)
    #expect(!attempt2)
    submissions.finish(key,receipt:"original receipt",outcome:.recorded)
    #expect(submissions.receipts[key] == "original receipt")
    let attempt3 = submissions.begin(key)
    #expect(!attempt3)
    let attempt4 = submissions.begin(AttentionSubmissions.key("a","3"))
    #expect(attempt4)
}

@Test("Unknown outcomes never become a free retry or disappear after reopening")
func attentionUnknownWriteIsNotRetried() {
    var submissions = AttentionSubmissions()
    let key = AttentionSubmissions.key("a","2")
    let attempt5 = submissions.begin(key)
    #expect(attempt5)
    submissions.finish(key,receipt:"unreadable receipt",outcome:.unknown)
    #expect(submissions.pending.isEmpty && submissions.uncertain.contains(key))
    let attempt6 = submissions.begin(key)
    #expect(!attempt6)
    #expect(submissions.receipts[key] == "unreadable receipt")
}
@Test("A verified rejection requires a fresh detail read and retains every receipt")
func attentionRejectedWriteNeedsVerifiedRead() {
    var submissions = AttentionSubmissions()
    let key = AttentionSubmissions.key("a","2")
    let first = submissions.begin(key)
    #expect(first)
    submissions.finish(key,receipt:"prewrite rejection",outcome:.rejected)
    #expect(submissions.blocked(key))
    submissions.verifiedRead(id:"b",revision:"2")
    #expect(submissions.blocked(key))
    submissions.verifiedRead(id:"a",revision:"2")
    let second = submissions.begin(key)
    #expect(second)
    submissions.finish(key,receipt:"second receipt",outcome:.unknown)
    submissions.verifiedRead(id:"a",revision:"2")
    #expect(submissions.blocked(key))
    #expect(submissions.receiptHistory[key] == ["prewrite rejection","second receipt"])
}

@Test("Only exact prewrite rejections unlock after rereading and ambiguous commits remain unknown")
func attentionOutcomeClassificationFailsClosed() {
    #expect(AttentionWriteOutcome.closure(["interface":"task-closure","status":"blocked","error":"choice_terms_changed"],id:"a",revision:"2") == .rejected)
    #expect(AttentionWriteOutcome.closure(["interface":"task-closure","status":"blocked","error":"choice_missing_or_already_answered"],id:"a",revision:"2") == .unknown)
    #expect(AttentionWriteOutcome.closure(["id":"b","revision":3,"state":"pending"],id:"a",revision:"2") == .unknown)
    #expect(AttentionWriteOutcome.closure(["id":"a","revision":3,"state":"pending"],id:"a",revision:"2") == .recorded)
    #expect(AttentionWriteOutcome.memory(["interface":"attention-center","contract":1,"status":"unavailable","error":"内容或成员已变化，请重新预览后确认。"] ) == .rejected)
    #expect(AttentionWriteOutcome.memory(["interface":"attention-center","contract":1,"status":"unavailable","error":"ledger write failed: uncertain"] ) == .unknown)
    #expect(AttentionWriteOutcome.activity(["status":"blocked","error":"group_already_resolved","group_key":"a"],groupKey:"a") == .unknown)
    #expect(AttentionWriteOutcome.activity(["status":"recorded","group_key":"b"],groupKey:"a") == .unknown)
    #expect(AttentionWriteOutcome.activity(["status":"stale","error":"group_members_changed","group_key":"a"],groupKey:"a") == .rejected)
    #expect(AttentionWriteOutcome.closure(["interface":"task-closure","status":"blocked","error":"choice_invalid","id":"b"],id:"a",revision:"2") == .unknown)
    #expect(AttentionWriteOutcome.closure(["id":"a","revision":3,"state":"blocked"],id:"a",revision:"2") == .unknown)
    let validPreview: [String: Any] = ["interface":"memory-review","contract":2,"raw_access":false,"attention_revision":"2",
        "preview_digest":String(repeating:"a",count:64),"item_count":1,"changes":[["memory_id":"m","action":"keep_active"]]]
    #expect(AttentionMemoryPreview.matches(validPreview,revision:"2",decisions:["m":"keep_active"]))
    #expect(!AttentionMemoryPreview.matches(validPreview,revision:"3",decisions:["m":"keep_active"]))
    #expect(!AttentionMemoryPreview.matches(validPreview,revision:"2",decisions:["n":"keep_active"]))
    #expect(!AttentionMemoryPreview.matches(validPreview,revision:"2",decisions:["m":"mark_legacy"]))
    let context = AttentionPresentation(issueID:"a",revision:"3",mode:.inline)
    #expect(!context.matches(issueID:"a",revision:"2"))
}

@Test("Recorded deferral permits a new explicit judgment only after a verified unresolved read")
func attentionDeferralCanBeAnsweredLater() {
    var submissions = AttentionSubmissions()
    let key = AttentionSubmissions.key("activity-a","2")
    let first = submissions.begin(key)
    #expect(first)
    submissions.finish(key,receipt:"defer recorded",outcome:.recorded,deferred:true)
    #expect(submissions.blocked(key))
    submissions.verifiedRead(id:"activity-a",revision:"2")
    #expect(submissions.blocked(key))
    submissions.verifiedRead(id:"activity-a",revision:"2",unresolvedActivity:true)
    let newJudgment = submissions.begin(key)
    #expect(newJudgment)
    submissions.finish(key,receipt:"occurred recorded",outcome:.recorded)
    submissions.verifiedRead(id:"activity-a",revision:"2",unresolvedActivity:true)
    #expect(submissions.blocked(key))
    #expect(submissions.receiptHistory[key] == ["defer recorded","occurred recorded"])
    let memoryKey = AttentionSubmissions.key("memory-a", "2")
    let memoryStart = submissions.begin(memoryKey)
    #expect(memoryStart)
    submissions.finish(memoryKey, receipt: "unchanged later", outcome: .recorded, reviewLaterMembers: ["m1"])
    submissions.verifiedRead(id: "memory-b", revision: "2", reviewLaterMembers: ["m1"])
    submissions.verifiedRead(id: "memory-a", revision: "3", reviewLaterMembers: ["m1"])
    submissions.verifiedRead(id: "memory-a", revision: "2", reviewLaterMembers: ["m2"])
    #expect(submissions.blocked(memoryKey))
    submissions.verifiedRead(id: "memory-a", revision: "2", reviewLaterMembers: ["m1"])
    let laterDecision = submissions.begin(memoryKey)
    #expect(laterDecision)
    submissions.finish(memoryKey, receipt: "unknown later", outcome: .unknown, reviewLaterMembers: ["m1"])
    submissions.verifiedRead(id: "memory-a", revision: "2", reviewLaterMembers: ["m1"])
    #expect(submissions.blocked(memoryKey))
    #expect(submissions.receiptHistory[memoryKey] == ["unchanged later", "unknown later"])
}
#endif
