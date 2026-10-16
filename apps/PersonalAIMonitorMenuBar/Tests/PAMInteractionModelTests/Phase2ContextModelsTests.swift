import Foundation
#if canImport(XCTest)
import XCTest
@testable import PAMInteractionModel

final class Phase2ContextModelsTests: XCTestCase {
    private func autonomyItem(_ state: String = "waiting_evidence") -> [String: Any] {
        ["id": "local-key", "state": state, "summary": "核对旧版本", "kind": "memory",
         "source_revision": "a", "processed_revision": "a"]
    }
    func testOnlyOfferedBoundOptionsAreSelectableWithExactLabels() {
        let choices = FeedbackDecisionPresentation.choices([
            "choices": ["option_2", "option_1", "option_1", "option_3", "option_4", "defer"],
            "alternatives": ["保留现状", "采用新行为"]])
        XCTAssertEqual(choices.map(\.id), ["option_2", "option_1", "defer"])
        XCTAssertEqual(choices.map(\.label), ["采用新行为", "保留现状", "暂缓，保留待决策"])
        XCTAssertTrue(FeedbackDecisionPresentation.choices(["alternatives":["未正式提供的方案"]]).isEmpty)
        XCTAssertTrue(FeedbackDecisionPresentation.choices(["choices":["option_1"], "alternatives":[" "]]).isEmpty)
    }
    func testWaitingAndRecurrenceAreNotHumanChoices() {
        for state in ["waiting_evidence", "blocked", "recurred", "investigating", "resolved"] {
            XCTAssertFalse(FeedbackDecisionPresentation.requiresHuman(state: state))
            XCTAssertTrue(FeedbackDecisionPresentation.pendingDecisions([["id":"old", "status":"pending"]], state: state).isEmpty)
        }
        XCTAssertTrue(FeedbackDecisionPresentation.requiresHuman(state: "human_required"))
        XCTAssertEqual(FeedbackDecisionPresentation.pendingDecisions([["status":"pending"], ["status":"answered"]], state: "decision").count, 1)
    }
    func testArbitraryEvidenceAndResultAreNeverRendered() throws {
        var raw = autonomyItem()
        raw["evidence"] = [["raw_text":"private text", "memory_id":"private id", "summary":"来源版本已核对"]]
        raw["result"] = ["secret":"raw result", "summary":"保持未知", "changes":["未新增记忆"]]
        raw["project_id"] = "/Users/test/project"
        let parsed = try XCTUnwrap(AutonomyWorkItem(raw))
        XCTAssertEqual(parsed.evidenceSummaries, ["来源版本已核对"])
        XCTAssertEqual(parsed.resultSummary, "保持未知")
        XCTAssertEqual(parsed.projectLabel, "关联项目待核对")
        raw["evidence"] = ["private untyped evidence"]
        raw["result"] = "private untyped result"
        XCTAssertTrue(try XCTUnwrap(AutonomyWorkItem(raw)).evidenceSummaries.isEmpty)
        XCTAssertEqual(try XCTUnwrap(AutonomyWorkItem(raw)).resultSummary, "")
        raw["state"] = "verified"
        raw["result"] = ["action":"keep_active", "changes":"确认继续有效"]
        let applied = try XCTUnwrap(AutonomyWorkItem(raw))
        XCTAssertEqual(applied.changes, ["确认继续有效"])
        XCTAssertEqual(applied.nextAction, "")
        raw["result"] = ["action":"mark_legacy", "changes":["保留原文", "标为历史"]]
        XCTAssertEqual(try XCTUnwrap(AutonomyWorkItem(raw)).changes, ["保留原文", "标为历史"])
        raw["result"] = ["action":"supersede"]
        XCTAssertEqual(try XCTUnwrap(AutonomyWorkItem(raw)).changes, ["新建正确记忆并关联旧记录"])
        raw["state"] = "waiting_evidence"
        raw["result"] = ["action":"unknown_internal_action", "changes":"unknown_internal_change", "next_trigger":"memory_or_authoritative_source_changed"]
        let waiting = try XCTUnwrap(AutonomyWorkItem(raw))
        XCTAssertEqual(waiting.nextAction, "")
        XCTAssertEqual(waiting.changes, [])
        XCTAssertEqual(waiting.nextTrigger, "这条记忆或对应权威来源更新后继续核对。")
        raw["result"] = ["action":"keep_active", "next_trigger":"evidence_or_operator_recovery_changed"]
        let planned = try XCTUnwrap(AutonomyWorkItem(raw))
        XCTAssertEqual(planned.nextAction, "核对依据后确认这条记忆是否继续有效。")
        XCTAssertEqual(planned.changes, [])
        XCTAssertEqual(planned.nextTrigger, "依据更新或审核处理恢复后继续核对。")
        raw["result"] = ["action":"核对当前源码与结果。", "next_trigger":"unknown_internal_trigger"]
        let prose = try XCTUnwrap(AutonomyWorkItem(raw))
        XCTAssertEqual(prose.nextAction, "核对当前源码与结果。")
        XCTAssertEqual(prose.nextTrigger, "相关证据或处理状态变化后重新核对。")
    }
    func testChangedSourceInvalidatesVerifiedPresentation() throws {
        var raw = autonomyItem("verified")
        raw["source_revision"] = "b"
        raw["result"] = ["action":"keep_active", "changes":"确认继续有效"]
        let parsed = try XCTUnwrap(AutonomyWorkItem(raw))
        XCTAssertEqual(parsed.nextAction, "")
        XCTAssertEqual(parsed.changes, ["确认继续有效"])
        XCTAssertTrue(parsed.evidenceChanged)
        XCTAssertEqual(parsed.statusLabel, "证据已变化 · 等待重新核对")
        raw["state"] = "human_required"
        XCTAssertFalse(try XCTUnwrap(AutonomyWorkItem(raw)).requiresHuman)
    }
    func testMalformedSnapshotDoesNotBecomeZeroOrComplete() {
        XCTAssertNil(AutonomySnapshot(["enabled":true, "items":[], "counts":["verified":-1]]))
        XCTAssertNil(AutonomySnapshot(["enabled":true, "items":[], "counts":["verified":true]]))
        XCTAssertNil(AutonomySnapshot(["enabled":true, "items":[autonomyItem()], "counts":[:]]))
        XCTAssertNil(AutonomySnapshot(["enabled":true, "items":[autonomyItem(), autonomyItem()], "counts":["waiting_evidence":2]]))
        XCTAssertNil(AutonomyWorkItem(["id":"x", "state":"future_state"]))
        XCTAssertNil(AutonomySnapshot(["enabled":true]))
    }
    func testCountsAndPauseAreIndependentFromHumanDecisions() throws {
        let snapshot = try XCTUnwrap(AutonomySnapshot(["enabled":true, "paused":true, "items":[autonomyItem()],
            "counts":["waiting_evidence":17, "investigating":2, "blocked":1, "observing":3, "verified":1, "human_required":4]]))
        XCTAssertTrue(snapshot.paused)
        XCTAssertEqual(snapshot.processing, 2)
        XCTAssertEqual(snapshot.waiting, 18)
        XCTAssertTrue(snapshot.summary.contains("持续观察 3 件"))
        XCTAssertFalse(try XCTUnwrap(AutonomyWorkItem(autonomyItem())).requiresHuman)
    }
    func testResolvedDoesNotClaimNaturalEffectVerification() throws {
        let resolved = try XCTUnwrap(AutonomyWorkItem(autonomyItem("resolved")))
        let observing = try XCTUnwrap(AutonomyWorkItem(autonomyItem("observing")))
        XCTAssertTrue(resolved.limitation.contains("不等同于"))
        XCTAssertTrue(observing.limitation.contains("后续样本"))
    }

    private let refA = String(repeating: "a", count: 32)
    private let refB = String(repeating: "b", count: 32)
    private let refC = String(repeating: "c", count: 32)
    private let hashA = "sha256:" + String(repeating: "a", count: 64)

    func testActivityConfirmationGenerationFenceRejectsStaleLoadMorePage() {
        let fence = ActivityConfirmationGenerationFence(
            refreshGeneration: 4,
            mutationGeneration: 9
        )

        XCTAssertTrue(
            fence.allows(currentRefreshGeneration: 4, currentMutationGeneration: 9)
        )
        XCTAssertFalse(
            fence.allows(currentRefreshGeneration: 5, currentMutationGeneration: 9)
        )
        XCTAssertFalse(
            fence.allows(currentRefreshGeneration: 4, currentMutationGeneration: 10)
        )
    }

    func testFeatureFlagDefaultsOffAndRouteCommandKFocusesFind() {
        let suite = "PAMInteractionModelTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        XCTAssertFalse(ContextConsolePhase2FeatureFlag.isEnabled(in: defaults))
        defaults.set(true, forKey: ContextConsolePhase2FeatureFlag.key)
        XCTAssertFalse(ContextConsolePhase2FeatureFlag.isEnabled(in: defaults))
        XCTAssertTrue(ContextConsolePhase2FeatureFlag.maintenanceLocked)

        var route = Phase2RouteState(destination: .today)
        route.focusFind()
        XCTAssertEqual(route.destination, .find)
        XCTAssertEqual(route.searchFocusGeneration, 1)
        route.select(.legacy)
        XCTAssertEqual(route.destination, .legacy)
        route.select(.today)
        XCTAssertEqual(route.destination, .today)

        let enterFind = Phase2DestinationTransition(
            current: .today,
            requested: .find
        )
        XCTAssertTrue(enterFind.shouldBeginFindFlow)
        XCTAssertFalse(enterFind.shouldCancelFindBeforeSelection)
        let stayInFind = Phase2DestinationTransition(
            current: .find,
            requested: .find
        )
        XCTAssertFalse(stayInFind.shouldBeginFindFlow)
        XCTAssertFalse(stayInFind.shouldCancelFindBeforeSelection)
        let restartFind = Phase2DestinationTransition(
            current: .find,
            requested: .find,
            restartFind: true
        )
        XCTAssertTrue(restartFind.shouldBeginFindFlow)
        XCTAssertFalse(restartFind.shouldCancelFindBeforeSelection)
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
            XCTAssertTrue(
                leaveFind.shouldCancelFindBeforeSelection,
                "Every route away from Find must revoke capabilities before selecting \(destination)"
            )
            XCTAssertFalse(leaveFind.shouldBeginFindFlow)
            XCTAssertEqual(leaveFind.destination, destination)
        }
    }

    func testSystemHealthResolverUsesOneThreeStatePolicy() {
        func resolve(
            unknown: Bool = false,
            status: String = "running",
            danger: Bool = false,
            captureError: Bool = false,
            loaded: Bool = true,
            running: Bool = true,
            stale: Bool = false,
            privacyPause: Bool = false
        ) -> Phase2SystemHealth {
            Phase2SystemHealth.resolve(
                displayStateUnknown: unknown,
                status: status,
                statusIsDanger: danger,
                hasKnownCaptureError: captureError,
                launchAgentLoaded: loaded,
                launchAgentRunning: running,
                captureFreshnessIsStale: stale,
                captureBlockedBySession: privacyPause
            )
        }

        XCTAssertEqual(resolve(), .normal)
        XCTAssertEqual(resolve(unknown: true), .error)
        XCTAssertEqual(resolve(danger: true), .error)
        XCTAssertEqual(resolve(captureError: true), .error)
        XCTAssertEqual(resolve(running: false), .error)
        XCTAssertEqual(resolve(stale: true), .error)
        XCTAssertEqual(resolve(status: "paused"), .paused)
        XCTAssertEqual(resolve(status: "locked"), .paused)
        XCTAssertEqual(resolve(privacyPause: true), .paused)
        XCTAssertEqual(resolve(status: "stopped", loaded: false, running: false), .paused)
        XCTAssertEqual(resolve(status: "capturing", loaded: false, running: false), .normal)
        XCTAssertEqual(resolve(loaded: false, running: true), .error)
        XCTAssertEqual(resolve(status: "error"), .error)
        XCTAssertEqual(resolve(status: "session_state_unknown"), .error)
        XCTAssertEqual(resolve(status: "stopped"), .error)
        XCTAssertEqual(resolve(status: "unexpected_status"), .error)
        XCTAssertEqual(resolve(status: "paused", danger: true), .error)
        XCTAssertEqual(resolve(status: "locked", stale: true), .paused)
        XCTAssertEqual(resolve(danger: true, privacyPause: true), .error)
    }

    func testPresentationCopyNeverLeaksCoreReasonCodes() {
        let diagnostic = "private_internal_reason_42"
        for state in [
            Phase2ReadState.busy,
            .unknown,
            .unavailable,
            .blocked,
        ] {
            let copy = Phase2PresentationCopy.unavailableMessage(for: state)
            XCTAssertFalse(copy.contains(diagnostic))
            XCTAssertFalse(copy.contains("reason"))
            XCTAssertFalse(copy.contains("database_busy"))
            XCTAssertFalse(copy.localizedCaseInsensitiveContains("busy"))
            XCTAssertFalse(copy.contains("数据库"))
            XCTAssertFalse(copy.isEmpty)
        }
        XCTAssertTrue(
            Phase2PresentationCopy.unavailableMessage(for: .available).isEmpty
        )
    }

    func testQuickCaptureRequiresExplicitOwnerAndRejectsUnsafeText() {
        var draft = QuickCaptureDraft(text: "完成了 Phase 2 契约")
        XCTAssertEqual(draft.validationReason, "owner_confirmation_required")
        draft.explicitlyConfirmedOwner = true
        XCTAssertTrue(draft.isSubmissionReady)

        XCTAssertFalse(
            QuickCaptureDraft(
                text: "秘密 token=abc123456789012",
                explicitlyConfirmedOwner: true
            ).isSubmissionReady
        )
        XCTAssertFalse(
            QuickCaptureDraft(
                text: "查看 /" + "Users/example/private.txt",
                explicitlyConfirmedOwner: true
            ).isSubmissionReady
        )
        XCTAssertFalse(
            QuickCaptureDraft(
                text: "含有\u{202E}双向控制符",
                explicitlyConfirmedOwner: true
            ).isSubmissionReady
        )
        XCTAssertFalse(
            QuickCaptureDraft(
                text: String(repeating: "字", count: 181),
                explicitlyConfirmedOwner: true
            ).isSubmissionReady
        )
    }

    func testCoreCommandBoundaryParsesOnlyStdout() throws {
        let stdout = try jsonData(["status": "available"])
        let result = CoreCommandResult(
            exitCode: 0,
            standardOutput: stdout,
            standardError: Data(#"{"status":"private-diagnostic"}"#.utf8)
        )
        let decoded = try CoreCommandJSONBoundary.decodeObject(from: result)
        XCTAssertEqual(decoded["status"] as? String, "available")

        XCTAssertThrowsError(
            try CoreCommandJSONBoundary.decodeObject(
                from: CoreCommandResult(
                    exitCode: 1,
                    standardOutput: stdout,
                    standardError: Data()
                )
            )
        )
        XCTAssertThrowsError(
            try CoreCommandJSONBoundary.decodeObject(
                from: CoreCommandResult(
                    exitCode: 0,
                    standardOutput: Data(),
                    standardError: stdout
                )
            )
        )
    }

    func testSearchQueryTravelsOnlyThroughBoundedStdin() throws {
        let query = try ContextSnapshotQuery(query: #"PAM "%_ 找回"#, limit: 25)
        let command = try ContextConsoleCoreCommands.snapshot(query)

        XCTAssertEqual(command.arguments, ["context-console", "snapshot", "--payload-stdin"])
        XCTAssertFalse(command.arguments.joined(separator: " ").contains("PAM"))
        let payload = try XCTUnwrap(
            JSONSerialization.jsonObject(with: XCTUnwrap(command.standardInput)) as? [String: Any]
        )
        XCTAssertEqual(payload["query"] as? String, #"PAM "%_ 找回"#)
        XCTAssertEqual(payload["limit"] as? Int, 25)
        XCTAssertEqual(command.timeoutSeconds, 3)

        XCTAssertThrowsError(try ContextSnapshotQuery(query: "bad\u{200B}query"))
        XCTAssertThrowsError(try ContextSnapshotQuery(query: String(repeating: "x", count: 81)))
    }

    func testFindProjectAndNinetyDayFiltersUseExplicitStdinRange() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try XCTUnwrap(TimeZone(secondsFromGMT: 0))
        let formatter = ISO8601DateFormatter()
        let now = try XCTUnwrap(formatter.date(from: "2026-07-30T12:00:00Z"))
        let range = Phase2FindTimePreset.retentionWindow.utcRange(
            now: now,
            calendar: calendar
        )
        XCTAssertEqual(range.startAt, "2026-05-02T00:00:00Z")
        XCTAssertEqual(range.endAt, "2026-07-31T00:00:00Z")

        let projectRef = "project_" + String(repeating: "a", count: 32)
        let query = try ContextSnapshotQuery(
            projectRef: projectRef,
            startAt: range.startAt,
            endAt: range.endAt,
            limit: 100
        )
        let command = try ContextConsoleCoreCommands.snapshot(query)
        let payload = try XCTUnwrap(
            JSONSerialization.jsonObject(with: XCTUnwrap(command.standardInput)) as? [String: Any]
        )
        XCTAssertEqual(payload["project_ref"] as? String, projectRef)
        XCTAssertEqual(payload["start_at"] as? String, range.startAt)
        XCTAssertEqual(payload["end_at"] as? String, range.endAt)
        XCTAssertFalse(command.arguments.joined().contains(projectRef))
    }

    func testAvailableSnapshotDecodesSafePartitionsAndAtMostThreeQuestions() throws {
        let snapshot = try ContextSnapshotV1(data: try jsonData(availableSnapshot()))

        XCTAssertEqual(snapshot.readState, .available)
        XCTAssertTrue(snapshot.actionsEnabled)
        XCTAssertEqual(snapshot.projects?.count, 1)
        XCTAssertEqual(snapshot.projects?.first?.verifiedItems.first?.safeLabel, "Phase 2")
        XCTAssertEqual(snapshot.projects?.first?.compatibilityItems.first?.safeLabel, "旧确认安全摘要")
        XCTAssertEqual(snapshot.projects?.first?.clues.first?.safeLabel, "待核实屏幕线索")
        XCTAssertNil(snapshot.projects?.first?.nextStep)
        XCTAssertEqual(snapshot.bundle?.verifiedContexts.count, 1)
        XCTAssertEqual(snapshot.bundle?.unverifiedClues.count, 1)
        XCTAssertEqual(snapshot.confirmationCards?.count, 1)
        XCTAssertTrue(snapshot.confirmationCards?.first?.answerable == true)
        XCTAssertNil(snapshot.confirmationCards?.first?.project)
        XCTAssertEqual(
            snapshot.confirmationCards?.first?.revision,
            "rev_" + String(repeating: "a", count: 24)
        )
        XCTAssertEqual(snapshot.capabilities?.quickCapture, true)
    }

    func testSnapshotRejectsInformationInsufficientAndBoundsConfirmationInbox() throws {
        var forbidden = availableSnapshot()
        var questions = try XCTUnwrap(forbidden["confirmation_questions"] as? [[String: Any]])
        questions[0]["question"] = "information_insufficient"
        forbidden["confirmation_questions"] = questions
        XCTAssertThrowsError(try ContextSnapshotV1(data: try jsonData(forbidden)))

        var inbox = availableSnapshot()
        var repeated: [[String: Any]] = []
        for index in 0..<4 {
            var question = confirmationQuestion()
            question["confirmation_ref"] = "confirmation_" + String(format: "%032x", index + 1)
            repeated.append(question)
        }
        inbox["confirmation_questions"] = repeated
        inbox["counts"] = [
            "verified_contexts": 1,
            "unverified_clues": 1,
            "legacy_confirmed": 1,
            "pending_confirmations": 4,
        ]
        XCTAssertEqual(
            try ContextSnapshotV1(data: try jsonData(inbox)).confirmationCards?.count,
            4
        )

        var oversized = availableSnapshot()
        oversized["confirmation_questions"] = (0..<51).map { index in
            var question = confirmationQuestion()
            question["confirmation_ref"] =
                "confirmation_" + String(format: "%032x", index + 1)
            return question
        }
        oversized["counts"] = [
            "verified_contexts": 1,
            "unverified_clues": 1,
            "legacy_confirmed": 1,
            "pending_confirmations": 51,
        ]
        XCTAssertThrowsError(try ContextSnapshotV1(data: try jsonData(oversized)))
    }

    func testSnapshotRejectsRawAccessAndInternalIdentifiers() throws {
        var raw = availableSnapshot()
        raw["raw_access"] = true
        XCTAssertThrowsError(try ContextSnapshotV1(data: try jsonData(raw)))

        var internalField = availableSnapshot()
        internalField["verified_ref"] = "verified_" + refA
        XCTAssertThrowsError(try ContextSnapshotV1(data: try jsonData(internalField)))

        var diagnosticField = availableSnapshot()
        diagnosticField["reason_code"] = "database_busy"
        XCTAssertThrowsError(try ContextSnapshotV1(data: try jsonData(diagnosticField)))

        var internalValue = availableSnapshot()
        internalValue["leak"] = "observation_" + refA
        XCTAssertThrowsError(try ContextSnapshotV1(data: try jsonData(internalValue)))

        for unsafeValue in [
            "file:///" + "Users/example/private.txt",
            "/" + "Users/example/private.txt",
            "token=abcdefghijklmnopqrstuvwxyz",
            #"{"raw":"json"}"#,
            #"[1,2,3]"#,
        ] {
            var unsafe = availableSnapshot()
            unsafe["note"] = unsafeValue
            XCTAssertThrowsError(try ContextSnapshotV1(data: try jsonData(unsafe)))
        }
    }

    func testSnapshotOwnerActionsRequireEnvelopeAndCapability() throws {
        var envelopeOff = availableSnapshot()
        envelopeOff["actions_enabled"] = false
        XCTAssertFalse(
            try ContextSnapshotV1(data: try jsonData(envelopeOff)).actionsEnabled
        )

        var capabilityOff = availableSnapshot()
        var capabilities = try XCTUnwrap(
            capabilityOff["capabilities"] as? [String: Any]
        )
        capabilities["owner_action"] = false
        capabilityOff["capabilities"] = capabilities
        XCTAssertFalse(
            try ContextSnapshotV1(data: try jsonData(capabilityOff)).actionsEnabled
        )
    }

    func testBusySnapshotRequiresNullContent() throws {
        let busy: [String: Any] = [
            "contract_version": "ContextSnapshotV1",
            "status": "busy",
            "observed_at": "2026-07-30T01:00:00Z",
            "last_successful_read_at": "2026-07-30T00:59:00Z",
            "actions_enabled": false,
            "counts": NSNull(),
            "context_bundle_v1": NSNull(),
            "projects": NSNull(),
            "legacy_confirmed": NSNull(),
            "confirmation_questions": NSNull(),
            "capabilities": NSNull(),
            "raw_access": false,
        ]
        let decoded = try ContextSnapshotV1(data: try jsonData(busy))
        XCTAssertEqual(decoded.readState, .busy)
        XCTAssertNil(decoded.projects)
        XCTAssertFalse(decoded.actionsEnabled)

        var leaking = busy
        leaking["projects"] = []
        XCTAssertThrowsError(try ContextSnapshotV1(data: try jsonData(leaking)))
    }

    func testUnreadableTransitionClearsEveryTrustedSurface() throws {
        let readable = try ContextSnapshotV1(data: try jsonData(availableSnapshot()))
        let memory = try MemoryCandidateListV1(data: try jsonData(memoryCandidateList()))
        let delivery = try DeliveryStatusV1(data: try jsonData(deliveryStatus()))
        let handoff = try LocalHandoffV1(data: try jsonData(localHandoff()))
        let busy = try ContextSnapshotV1(data: try jsonData(busySnapshot()))

        var state = Phase2ConsoleViewState()
        let today = state.beginRequest(for: .today)
        XCTAssertTrue(state.apply(snapshot: readable, for: .today, token: today))
        let search = state.beginRequest(for: .search)
        XCTAssertTrue(state.apply(snapshot: readable, for: .search, token: search))
        let memoryToken = state.beginRequest(for: .memory)
        XCTAssertTrue(state.apply(memoryCandidates: memory, token: memoryToken))
        let deliveryToken = state.beginRequest(for: .delivery)
        XCTAssertTrue(state.apply(deliveryStatus: delivery, token: deliveryToken))
        let handoffToken = state.beginRequest(for: .handoff)
        XCTAssertTrue(state.apply(localHandoff: handoff, token: handoffToken))

        XCTAssertNotNil(state.snapshot)
        XCTAssertNotNil(state.searchSnapshot)
        XCTAssertNotNil(state.memoryCandidates)
        XCTAssertNotNil(state.deliveryStatus)
        XCTAssertNotNil(state.localHandoff)

        let refresh = state.beginRequest(for: .today)
        XCTAssertTrue(state.apply(snapshot: busy, for: .today, token: refresh))
        XCTAssertEqual(state.readState, .busy)
        XCTAssertFalse(state.actionsEnabled)
        XCTAssertNil(state.snapshot)
        XCTAssertNil(state.searchSnapshot)
        XCTAssertNil(state.memoryCandidates)
        XCTAssertNil(state.deliveryStatus)
        XCTAssertNil(state.localHandoff)
    }

    func testUnreadableHandoffClearsEveryTrustedSurface() throws {
        let readable = try ContextSnapshotV1(data: try jsonData(availableSnapshot()))
        let unavailableHandoff: [String: Any] = [
            "contract_version": "LocalHandoffV1",
            "status": "busy",
            "observed_at": "2026-08-01T07:00:00Z",
            "last_successful_read_at": "2026-08-01T06:59:00Z",
            "handoff_ref": NSNull(),
            "bundle_hash": NSNull(),
            "project": NSNull(),
            "verified_contexts": NSNull(),
            "unresolved_questions": NSNull(),
            "expires_at": NSNull(),
            "revocation_state": NSNull(),
            "raw_access": false,
        ]
        let handoff = try LocalHandoffV1(data: try jsonData(unavailableHandoff))
        var state = Phase2ConsoleViewState()
        let today = state.beginRequest(for: .today)
        XCTAssertTrue(state.apply(snapshot: readable, for: .today, token: today))
        XCTAssertTrue(state.actionsEnabled)
        let handoffToken = state.beginRequest(for: .handoff)
        XCTAssertTrue(state.apply(localHandoff: handoff, token: handoffToken))
        XCTAssertEqual(state.readState, .busy)
        XCTAssertFalse(state.actionsEnabled)
        XCTAssertNil(state.snapshot)
        XCTAssertNil(state.searchSnapshot)
        XCTAssertNil(state.memoryCandidates)
        XCTAssertNil(state.deliveryStatus)
        XCTAssertNil(state.localHandoff)
    }

    func testIndependentRuntimeTrustLossClearsAndInvalidatesOldResponses() throws {
        let readable = try ContextSnapshotV1(data: try jsonData(availableSnapshot()))
        var state = Phase2ConsoleViewState()
        let today = state.beginRequest(for: .today)
        XCTAssertTrue(state.apply(snapshot: readable, for: .today, token: today))
        let responseStartedBeforeTrustLoss = state.beginRequest(for: .search)

        XCTAssertTrue(
            state.applyRuntimeTrustLoss(
                .unavailable,
                observedAt: "2026-08-01T07:01:00Z"
            )
        )
        XCTAssertEqual(state.readState, .unavailable)
        XCTAssertFalse(state.actionsEnabled)
        XCTAssertNil(state.snapshot)
        XCTAssertNil(state.searchSnapshot)
        XCTAssertFalse(
            state.apply(
                snapshot: readable,
                for: .search,
                token: responseStartedBeforeTrustLoss
            )
        )
        XCTAssertFalse(
            state.applyRuntimeTrustLoss(
                .available,
                observedAt: "2026-08-01T07:02:00Z"
            ),
            "runtime recovery must not repopulate old content without a fresh Context read"
        )
        XCTAssertNil(state.snapshot)
        let freshToday = state.beginRequest(for: .today)
        XCTAssertTrue(state.apply(snapshot: readable, for: .today, token: freshToday))
        XCTAssertEqual(state.readState, .available)
        XCTAssertNotNil(state.snapshot)
    }

    func testStaleAndCancelledResponsesCannotOverwriteNewerState() throws {
        let readable = try ContextSnapshotV1(data: try jsonData(availableSnapshot()))
        let busy = try ContextSnapshotV1(data: try jsonData(busySnapshot()))
        var state = Phase2ConsoleViewState()

        let old = state.beginRequest(for: .search)
        let current = state.beginRequest(for: .search)
        XCTAssertFalse(state.apply(snapshot: readable, for: .search, token: old))
        XCTAssertTrue(state.apply(snapshot: busy, for: .search, token: current))
        XCTAssertEqual(state.readState, .busy)
        XCTAssertNil(state.searchSnapshot)

        let cancelled = state.beginRequest(for: .search)
        XCTAssertTrue(state.cancel(cancelled))
        XCTAssertFalse(state.apply(snapshot: readable, for: .search, token: cancelled))
        XCTAssertNil(state.searchSnapshot)
    }

    func testCrossChannelInvalidationBlocksOldSearchAndHandoffResponses() throws {
        let readable = try ContextSnapshotV1(data: try jsonData(availableSnapshot()))
        let busy = try ContextSnapshotV1(data: try jsonData(busySnapshot()))
        let handoffPayload = try LocalHandoffV1(data: try jsonData(localHandoff()))

        var state = Phase2ConsoleViewState()
        let oldSearch = state.beginRequest(for: .search)
        state.invalidateAllRequests()
        let refresh = state.beginRequest(for: .today)
        XCTAssertTrue(state.apply(snapshot: busy, for: .today, token: refresh))
        XCTAssertFalse(state.apply(snapshot: readable, for: .search, token: oldSearch))
        XCTAssertEqual(state.readState, .busy)
        XCTAssertNil(state.searchSnapshot)

        var searchState = Phase2ConsoleViewState()
        let today = searchState.beginRequest(for: .today)
        XCTAssertTrue(searchState.apply(snapshot: readable, for: .today, token: today))
        let oldHandoff = searchState.beginRequest(for: .handoff)
        searchState.invalidateRequests([.handoff])
        let newSearch = searchState.beginRequest(for: .search)
        XCTAssertTrue(
            searchState.apply(snapshot: readable, for: .search, token: newSearch)
        )
        XCTAssertFalse(
            searchState.apply(localHandoff: handoffPayload, token: oldHandoff)
        )
        XCTAssertNil(searchState.localHandoff)
        XCTAssertNotNil(searchState.searchSnapshot)
    }

    func testMemoryCandidatesAreSummaryOnlyAndWriterDisabled() throws {
        let decoded = try MemoryCandidateListV1(data: try jsonData(memoryCandidateList()))
        XCTAssertEqual(decoded.readState, .available)
        XCTAssertFalse(decoded.actionsEnabled)
        XCTAssertEqual(decoded.candidates?.first?.safeSummary, "PAM Phase 2 决策仍然有效")
        XCTAssertFalse(decoded.candidates?.first?.revoked == true)

        var invalid = memoryCandidateList()
        var candidates = try XCTUnwrap(invalid["candidates"] as? [[String: Any]])
        candidates[0]["writer_enabled"] = true
        invalid["candidates"] = candidates
        XCTAssertThrowsError(try MemoryCandidateListV1(data: try jsonData(invalid)))
    }

    func testDeliveryRequiresReceiptTimestampForTerminalStage() throws {
        let decoded = try DeliveryStatusV1(data: try jsonData(deliveryStatus()))
        XCTAssertEqual(decoded.dailyReport?.currentState, .delivered)
        XCTAssertEqual(decoded.projects?.first?.stage, .delivered)
        XCTAssertEqual(decoded.projects?.first?.withdrawn, false)

        var invalid = deliveryStatus()
        var deliveries = try XCTUnwrap(invalid["deliveries"] as? [[String: Any]])
        deliveries[0]["delivered_at"] = NSNull()
        invalid["deliveries"] = deliveries
        XCTAssertThrowsError(try DeliveryStatusV1(data: try jsonData(invalid)))

        var withdrawn = deliveryStatus()
        var withdrawnDeliveries = try XCTUnwrap(
            withdrawn["deliveries"] as? [[String: Any]]
        )
        withdrawnDeliveries[0]["withdrawal_state"] = "restricted"
        withdrawn["deliveries"] = withdrawnDeliveries
        let withdrawnDecoded = try DeliveryStatusV1(data: try jsonData(withdrawn))
        XCTAssertEqual(withdrawnDecoded.projects?.first?.withdrawn, true)
    }

    func testLocalHandoffContainsOnlyVerifiedSources() throws {
        let decoded = try LocalHandoffV1(data: try jsonData(localHandoff()))
        XCTAssertEqual(decoded.readState, .available)
        XCTAssertEqual(decoded.results, [])
        XCTAssertEqual(decoded.nextSteps, [])
        XCTAssertEqual(decoded.sources?.first?.contextRef, "context_" + refA)
        XCTAssertEqual(decoded.unresolvedQuestions, ["确认 UI 是否足够高密度"])

        var internalLeak = localHandoff()
        internalLeak["receipt_ref"] = "receipt_" + refA
        XCTAssertThrowsError(try LocalHandoffV1(data: try jsonData(internalLeak)))
    }

    func testSemanticHandoffRejectionDoesNotClearTrustedConsoleState() throws {
        let readable = try ContextSnapshotV1(data: try jsonData(availableSnapshot()))
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
        let localRejection = try LocalHandoffV1(data: try jsonData(rejected))
        XCTAssertEqual(localRejection.readState, .blocked)

        var state = Phase2ConsoleViewState()
        let today = state.beginRequest(for: .today)
        XCTAssertTrue(state.apply(snapshot: readable, for: .today, token: today))
        let existingHandoff = try LocalHandoffV1(data: try jsonData(localHandoff()))
        let initialHandoff = state.beginRequest(for: .handoff)
        XCTAssertTrue(state.apply(localHandoff: existingHandoff, token: initialHandoff))
        XCTAssertNotNil(state.localHandoff)
        let rejectedHandoff = state.beginRequest(for: .handoff)
        XCTAssertTrue(state.apply(localHandoff: localRejection, token: rejectedHandoff))
        XCTAssertEqual(state.readState, .available)
        XCTAssertNotNil(state.snapshot)
        XCTAssertNil(state.localHandoff)
    }

    func testOwnerActionAndFeedbackBodiesNeverEnterArguments() throws {
        let action = OwnerContextActionV1(
            action: .manualNote,
            idempotencyKey: "idem_" + refA,
            explicitlyConfirmedOwner: true,
            safeLabel: "完成 Phase 2 模型",
            outcome: "completed",
            startedAt: "2026-07-30T01:00:00Z",
            endedAt: "2026-07-30T01:05:00Z",
            projectRef: "project_" + refA,
            projectLabel: "PAM"
        )
        let command = try action.coreCommandRequest()
        XCTAssertEqual(command.arguments, ["context-console", "owner-action", "--payload-stdin"])
        XCTAssertFalse(command.arguments.joined().contains("完成 Phase 2"))
        let payload = try XCTUnwrap(
            JSONSerialization.jsonObject(with: XCTUnwrap(command.standardInput)) as? [String: Any]
        )
        XCTAssertEqual(payload["confirmation_action"] as? String, "CONFIRM_OWNER_CONTEXT_ACTION")
        XCTAssertEqual(payload["safe_label"] as? String, "完成 Phase 2 模型")
        XCTAssertEqual(payload["project_label"] as? String, "PAM")
        XCTAssertNil(payload["context_state"])

        let confirmationAction = OwnerContextActionV1(
            action: .confirmCard,
            idempotencyKey: "idem_" + refB,
            explicitlyConfirmedOwner: true,
            confirmationRef: "confirmation_" + refA,
            expectedRevision: "rev_" + String(repeating: "a", count: 24),
            answer: .occurred,
            reason: "correct"
        )
        let confirmationCommand = try confirmationAction.coreCommandRequest()
        let confirmationPayload = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: try XCTUnwrap(confirmationCommand.standardInput)
            ) as? [String: Any]
        )
        XCTAssertEqual(
            confirmationPayload["expected_revision"] as? String,
            "rev_" + String(repeating: "a", count: 24)
        )
        XCTAssertNil(confirmationPayload["project_ref"])

        let unclassified = OwnerContextActionV1(
            action: .manualNote,
            idempotencyKey: "idem_" + refC,
            explicitlyConfirmedOwner: true,
            safeLabel: "空库补记",
            outcome: "unknown",
            startedAt: "2026-07-30T01:00:00Z",
            endedAt: "2026-07-30T01:01:00Z",
            projectLabel: "未归类"
        )
        let unclassifiedPayload = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: try XCTUnwrap(unclassified.coreCommandRequest().standardInput)
            ) as? [String: Any]
        )
        XCTAssertNil(unclassifiedPayload["project_ref"])
        XCTAssertEqual(unclassifiedPayload["project_label"] as? String, "未归类")

        let feedback = ProductFeedbackRequestV1(
            idempotencyKey: "idem_" + refB,
            sourceKind: .localHandoff,
            sourceRef: "bundle_" + refA,
            projectRef: "project_" + refA,
            useful: false,
            resumeSeconds: 12,
            experimentRef: "experiment_" + refC
        )
        let feedbackCommand = try ContextConsoleCoreCommands.productFeedback(feedback)
        XCTAssertEqual(
            feedbackCommand.arguments,
            ["context-console", "product-feedback", "--payload-stdin"]
        )
        XCTAssertNotNil(feedbackCommand.standardInput)
        let feedbackPayload = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: try XCTUnwrap(feedbackCommand.standardInput)
            ) as? [String: Any]
        )
        XCTAssertEqual(feedbackPayload["resume_seconds"] as? Double, 12)
        XCTAssertEqual(
            feedbackPayload["experiment_ref"] as? String,
            "experiment_" + refC
        )
    }

    func testNegativeFeedbackCannotClaimAWin() throws {
        let negative: [String: Any] = [
            "contract_version": "ProductFeedbackV1",
            "status": "accepted",
            "feedback_ref": "feedback_" + refA,
            "source_kind": "bundle",
            "source_ref": "bundle_" + refA,
            "useful": false,
            "win_recorded": false,
            "experiment_recorded": true,
            "raw_access": false,
        ]
        let decoded = try ProductFeedbackV1(data: try jsonData(negative))
        XCTAssertFalse(decoded.winRecorded)
        XCTAssertTrue(decoded.experimentRecorded)

        var impossible = negative
        impossible["win_recorded"] = true
        XCTAssertThrowsError(try ProductFeedbackV1(data: try jsonData(impossible)))
    }

    func testProductExperimentStatusIsStrictPublicProjection() throws {
        let running = try ProductExperimentStatusV1(
            data: try jsonData(productExperimentStatus())
        )
        XCTAssertEqual(running.readState, .available)
        XCTAssertEqual(running.phase, .collecting)
        XCTAssertEqual(running.runningExperimentRef, "experiment_" + refC)
        XCTAssertEqual(running.attemptCount, 3)
        XCTAssertEqual(running.completeAttemptCount, 3)
        XCTAssertEqual(running.usefulRate, 2.0 / 3.0)
        XCTAssertEqual(running.medianResumeSeconds, 12.5)

        let command = ContextConsoleCoreCommands.productExperimentStatus()
        XCTAssertEqual(command.arguments, ["context-console", "experiment-status"])
        XCTAssertNil(command.standardInput)
        XCTAssertFalse(command.arguments.joined().contains(running.runningExperimentRef!))

        var diagnosticLeak = productExperimentStatus()
        diagnosticLeak["reason_code"] = "minimum_window_not_reached"
        XCTAssertThrowsError(
            try ProductExperimentStatusV1(data: try jsonData(diagnosticLeak))
        )

        var privateLeak = productExperimentStatus()
        privateLeak["verified_ref"] = "verified_" + refA
        XCTAssertThrowsError(
            try ProductExperimentStatusV1(data: try jsonData(privateLeak))
        )

        var terminal = productExperimentStatus()
        terminal["status"] = "passed"
        terminal["experiment_ref"] = NSNull()
        terminal["attempt_count"] = 10
        terminal["complete_attempt_count"] = 10
        terminal["useful_rate"] = 0.8
        terminal["median_resume_seconds"] = 20.0
        let terminalStatus = try ProductExperimentStatusV1(
            data: try jsonData(terminal)
        )
        XCTAssertEqual(terminalStatus.phase, .passed)
        XCTAssertNil(terminalStatus.experimentRef)
        XCTAssertNil(terminalStatus.runningExperimentRef)

        terminal["experiment_ref"] = "experiment_" + refC
        XCTAssertThrowsError(
            try ProductExperimentStatusV1(data: try jsonData(terminal))
        )

        var fabricatedTerminal = terminal
        fabricatedTerminal["experiment_ref"] = NSNull()
        fabricatedTerminal["attempt_count"] = 0
        fabricatedTerminal["complete_attempt_count"] = 0
        fabricatedTerminal["useful_rate"] = NSNull()
        fabricatedTerminal["median_resume_seconds"] = NSNull()
        XCTAssertThrowsError(
            try ProductExperimentStatusV1(data: try jsonData(fabricatedTerminal))
        )

        var prematureTerminal = terminal
        prematureTerminal["experiment_ref"] = NSNull()
        prematureTerminal["attempt_count"] = 9
        prematureTerminal["complete_attempt_count"] = 9
        XCTAssertThrowsError(
            try ProductExperimentStatusV1(data: try jsonData(prematureTerminal))
        )

        var partialWithCompleteMetrics = productExperimentStatus()
        partialWithCompleteMetrics["complete_attempt_count"] = 2
        XCTAssertThrowsError(
            try ProductExperimentStatusV1(data: try jsonData(partialWithCompleteMetrics))
        )

        var inventedMetrics = productExperimentStatus()
        inventedMetrics["attempt_count"] = 0
        inventedMetrics["complete_attempt_count"] = 0
        inventedMetrics["useful_rate"] = 1.0
        XCTAssertThrowsError(
            try ProductExperimentStatusV1(data: try jsonData(inventedMetrics))
        )
    }

    func testUnavailableProductExperimentCannotRetainBinding() throws {
        let busy: [String: Any] = [
            "contract_version": "ProductExperimentStatusV1",
            "read_state": "busy",
            "observed_at": "2026-08-01T06:00:00Z",
            "status": NSNull(),
            "experiment_started": NSNull(),
            "experiment_ref": NSNull(),
            "attempt_count": NSNull(),
            "complete_attempt_count": NSNull(),
            "useful_rate": NSNull(),
            "median_resume_seconds": NSNull(),
            "raw_access": false,
        ]
        let decoded = try ProductExperimentStatusV1(data: try jsonData(busy))
        XCTAssertEqual(decoded.readState, .busy)
        XCTAssertNil(decoded.runningExperimentRef)

        var leaking = busy
        leaking["experiment_ref"] = "experiment_" + refC
        XCTAssertThrowsError(try ProductExperimentStatusV1(data: try jsonData(leaking)))

        let running = try ProductExperimentStatusV1(
            data: try jsonData(productExperimentStatus())
        )
        var state = Phase2ConsoleViewState()
        let trusted = try ContextSnapshotV1(data: try jsonData(availableSnapshot()))
        let today = state.beginRequest(for: .today)
        XCTAssertTrue(state.apply(snapshot: trusted, for: .today, token: today))
        XCTAssertNotNil(state.snapshot)
        let old = state.beginRequest(for: .experiment)
        let current = state.beginRequest(for: .experiment)
        XCTAssertFalse(state.apply(productExperimentStatus: running, token: old))
        XCTAssertTrue(state.apply(productExperimentStatus: running, token: current))
        XCTAssertEqual(
            state.productExperimentStatus?.runningExperimentRef,
            "experiment_" + refC
        )
        let unavailable = state.beginRequest(for: .experiment)
        XCTAssertTrue(state.apply(productExperimentStatus: decoded, token: unavailable))
        XCTAssertEqual(state.readState, .busy)
        XCTAssertNil(state.snapshot)
        XCTAssertNil(state.searchSnapshot)
        XCTAssertNil(state.memoryCandidates)
        XCTAssertNil(state.deliveryStatus)
        XCTAssertNil(state.productExperimentStatus)

        var availableUnknown = busy
        availableUnknown["read_state"] = "available"
        availableUnknown["status"] = "unknown"
        let unknown = try ProductExperimentStatusV1(
            data: try jsonData(availableUnknown)
        )
        let refreshedToday = state.beginRequest(for: .today)
        XCTAssertTrue(state.apply(snapshot: trusted, for: .today, token: refreshedToday))
        let unknownToken = state.beginRequest(for: .experiment)
        XCTAssertTrue(state.apply(productExperimentStatus: unknown, token: unknownToken))
        XCTAssertEqual(state.readState, .unknown)
        XCTAssertNil(state.snapshot)
        XCTAssertFalse(state.actionsEnabled)
    }

    func testProductExperimentTransportFailureClearsEveryTrustedSurface() throws {
        let trusted = try ContextSnapshotV1(data: try jsonData(availableSnapshot()))
        var state = Phase2ConsoleViewState()
        let today = state.beginRequest(for: .today)
        XCTAssertTrue(state.apply(snapshot: trusted, for: .today, token: today))
        let search = state.beginRequest(for: .search)
        XCTAssertTrue(state.apply(snapshot: trusted, for: .search, token: search))
        let memory = state.beginRequest(for: .memory)
        XCTAssertTrue(
            state.apply(
                memoryCandidates: MemoryCandidateListV1(
                    readState: .available,
                    observedAt: "2026-08-01T06:00:30Z",
                    candidates: [],
                    actionsEnabled: false
                ),
                token: memory
            )
        )
        let delivery = state.beginRequest(for: .delivery)
        XCTAssertTrue(
            state.apply(
                deliveryStatus: DeliveryStatusV1(
                    readState: .available,
                    observedAt: "2026-08-01T06:00:30Z",
                    dailyReport: nil,
                    deliveries: [],
                    projects: []
                ),
                token: delivery
            )
        )
        let staleSearch = state.beginRequest(for: .search)
        let experiment = state.beginRequest(for: .experiment)

        // This is the state transition used when the experiment-status service
        // throws (timeout, process error, or invalid JSON). It must be global,
        // because refresh must not continue with trusted data after that error.
        XCTAssertTrue(
            state.applyFailure(
                .unavailable,
                observedAt: "2026-08-01T06:01:00Z",
                token: experiment
            )
        )
        XCTAssertEqual(state.readState, .unavailable)
        XCTAssertFalse(state.actionsEnabled)
        XCTAssertNil(state.snapshot)
        XCTAssertNil(state.searchSnapshot)
        XCTAssertNil(state.memoryCandidates)
        XCTAssertNil(state.deliveryStatus)
        XCTAssertNil(state.productExperimentStatus)
        XCTAssertNil(state.localHandoff)
        XCTAssertFalse(
            state.apply(snapshot: trusted, for: .search, token: staleSearch),
            "a response started before the experiment transport failure must stay invalid"
        )
    }

    func testResumeTimerUsesMonotonicGenerationAndExactBindings() {
        XCTAssertTrue(Phase2ProductFeedbackChoice.useful.useful)
        XCTAssertEqual(Phase2ProductFeedbackChoice.useful.title, "有用，继续工作")
        XCTAssertFalse(Phase2ProductFeedbackChoice.notUseful.useful)
        XCTAssertEqual(Phase2ProductFeedbackChoice.notUseful.title, "没帮助")

        let projectA = "project_" + refA
        let projectB = "project_" + refB
        let sourceA = "handoff_" + refA
        var timer = Phase2ResumeTimerState()

        let commandKGeneration = timer.beginFindEntry(atUptimeNanoseconds: 500_000_000)
        XCTAssertTrue(timer.isActive)
        XCTAssertEqual(
            timer.beginSearch(atUptimeNanoseconds: 1_500_000_000),
            commandKGeneration
        )
        XCTAssertTrue(timer.select(projectRef: projectA))
        XCTAssertEqual(
            timer.freeze(
                sourceRef: sourceA,
                projectRef: projectA,
                atUptimeNanoseconds: 2_500_000_000,
                generation: commandKGeneration
            )?.resumeSeconds,
            2
        )
        XCTAssertFalse(timer.isActive)

        let firstGeneration = timer.begin(atUptimeNanoseconds: 1_000_000_000)
        XCTAssertTrue(timer.select(projectRef: projectA))
        let measurement = timer.freeze(
            sourceRef: sourceA,
            projectRef: projectA,
            atUptimeNanoseconds: 4_250_000_000,
            generation: firstGeneration
        )
        XCTAssertEqual(measurement?.sourceRef, sourceA)
        XCTAssertEqual(measurement?.projectRef, projectA)
        XCTAssertEqual(measurement?.resumeSeconds, 3.25)

        let staleGeneration = timer.beginFindEntry(atUptimeNanoseconds: 10_000_000_000)
        XCTAssertEqual(
            timer.beginSearch(atUptimeNanoseconds: 11_000_000_000),
            staleGeneration
        )
        let currentGeneration = timer.beginSearch(atUptimeNanoseconds: 20_000_000_000)
        XCTAssertNotEqual(currentGeneration, staleGeneration)
        XCTAssertTrue(timer.select(projectRef: projectA))
        XCTAssertNil(
            timer.freeze(
                sourceRef: sourceA,
                projectRef: projectA,
                atUptimeNanoseconds: 21_000_000_000,
                generation: staleGeneration
            )
        )
        XCTAssertNotNil(
            timer.freeze(
                sourceRef: sourceA,
                projectRef: projectA,
                atUptimeNanoseconds: 22_000_000_000,
                generation: currentGeneration
            )
        )

        _ = timer.begin(atUptimeNanoseconds: 30_000_000_000)
        XCTAssertTrue(timer.select(projectRef: projectA))
        XCTAssertFalse(timer.select(projectRef: projectB))
        XCTAssertNil(timer.measurement)
        XCTAssertNil(
            timer.freeze(
                sourceRef: sourceA,
                projectRef: projectB,
                atUptimeNanoseconds: 31_000_000_000,
                generation: timer.generation
            )
        )

        var resultSelection = Phase2SearchResultSelectionState()
        resultSelection.accept(
            timerGeneration: staleGeneration,
            projectRefs: [projectA]
        )
        XCTAssertTrue(
            resultSelection.canSelect(
                timerGeneration: staleGeneration,
                projectRef: projectA
            )
        )
        resultSelection.beginSearch()
        XCTAssertFalse(
            resultSelection.canSelect(
                timerGeneration: currentGeneration,
                projectRef: projectA
            ),
            "starting a replacement search must disable every older result card"
        )
        resultSelection.accept(
            timerGeneration: currentGeneration,
            projectRefs: [projectB]
        )
        XCTAssertFalse(
            resultSelection.canSelect(
                timerGeneration: currentGeneration,
                projectRef: projectA
            ),
            "a delayed click from Search A must not bind Search B's timer"
        )
        XCTAssertTrue(
            resultSelection.canSelect(
                timerGeneration: currentGeneration,
                projectRef: projectB
            )
        )
    }

    func testRootRefreshGenerationRejectsOlderRequestChains() {
        var generation = Phase2RefreshGenerationState()
        let first = generation.begin()
        XCTAssertTrue(generation.isCurrent(first))
        let second = generation.begin()
        XCTAssertFalse(generation.isCurrent(first))
        XCTAssertTrue(generation.isCurrent(second))
    }

    func testQueuedPreviewFeedbackAuthorizationCannotCallServiceForReplacementHandoff() throws {
        let sourceA = "handoff_" + refA
        let projectA = "project_" + refA
        let sourceB = "handoff_" + refB
        let projectB = "project_" + refB
        var authorization = Phase2FeedbackActionAuthorizationState()

        let previewA = try XCTUnwrap(
            authorization.bind(
                sourceRef: sourceA,
                projectRef: projectA,
                timerGeneration: 41
            )
        )
        // Switching projects synchronously revokes Preview A before any
        // queued SwiftUI Task is allowed to reach the service boundary.
        authorization.invalidate()
        let previewB = try XCTUnwrap(
            authorization.bind(
                sourceRef: sourceB,
                projectRef: projectB,
                timerGeneration: 42
            )
        )

        var serviceCalls = 0
        func attemptServiceCall(
            expected: Phase2FeedbackActionAuthorization,
            currentSourceRef: String,
            currentProjectRef: String,
            currentTimerGeneration: UInt64
        ) {
            guard authorization.authorizes(
                expected,
                currentSourceRef: currentSourceRef,
                currentProjectRef: currentProjectRef,
                currentTimerGeneration: currentTimerGeneration
            ) else { return }
            serviceCalls += 1
        }

        attemptServiceCall(
            expected: previewA,
            currentSourceRef: sourceB,
            currentProjectRef: projectB,
            currentTimerGeneration: 42
        )
        XCTAssertEqual(
            serviceCalls,
            0,
            "Preview A's old closure must make zero service calls after Preview B is bound"
        )

        attemptServiceCall(
            expected: previewB,
            currentSourceRef: sourceB,
            currentProjectRef: projectB,
            currentTimerGeneration: 42
        )
        XCTAssertEqual(serviceCalls, 1)

        // A replacement search of the same project/source still receives a
        // new monotonic generation, so the old button remains unauthorized.
        let replacement = try XCTUnwrap(
            authorization.bind(
                sourceRef: sourceB,
                projectRef: projectB,
                timerGeneration: 43
            )
        )
        attemptServiceCall(
            expected: previewB,
            currentSourceRef: sourceB,
            currentProjectRef: projectB,
            currentTimerGeneration: 43
        )
        XCTAssertEqual(serviceCalls, 1)
        XCTAssertNotEqual(replacement, previewB)

        authorization.invalidate()
        attemptServiceCall(
            expected: replacement,
            currentSourceRef: sourceB,
            currentProjectRef: projectB,
            currentTimerGeneration: 43
        )
        XCTAssertEqual(serviceCalls, 1, "cancel must synchronously revoke the current buttons")

        XCTAssertNil(
            Phase2FeedbackActionAuthorization(
                sourceRef: "verified_" + refA,
                projectRef: projectA,
                timerGeneration: 44
            ),
            "internal/non-public reference families must never become UI authorizations"
        )
    }

    func testFeedbackSubmissionCheckpointSurvivesNavigationAndStalePresentationToken() throws {
        let running = try ProductExperimentStatusV1(
            data: try jsonData(productExperimentStatus())
        )
        let first = ProductFeedbackRequestV1(
            idempotencyKey: "idem_" + refA,
            sourceKind: .localHandoff,
            sourceRef: "handoff_" + refA,
            projectRef: "project_" + refA,
            useful: true,
            resumeSeconds: 8,
            experimentRef: "experiment_" + refC
        )
        let replacement = ProductFeedbackRequestV1(
            idempotencyKey: "idem_" + refB,
            sourceKind: .localHandoff,
            sourceRef: "handoff_" + refB,
            projectRef: "project_" + refB,
            useful: false,
            resumeSeconds: 9,
            experimentRef: "experiment_" + refC
        )

        var binding = Phase2ExperimentFeedbackBindingState()
        XCTAssertTrue(binding.applyExperimentStatus(running))
        XCTAssertEqual(binding.requestForSubmission(first, choice: .useful), first)
        XCTAssertTrue(binding.submissionInFlight)

        // Equivalent to a delayed service call while Find navigates: only the
        // presentation token, timer and button capability are invalidated.
        var viewState = Phase2ConsoleViewState()
        let oldFeedbackToken = viewState.beginRequest(for: .feedback)
        var timer = Phase2ResumeTimerState()
        _ = timer.begin(atUptimeNanoseconds: 1_000_000_000)
        var authorization = Phase2FeedbackActionAuthorizationState()
        _ = authorization.bind(
            sourceRef: first.sourceRef,
            projectRef: first.projectRef,
            timerGeneration: timer.generation
        )
        viewState.invalidateRequests([.feedback, .handoff])
        timer.cancel()
        authorization.invalidate()

        XCTAssertFalse(viewState.cancel(oldFeedbackToken))
        XCTAssertTrue(binding.submissionInFlight)
        XCTAssertEqual(binding.pendingRetry?.request, first)
        XCTAssertFalse(binding.canSubmit(sourceRef: replacement.sourceRef))
        XCTAssertNil(
            binding.requestForSubmission(replacement, choice: .notUseful),
            "navigation must not open another source while Core commit status is unresolved"
        )

        // The delayed recorded response remains authoritative even though its
        // old presentation token is stale.
        XCTAssertTrue(binding.markRecordedAwaitingStatusRefresh(request: first))
        XCTAssertTrue(binding.statusRefreshPending)
        XCTAssertFalse(binding.canSubmit(sourceRef: replacement.sourceRef))

        let staleStatus = viewState.beginRequest(for: .experiment)
        let currentStatus = viewState.beginRequest(for: .experiment)
        XCTAssertFalse(
            viewState.apply(productExperimentStatus: running, token: staleStatus)
        )
        XCTAssertTrue(binding.statusRefreshPending)
        XCTAssertTrue(
            viewState.apply(productExperimentStatus: running, token: currentStatus)
        )
        XCTAssertTrue(binding.applyExperimentStatus(running))
        XCTAssertFalse(binding.statusRefreshPending)
        XCTAssertTrue(binding.canSubmit(sourceRef: replacement.sourceRef))

        // A transport-unknown submission also survives navigation. A same
        // source/same choice action returns the original bytes, even if the new
        // Find measurement differs; every other source remains blocked.
        var retryBinding = Phase2ExperimentFeedbackBindingState()
        XCTAssertTrue(retryBinding.applyExperimentStatus(running))
        XCTAssertEqual(retryBinding.requestForSubmission(first, choice: .useful), first)
        XCTAssertTrue(retryBinding.markTransportUnknown(request: first))
        XCTAssertEqual(retryBinding.submissionCheckpoint, .transportUnknown)
        XCTAssertFalse(retryBinding.canSubmit(sourceRef: replacement.sourceRef))
        XCTAssertNil(
            retryBinding.requestForSubmission(replacement, choice: .notUseful)
        )
        let remeasuredSameIntent = ProductFeedbackRequestV1(
            idempotencyKey: first.idempotencyKey,
            sourceKind: first.sourceKind,
            sourceRef: first.sourceRef,
            projectRef: first.projectRef,
            useful: first.useful,
            resumeSeconds: 99,
            experimentRef: first.experimentRef
        )
        XCTAssertEqual(
            retryBinding.requestForSubmission(remeasuredSameIntent, choice: .useful),
            first,
            "the retry crossing Core must be the exact originally staged request"
        )
    }

    func testHandoffRenderCheckpointRejectsOldSameReferenceGeneration() throws {
        let source = "handoff_" + refA
        let project = "project_" + refA
        var render = Phase2HandoffRenderCheckpointState()
        let old = try XCTUnwrap(
            render.stage(
                sourceRef: source,
                projectRef: project,
                timerGeneration: 51
            )
        )
        let current = try XCTUnwrap(
            render.stage(
                sourceRef: source,
                projectRef: project,
                timerGeneration: 52
            )
        )

        XCTAssertFalse(
            render.consume(
                old,
                currentSourceRef: source,
                currentProjectRef: project,
                currentTimerGeneration: 52
            ),
            "an old same-ref onAppear must not consume the replacement render"
        )
        XCTAssertEqual(render.pending, current)
        XCTAssertFalse(
            render.consume(
                current,
                currentSourceRef: source,
                currentProjectRef: project,
                currentTimerGeneration: 51
            )
        )
        XCTAssertEqual(render.pending, current)
        XCTAssertTrue(
            render.consume(
                current,
                currentSourceRef: source,
                currentProjectRef: project,
                currentTimerGeneration: 52
            )
        )
        XCTAssertNil(render.pending)
    }

    func testFeedbackBindingFreezesAtTerminalAndRetainsOnlyExactTransportRetry() throws {
        let running = try ProductExperimentStatusV1(
            data: try jsonData(productExperimentStatus())
        )
        var binding = Phase2ExperimentFeedbackBindingState()
        XCTAssertTrue(binding.applyExperimentStatus(running))

        let first = ProductFeedbackRequestV1(
            idempotencyKey: "idem_" + refB,
            sourceKind: .localHandoff,
            sourceRef: "bundle_" + refA,
            projectRef: "project_" + refA,
            useful: true,
            resumeSeconds: 12.5,
            experimentRef: "experiment_" + refC
        )
        XCTAssertEqual(
            binding.requestForSubmission(first, choice: .useful),
            first
        )
        XCTAssertTrue(binding.submissionInFlight)
        XCTAssertFalse(binding.canSubmit(sourceRef: "bundle_" + refB))
        XCTAssertTrue(binding.markTransportUnknown(request: first))
        XCTAssertEqual(binding.submissionCheckpoint, .transportUnknown)
        let changedChoice = ProductFeedbackRequestV1(
            idempotencyKey: "idem_" + refA,
            sourceKind: .localHandoff,
            sourceRef: "bundle_" + refA,
            projectRef: "project_" + refA,
            useful: false,
            resumeSeconds: 12.5,
            experimentRef: "experiment_" + refC
        )
        XCTAssertNil(
            binding.requestForSubmission(changedChoice, choice: .notUseful)
        )
        // After an ambiguous transport failure, only the exact stored payload
        // may cross the service boundary again; changing the choice is refused.
        XCTAssertEqual(
            binding.requestForSubmission(first, choice: .useful),
            first
        )

        XCTAssertTrue(
            binding.markRecordedAwaitingStatusRefresh(request: first)
        )
        XCTAssertTrue(binding.statusRefreshPending)
        XCTAssertFalse(binding.canSubmit(sourceRef: first.sourceRef))
        XCTAssertFalse(
            binding.canSubmit(sourceRef: "bundle_" + refB),
            "no source may submit before the current experiment-status checkpoint returns"
        )
        XCTAssertNil(binding.pendingRetry)

        XCTAssertTrue(binding.applyExperimentStatus(running))
        XCTAssertFalse(binding.statusRefreshPending)
        XCTAssertFalse(binding.canSubmit(sourceRef: first.sourceRef))
        XCTAssertTrue(binding.canSubmit(sourceRef: "bundle_" + refB))

        var timer = Phase2ResumeTimerState()
        let timerGeneration = timer.begin(atUptimeNanoseconds: 1_000_000_000)
        XCTAssertTrue(timer.select(projectRef: first.projectRef))
        XCTAssertNotNil(
            timer.freeze(
                sourceRef: "bundle_" + refB,
                projectRef: first.projectRef,
                atUptimeNanoseconds: 2_000_000_000,
                generation: timerGeneration
            )
        )

        var terminalObject = productExperimentStatus()
        terminalObject["status"] = "passed"
        terminalObject["experiment_ref"] = NSNull()
        terminalObject["attempt_count"] = 10
        terminalObject["complete_attempt_count"] = 10
        terminalObject["useful_rate"] = 0.8
        terminalObject["median_resume_seconds"] = 20.0
        let terminal = try ProductExperimentStatusV1(
            data: try jsonData(terminalObject)
        )
        if !binding.applyExperimentStatus(terminal) {
            timer.cancel()
        }
        XCTAssertNil(binding.runningExperimentRef)
        XCTAssertFalse(binding.canSubmit(sourceRef: "bundle_" + refB))
        XCTAssertNil(timer.measurement)

        XCTAssertTrue(binding.applyExperimentStatus(running))
        XCTAssertNotNil(
            binding.requestForSubmission(first, choice: .useful)
        )
        binding.failClosed()
        XCTAssertNil(binding.runningExperimentRef)
        XCTAssertNil(binding.pendingRetry)
        XCTAssertFalse(binding.statusRefreshPending)
        XCTAssertFalse(binding.canSubmit(sourceRef: first.sourceRef))
    }

    func testFeedbackStatusRefreshPendingIgnoresStaleExperimentChannelResponse() throws {
        let running = try ProductExperimentStatusV1(
            data: try jsonData(productExperimentStatus())
        )
        let first = ProductFeedbackRequestV1(
            idempotencyKey: "idem_" + refA,
            sourceKind: .localHandoff,
            sourceRef: "handoff_" + refA,
            projectRef: "project_" + refA,
            useful: true,
            resumeSeconds: 8,
            experimentRef: "experiment_" + refC
        )
        var binding = Phase2ExperimentFeedbackBindingState()
        XCTAssertTrue(binding.applyExperimentStatus(running))
        XCTAssertNotNil(binding.requestForSubmission(first, choice: .useful))
        XCTAssertTrue(
            binding.markRecordedAwaitingStatusRefresh(request: first)
        )
        XCTAssertTrue(binding.statusRefreshPending)

        var viewState = Phase2ConsoleViewState()
        let staleToken = viewState.beginRequest(for: .experiment)
        let currentToken = viewState.beginRequest(for: .experiment)
        let staleAccepted = viewState.apply(
            productExperimentStatus: running,
            token: staleToken
        )
        XCTAssertFalse(staleAccepted)
        if staleAccepted {
            _ = binding.applyExperimentStatus(running)
        }
        XCTAssertTrue(binding.statusRefreshPending)
        XCTAssertFalse(binding.canSubmit(sourceRef: "handoff_" + refB))

        let currentAccepted = viewState.apply(
            productExperimentStatus: running,
            token: currentToken
        )
        XCTAssertTrue(currentAccepted)
        if currentAccepted {
            XCTAssertTrue(binding.applyExperimentStatus(running))
        }
        XCTAssertFalse(binding.statusRefreshPending)
        XCTAssertTrue(binding.canSubmit(sourceRef: "handoff_" + refB))

        let second = ProductFeedbackRequestV1(
            idempotencyKey: "idem_" + refB,
            sourceKind: .localHandoff,
            sourceRef: "handoff_" + refB,
            projectRef: "project_" + refB,
            useful: false,
            resumeSeconds: 9,
            experimentRef: "experiment_" + refC
        )
        XCTAssertNotNil(binding.requestForSubmission(second, choice: .notUseful))
        XCTAssertTrue(
            binding.markRecordedAwaitingStatusRefresh(request: second)
        )
        XCTAssertTrue(binding.statusRefreshPending)
        binding.failClosed()
        XCTAssertFalse(binding.statusRefreshPending)
        XCTAssertNil(binding.runningExperimentRef)
        XCTAssertFalse(binding.canSubmit(sourceRef: first.sourceRef))
    }

    func testExperimentStatusRefreshFailurePreservesEveryFeedbackSubmissionCheckpoint() throws {
        let running = try ProductExperimentStatusV1(
            data: try jsonData(productExperimentStatus())
        )
        var unavailableObject = productExperimentStatus()
        unavailableObject["read_state"] = "unavailable"
        unavailableObject["status"] = NSNull()
        unavailableObject["experiment_started"] = NSNull()
        unavailableObject["experiment_ref"] = NSNull()
        unavailableObject["attempt_count"] = NSNull()
        unavailableObject["complete_attempt_count"] = NSNull()
        unavailableObject["useful_rate"] = NSNull()
        unavailableObject["median_resume_seconds"] = NSNull()
        let unavailable = try ProductExperimentStatusV1(
            data: try jsonData(unavailableObject)
        )
        let first = ProductFeedbackRequestV1(
            idempotencyKey: "idem_" + refA,
            sourceKind: .localHandoff,
            sourceRef: "handoff_" + refA,
            projectRef: "project_" + refA,
            useful: true,
            resumeSeconds: 8,
            experimentRef: "experiment_" + refC
        )
        let replacement = ProductFeedbackRequestV1(
            idempotencyKey: "idem_" + refB,
            sourceKind: .localHandoff,
            sourceRef: "handoff_" + refB,
            projectRef: "project_" + refB,
            useful: false,
            resumeSeconds: 9,
            experimentRef: "experiment_" + refC
        )

        // Equivalent to a normal refresh's experiment-status command throwing
        // while feedback is already across the Core boundary. Trusted UI is
        // cleared, but the old result must still advance the exact checkpoint.
        var submitting = Phase2ExperimentFeedbackBindingState()
        XCTAssertTrue(submitting.applyExperimentStatus(running))
        XCTAssertEqual(
            submitting.requestForSubmission(first, choice: .useful),
            first
        )
        submitting.loseTrustPreservingSubmission()
        XCTAssertNil(submitting.runningExperimentRef)
        XCTAssertTrue(submitting.submissionInFlight)
        XCTAssertEqual(submitting.pendingRetry?.request, first)
        XCTAssertFalse(submitting.canSubmit(sourceRef: replacement.sourceRef))
        XCTAssertNil(
            submitting.requestForSubmission(replacement, choice: .notUseful)
        )
        XCTAssertTrue(
            submitting.markRecordedAwaitingStatusRefresh(request: first),
            "the delayed feedback receipt remains authoritative after refresh failure"
        )
        XCTAssertTrue(submitting.statusRefreshPending)
        XCTAssertFalse(submitting.canSubmit(sourceRef: replacement.sourceRef))
        XCTAssertTrue(submitting.applyExperimentStatus(running))
        XCTAssertFalse(submitting.statusRefreshPending)
        XCTAssertTrue(submitting.canSubmit(sourceRef: replacement.sourceRef))

        // A transport-unknown checkpoint also survives the failed independent
        // status read and permits only the exact stored idempotent request.
        var transportUnknown = Phase2ExperimentFeedbackBindingState()
        XCTAssertTrue(transportUnknown.applyExperimentStatus(running))
        XCTAssertEqual(
            transportUnknown.requestForSubmission(first, choice: .useful),
            first
        )
        XCTAssertTrue(transportUnknown.markTransportUnknown(request: first))
        transportUnknown.loseTrustPreservingSubmission()
        XCTAssertEqual(
            transportUnknown.submissionCheckpoint,
            .transportUnknown
        )
        XCTAssertNil(transportUnknown.runningExperimentRef)
        XCTAssertFalse(transportUnknown.canSubmit(sourceRef: replacement.sourceRef))
        let remeasuredSameIntent = ProductFeedbackRequestV1(
            idempotencyKey: "idem_" + refC,
            sourceKind: first.sourceKind,
            sourceRef: first.sourceRef,
            projectRef: first.projectRef,
            useful: first.useful,
            resumeSeconds: 99,
            experimentRef: first.experimentRef
        )
        XCTAssertEqual(
            transportUnknown.requestForSubmission(
                remeasuredSameIntent,
                choice: .useful
            ),
            first
        )
        XCTAssertTrue(
            transportUnknown.markRecordedAwaitingStatusRefresh(request: first)
        )
        XCTAssertTrue(transportUnknown.statusRefreshPending)

        // Busy/unknown/unavailable status projections clear trust but cannot
        // discard a recorded receipt awaiting its current status checkpoint.
        XCTAssertFalse(transportUnknown.applyExperimentStatus(unavailable))
        XCTAssertTrue(transportUnknown.statusRefreshPending)
        XCTAssertNil(transportUnknown.runningExperimentRef)
        XCTAssertFalse(transportUnknown.canSubmit(sourceRef: replacement.sourceRef))
        XCTAssertTrue(transportUnknown.applyExperimentStatus(running))
        XCTAssertFalse(transportUnknown.statusRefreshPending)
        XCTAssertTrue(transportUnknown.canSubmit(sourceRef: replacement.sourceRef))
    }

    // MARK: - Fixtures

    private func availableSnapshot() -> [String: Any] {
        let verified = verifiedContext()
        let legacy = legacyContext()
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
                "bundle_hash": hashA,
                "verified_contexts": [verified],
                "unverified_clues": [unverifiedClue()],
                "raw_access": false,
            ],
            "projects": [[
                "project": label("project", "PAM"),
                "verified_contexts": [verified],
                "compatibility_contexts": [legacy],
                "unverified_clues": [unverifiedClue()],
                "latest_occurred_at": "2026-07-30T00:55:00Z",
            ]],
            "legacy_confirmed": [legacy],
            "confirmation_questions": [confirmationQuestion()],
            "capabilities": [
                "search": true,
                "owner_action": true,
                "local_handoff": true,
                "product_feedback": true,
            ],
            "raw_access": false,
        ]
    }

    private func busySnapshot() -> [String: Any] {
        [
            "contract_version": "ContextSnapshotV1",
            "status": "busy",
            "observed_at": "2026-07-30T01:01:00Z",
            "last_successful_read_at": "2026-07-30T01:00:00Z",
            "counts": NSNull(),
            "context_bundle_v1": NSNull(),
            "projects": NSNull(),
            "legacy_confirmed": NSNull(),
            "confirmation_questions": NSNull(),
            "capabilities": NSNull(),
            "raw_access": false,
        ]
    }

    private func verifiedContext() -> [String: Any] {
        [
            "context_ref": "context_" + refA,
            "source_ref": "source_" + refA,
            "state": "in_progress",
            "occurred_at": "2026-07-30T00:50:00Z",
            "expires_at": "2026-10-28T00:50:00Z",
            "revocation_state": "active",
            "privacy_class": "private_local",
            "project": label("project", "PAM"),
            "workstream": label("workstream", "Phase 2"),
            "object": label("object", "Phase 2"),
            "version": label("version", "v2"),
            "subject": label("subject", "Local Owner"),
            "source": [
                "kind": "user_confirmation",
                "evidence_tier": "P1",
            ],
        ]
    }

    private func unverifiedClue() -> [String: Any] {
        [
            "source_ref": "source_" + refB,
            "clue_type": "screen",
            "observed_at": "2026-07-30T00:49:00Z",
            "expires_at": "2026-07-31T23:59:59Z",
            "revocation_state": "active",
            "privacy_class": "private_local",
            "project": label("project", "PAM"),
            "verification_state": "unverified",
            "safe_label": "待核实屏幕线索",
        ]
    }

    private func legacyContext() -> [String: Any] {
        [
            "context_ref": "context_" + refC,
            "state": "completed_confirmed",
            "occurred_at": "2026-07-29T08:00:00Z",
            "expires_at": "2026-10-27T08:00:00Z",
            "revocation_state": "active",
            "privacy_class": "private_local",
            "project": label("project", "PAM"),
            "summary": "旧确认安全摘要",
            "source": [
                "kind": "legacy_user_confirmation",
                "label": "本人确认",
            ],
            "compatibility_source": true,
            "external_delivery_eligible": false,
            "memory_candidate_eligible": false,
            "raw_access": false,
        ]
    }

    private func confirmationQuestion() -> [String: Any] {
        [
            "confirmation_ref": "confirmation_" + refA,
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
        ]
    }

    private func memoryCandidateList() -> [String: Any] {
        [
            "contract_version": "MemoryCandidateListV1",
            "status": "available",
            "observed_at": "2026-07-30T01:00:00Z",
            "candidates": [[
                "memory_candidate_ref": "memory_" + refA,
                "context_ref": "context_" + refA,
                "safe_summary": "PAM Phase 2 决策仍然有效",
                "project": label("project", "PAM"),
                "subject": label("subject", "Local Owner"),
                "state": "candidate_only",
                "source_kind": "user_confirmation",
                "expires_at": "2026-10-28T00:50:00Z",
                "revocation_state": "active",
                "direct_write": false,
                "writer_enabled": false,
            ]],
            "actions": NSNull(),
            "raw_access": false,
        ]
    }

    private func deliveryStatus() -> [String: Any] {
        [
            "contract_version": "DeliveryStatusV1",
            "status": "available",
            "observed_at": "2026-07-30T01:00:00Z",
            "daily_report": [
                "needs_input": false,
                "pending_confirmation_count": 0,
                "current_state": "delivered",
                "prepared_at": "2026-07-30T00:50:00Z",
                "consumed_at": "2026-07-30T00:51:00Z",
                "delivered_at": "2026-07-30T00:52:00Z",
                "failed_at": NSNull(),
            ],
            "deliveries": [[
                "delivery_ref": "delivery_" + refA,
                "bundle_ref": "bundle_" + refA,
                "attempt_no": 1,
                "project": label("project", "PAM"),
                "target": label("target", "飞书"),
                "state": "delivered",
                "prepared_at": "2026-07-30T00:50:00Z",
                "consumed_at": "2026-07-30T00:51:00Z",
                "delivered_at": "2026-07-30T00:52:00Z",
                "failed_at": NSNull(),
                "failure_category": NSNull(),
                "withdrawal_state": "active",
            ]],
            "raw_access": false,
        ]
    }

    private func localHandoff() -> [String: Any] {
        [
            "contract_version": "LocalHandoffV1",
            "status": "available",
            "observed_at": "2026-07-30T01:00:00Z",
            "handoff_ref": "bundle_" + refA,
            "bundle_hash": hashA,
            "project": label("project", "PAM"),
            "verified_contexts": [verifiedContext()],
            "unresolved_questions": ["确认 UI 是否足够高密度"],
            "expires_at": "2026-10-28T00:50:00Z",
            "revocation_state": "active",
            "raw_access": false,
        ]
    }

    private func productExperimentStatus() -> [String: Any] {
        [
            "contract_version": "ProductExperimentStatusV1",
            "read_state": "available",
            "observed_at": "2026-08-01T06:00:00Z",
            "status": "collecting",
            "experiment_started": true,
            "experiment_ref": "experiment_" + refC,
            "attempt_count": 3,
            "complete_attempt_count": 3,
            "useful_rate": 2.0 / 3.0,
            "median_resume_seconds": 12.5,
            "raw_access": false,
        ]
    }

    private func label(_ kind: String, _ value: String) -> [String: Any] {
        [
            "ref": "\(kind)_" + refA,
            "label": value,
        ]
    }

    private func jsonData(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }
    private func workbenchItem(_ id: String, state: String = "decision", kind: String = "closure", check: Bool = false) -> [String: Any] {
        ["id":id,"title":id,"kind":kind,"state":state,"requires_check":check,"revision":"2"]
    }
    private func workbenchSnapshot(_ status: String = "ok", decisions: [[String: Any]] = [], closures: [[String: Any]] = [], systems: [[String: Any]] = [], resources: [[String: Any]] = []) throws -> AttentionSnapshot {
        let raw: [String: Any] = ["interface":"attention-center","contract":1,"raw_access":false,"status":status,
            "decision_count":status == "ok" ? decisions.count as Any : NSNull(),"decisions":decisions,"closures":closures,"systems":systems,"resources":resources]
        return AttentionSnapshot(jsonText: String(data: try JSONSerialization.data(withJSONObject:raw), encoding:.utf8)!)
    }
    func testFiltersPreserveRealCategoriesAndDeduplicate() throws {
        let decision = workbenchItem("d"), pending = workbenchItem("p",state:"pending"), closed = workbenchItem("c",state:"resolved"), system = workbenchItem("s",kind:"system")
        let value = try workbenchSnapshot(decisions:[decision],closures:[decision,pending,closed],systems:[system,pending],resources:[workbenchItem("history",kind:"resource")])
        XCTAssertEqual(AttentionWorkbenchScope.decisions.rows(in:value).map(\.id),["d"])
        XCTAssertEqual(AttentionWorkbenchScope.following.rows(in:value).map(\.id),["p","s"])
        XCTAssertEqual(AttentionWorkbenchScope.closed.rows(in:value).map(\.id),["c"])
        XCTAssertEqual(AttentionWorkbenchScope.all.rows(in:value).map(\.id),["d","p","s","c"])
    }
    func testCanceledButUncheckedRemainsFollowing() throws {
        let value=try workbenchSnapshot(closures:[workbenchItem("a",state:"canceled",check:true),workbenchItem("b",state:"canceled")])
        XCTAssertEqual(AttentionWorkbenchScope.following.rows(in:value).map(\.id),["a"])
        XCTAssertEqual(AttentionWorkbenchScope.closed.rows(in:value).map(\.id),["b"])
        XCTAssertEqual(value.closures[0].workbenchStateLabel,"仍需核对")
    }
    func testUnknownReadRetainsAllOldRowsWithoutVerifiedCount() throws {
        let old=try workbenchSnapshot(decisions:[workbenchItem("a")],closures:[workbenchItem("p",state:"running")],systems:[workbenchItem("s",kind:"system")],resources:[workbenchItem("r",kind:"resource")])
        let value=AttentionSnapshot().retainingUnreadRows(from:old)
        XCTAssertEqual(value.status,"unknown"); XCTAssertNil(value.decisionCount); XCTAssertNil(value.badge)
        XCTAssertEqual(value.decisions.map(\.id),["a"]); XCTAssertEqual(value.resources.map(\.id),["r"])
        XCTAssertEqual(AttentionWorkbenchScope.all.rows(in:value).map(\.id),["a","p","s"])
    }
    func testPartialReadPrefersNewEvidenceAndDoesNotEraseMissingRows() throws {
        let old=try workbenchSnapshot(decisions:[workbenchItem("a"),workbenchItem("b")])
        var updated=workbenchItem("a"); updated["revision"]="3"
        let next=try workbenchSnapshot("partial",decisions:[updated,workbenchItem("new")])
        let value=next.retainingUnreadRows(from:old)
        XCTAssertEqual(value.decisions.map(\.id),["a","new","b"])
        XCTAssertEqual(value.workbenchItem("a")?.revision,"3"); XCTAssertNil(value.decisionCount)
    }
    func testCompleteReadCanRemoveAnEndedDecision() throws {
        let old=try workbenchSnapshot(decisions:[workbenchItem("a")])
        let value=try workbenchSnapshot(closures:[workbenchItem("a",state:"resolved")]).retainingUnreadRows(from:old)
        XCTAssertTrue(value.decisions.isEmpty); XCTAssertEqual(value.decisionCount,0)
        XCTAssertEqual(AttentionWorkbenchScope.closed.rows(in:value).map(\.id),["a"])
    }
    func testPaneChildOwnsItsReadAndKeepsParentAnchor() {
        let parent=AttentionPresentation(issueID:"parent",revision:"2",mode:.pane)
        let child=AttentionPresentation(issueID:"child",revision:"4",mode:.pane,anchorID:"parent")
        XCTAssertEqual(child.anchorID,parent.issueID)
        XCTAssertFalse(parent.owns(child)); XCTAssertTrue(child.matches(issueID:"child",revision:"4"))
        XCTAssertFalse(child.matches(issueID:"parent",revision:"4"))
        var parentRow = workbenchItem("parent")
        var childRow = workbenchItem("child"); childRow["revision"] = "4"
        parentRow["children"] = [childRow]
        let snapshot = try! workbenchSnapshot(decisions:[parentRow])
        XCTAssertEqual(snapshot.workbenchItem("child")?.revision,"4")
        XCTAssertNil(snapshot.workbenchItem("missing"))
    }
    func testOldActionCannotMatchReopenedOrRevisedPane() {
        let bound=AttentionPresentation(issueID:"a",revision:"2",mode:.pane)
        let reopened=AttentionPresentation(issueID:"a",revision:"2",mode:.pane)
        var revised=bound; revised.revision="3"
        XCTAssertFalse(reopened.owns(bound)); XCTAssertFalse(revised.owns(bound))
        XCTAssertFalse(AttentionPresentation(issueID:"a",revision:"2",mode:.sheet).owns(bound))
    }
    func testUnknownSubmissionRemainsBlockedAfterPaneCloses() {
        var submissions=AttentionSubmissions(); let key=AttentionSubmissions.key("a","2")
        XCTAssertTrue(submissions.begin(key)); submissions.finish(key,receipt:"unknown",outcome:.unknown)
        XCTAssertTrue(submissions.blocked(key)); XCTAssertFalse(submissions.begin(key))
        XCTAssertFalse(AttentionHandoffReceipt.verified([:]))
        XCTAssertFalse(AttentionHandoffReceipt.verified(["error":"截断", "outcome_unknown":true]))
        XCTAssertFalse(AttentionHandoffReceipt.verified(["interface":"attention-handoff","status":"ok"]))
        XCTAssertTrue(AttentionHandoffReceipt.verified(["interface":"attention-handoff","status":"ok","handoff":NSNull()]))
        for state in ["queued","delivered","uncertain","failed"] {
            XCTAssertTrue(AttentionHandoffReceipt.verified(["interface":"attention-handoff","status":"ok","handoff":["submission_id":"existing", "status":state]]))
        }
        XCTAssertFalse(AttentionHandoffReceipt.verified(["interface":"attention-handoff","status":"ok","handoff":["status":"delivered"]]))
        XCTAssertFalse(AttentionHandoffReceipt.verified(["interface":"attention-handoff","status":"ok","handoff":["submission_id":"existing", "status":"unrecognized"]]))
    }
    func testWithdrawnHandoffCannotReturnThroughDelayedSaveOrDisappear() {
        var cache = AttentionHandoffDraftCache()
        let retired = cache.lease(for: "company"), personal = cache.lease(for: "personal")
        XCTAssertTrue(cache.save("Generic retained draft", revision: 2, using: retired))
        XCTAssertTrue(cache.save("Personal input", revision: 3, using: personal))
        cache.withdraw("company")
        XCTAssertNil(cache.draft(using: retired)); XCTAssertFalse(cache.isCurrent(retired))
        XCTAssertFalse(cache.save("Old delayed save", revision: 4, using: retired))
        XCTAssertFalse(cache.save("Old onDisappear save", revision: 5, using: retired))
        XCTAssertEqual(cache.draft(using: personal)?.text, "Personal input")
        let reopened = cache.lease(for: "company")
        XCTAssertNil(cache.draft(using: reopened))
        XCTAssertFalse(cache.save("Late prior pane", revision: 6, using: retired))
    }
    func testScopeWithdrawalInvalidatesEveryPaneAndPendingDraftSave() {
        var cache = AttentionHandoffDraftCache()
        let first = cache.lease(for: "a"), second = cache.lease(for: "b")
        cache.save("Old a", revision: 1, using: first); cache.save("Old b", revision: 1, using: second)
        cache.withdrawAll()
        XCTAssertFalse(cache.save("Stale a", revision: 2, using: first))
        XCTAssertFalse(cache.save("Stale b", revision: 2, using: second))
        XCTAssertNil(cache.draft(using: cache.lease(for: "a")))
        XCTAssertNil(cache.draft(using: cache.lease(for: "b")))
    }
    func testOrdinaryHandoffFailurePreservesDraftAndDoesNotImplyWithdrawal() {
        var cache = AttentionHandoffDraftCache(); let pane = cache.lease(for: "personal")
        cache.save("Input awaiting receipt", revision: 8, using: pane)
        for response: [String: Any] in [[:], ["error":"source_not_in_display_scope"],
            ["interface":"attention-handoff", "status":"unavailable", "reason":"receipt_uncertain"],
            ["interface":"other", "status":"unavailable", "reason":"display_scope_unavailable"]] {
            XCTAssertFalse(AttentionHandoffReceipt.sourceWithdrawn(response))
        }
        XCTAssertEqual(cache.draft(using: pane)?.text, "Input awaiting receipt")
        cache.save("Older response", revision: 7, using: pane)
        XCTAssertEqual(cache.draft(using: pane)?.revision, 8)
        for reason in ["source_not_in_display_scope", "display_scope_unavailable"] {
            XCTAssertTrue(AttentionHandoffReceipt.sourceWithdrawn(["interface":"attention-handoff", "status":"unavailable", "reason":reason]))
        }
    }
    func testWithdrawnNestedSourceDisappearsAcrossScopesWithoutAZeroBadge() throws {
        var parent = workbenchItem("parent", kind:"system")
        var child = workbenchItem("child", kind:"system")
        child["children"] = [workbenchItem("retired", kind:"system")]; parent["children"] = [child]
        let previous = try workbenchSnapshot(decisions:[workbenchItem("retired"), workbenchItem("personal")],
            closures:[workbenchItem("retired")], systems:[parent, workbenchItem("other", kind:"system")], resources:[parent])
        let withdrawn = previous.withdrawingItem("retired")
        XCTAssertNil(withdrawn.workbenchItem("retired")); XCTAssertNil(withdrawn.workbenchItem("parent"))
        XCTAssertEqual(withdrawn.decisions.map(\.id), ["personal"])
        XCTAssertEqual(withdrawn.systems.map(\.id), ["other"])
        XCTAssertTrue(withdrawn.resources.isEmpty); XCTAssertTrue(withdrawn.closures.isEmpty)
        XCTAssertEqual(withdrawn.status, "partial"); XCTAssertNil(withdrawn.decisionCount); XCTAssertNil(withdrawn.badge)
    }
    func testAttentionReadCoalescingPreservesRequiredFollowUpAndWithdrawal() throws {
        var reads = AttentionRefreshReads()
        let first = try XCTUnwrap(reads.request(coalescingIfLoading: true))
        XCTAssertNil(reads.request(coalescingIfLoading: true))
        XCTAssertFalse(reads.pending)
        XCTAssertNil(reads.request()) // A write or an explicit refresh needs a new read.
        XCTAssertTrue(reads.pending)
        XCTAssertNil(reads.request(coalescingIfLoading: true))
        XCTAssertTrue(reads.finish(first))
        let followUp = try XCTUnwrap(reads.request())
        XCTAssertFalse(reads.finish(first)); XCTAssertTrue(reads.isCurrent(followUp))
        reads.invalidate() // A withdrawal invalidates both work and its queued follow-up.
        XCTAssertFalse(reads.finish(followUp)); XCTAssertFalse(reads.loading); XCTAssertFalse(reads.pending)
        let current = try XCTUnwrap(reads.request(coalescingIfLoading: true))
        XCTAssertFalse(reads.isCurrent(followUp)); XCTAssertTrue(reads.isCurrent(current))
        XCTAssertFalse(reads.finish(current)); XCTAssertFalse(reads.loading)
    }
    func testSnapshotStartedBeforeSourceWithdrawalCannotRestoreItsRows() throws {
        var generations = AttentionRefreshReads()
        let lateRequest = try XCTUnwrap(generations.request())
        let oldResponse = try workbenchSnapshot(decisions:[workbenchItem("retired"), workbenchItem("personal")])
        var displayed = oldResponse.withdrawingItem("retired")
        generations.invalidate() // The source-withdrawal receipt invalidates the whole read chain.
        if generations.isCurrent(lateRequest) { displayed = oldResponse }
        XCTAssertNil(displayed.workbenchItem("retired")); XCTAssertNil(displayed.badge)
        let newRequest = try XCTUnwrap(generations.request())
        let verifiedPersonal = try workbenchSnapshot(decisions:[workbenchItem("personal")])
        if generations.isCurrent(newRequest) { displayed = verifiedPersonal }
        XCTAssertEqual(displayed.decisionCount, 1); XCTAssertNil(displayed.workbenchItem("retired"))
        XCTAssertFalse(generations.isCurrent(lateRequest))
    }
    func testIdlePublicationUntouchedBackgroundCanYieldForFreshRequest() {
        XCTAssertTrue(IdlePublicationPolicy.permitsExit(untouched: true, active: false, modal: false, windowAtRisk: false, age: 1))
    }
    func testIdlePublicationInteractionStaysProtectedAfterWindowCloses() {
        XCTAssertFalse(IdlePublicationPolicy.permitsExit(untouched: false, active: false, modal: false, windowAtRisk: false, age: 1))
    }
    func testIdlePublicationWindowAndDialogAndActiveAppPreventExit() {
        XCTAssertFalse(IdlePublicationPolicy.permitsExit(untouched: true, active: false, modal: false, windowAtRisk: true, age: 1))
        XCTAssertFalse(IdlePublicationPolicy.permitsExit(untouched: true, active: false, modal: true, windowAtRisk: false, age: 1))
        XCTAssertFalse(IdlePublicationPolicy.permitsExit(untouched: true, active: true, modal: false, windowAtRisk: false, age: 1))
    }
    func testIdlePublicationExpiredAndFutureRequestsCannotQuitApp() {
        for age in [-1.0, 10.1, Double.infinity, Double.nan] {
            XCTAssertFalse(IdlePublicationPolicy.permitsExit(untouched: true, active: false, modal: false, windowAtRisk: false, age: age))
        }
    }

    func testServiceQueryUnavailableClearsTrustedStateAndKeepsItsReason() throws {
        var state = ProductionStatusClientState()
        let full: [String: Any] = [
            "interface": "full_status", "status": "paused", "raw_access": false,
            "observed_at": "2026-10-16T00:00:00+00:00", "capture_allowed_by_session": false,
            "launch_agent_loaded": true, "launch_agent_running": true, "runtime_identity": [:],
            "today_activity_count": 0, "today_privacy_count": 0,
            "today_gap_count": 0, "raw_residue_count": 0,
        ]
        XCTAssertTrue(state.apply(dictionary: full, interface: .fullStatus).accepted)
        XCTAssertTrue(state.hasCurrentTrustedState)
        // This is the content-free Python service-query failure envelope,
        // including its legacy checked_at field. No service fact is guessed.
        let raw = """
        {"status":"unavailable","kind":"warning","interface":"quick_status",
         "checked_at":"2026-10-16T00:00:01+00:00","observed_at":"2026-10-16T00:00:01+00:00",
         "last_successful_read_at":null,"actions_enabled":false,
         "reason_code":"service_status_unavailable","capture_allowed_by_session":false,
         "attention_codes":["service_status_unavailable"],"raw_access":false}
        """
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [String: Any])
        let result = state.apply(dictionary: payload, interface: .quickStatus)
        XCTAssertTrue(result.accepted)
        XCTAssertTrue(result.mustClearCurrentState)
        XCTAssertEqual(state.readState, .unavailable)
        XCTAssertEqual(state.reasonCode, "service_status_unavailable")
        XCTAssertNil(state.trustedSnapshot)
        XCTAssertEqual(state.actionAvailability, .refreshOnly)
        XCTAssertTrue(state.requiresFullRefresh)
    }

    @MainActor
    func testStatusFullRequestsDuringQuickWaitForOneSerialFullRead() async {
        let reads = StatusRefreshReadHarness()
        defer { reads.releaseAll() }
        let first = Task { await reads.request(.quick, caller: "quick") }
        let quickBegan = await reads.settle { reads.quickCalls == 1 }
        XCTAssertTrue(quickBegan)
        let fullA = Task { await reads.request(.full, caller: "full-a") }
        let fullB = Task { await reads.request(.full, caller: "full-b") }
        let automatic = Task { await reads.request(.full, caller: "automatic", coalescing: true) }
        let quickB = Task { await reads.request(.quick, caller: "quick-b") }
        let allBegan = await reads.settle { reads.began.count == 5 }
        XCTAssertTrue(allBegan)
        XCTAssertEqual(reads.fullCalls, 0)
        reads.quickGate.release()
        let fullBegan = await reads.settle { reads.fullCalls == 1 }
        XCTAssertTrue(fullBegan)
        XCTAssertTrue(reads.returned.isEmpty)
        XCTAssertTrue(reads.coordinator.isRunning)
        reads.fullGates[0].release()
        await first.value; await fullA.value; await fullB.value
        await automatic.value; await quickB.value
        XCTAssertEqual(reads.quickCalls, 1)
        XCTAssertEqual(reads.fullCalls, 1)
        XCTAssertEqual(reads.peakActiveReads, 1)
        XCTAssertEqual(reads.returned, reads.began)
        XCTAssertFalse(reads.coordinator.isRunning)
    }

    @MainActor
    func testStatusQuickTrustLossAwaitsOneFullAttemptWithoutFailureLoop() async {
        let reads = StatusRefreshReadHarness(needsFull: true)
        defer { reads.releaseAll() }
        let request = Task { await reads.request(.quick, caller: "quick") }
        let quickBegan = await reads.settle { reads.quickCalls == 1 }
        XCTAssertTrue(quickBegan)
        reads.quickGate.release()
        let fullBegan = await reads.settle { reads.fullCalls == 1 }
        XCTAssertTrue(fullBegan)
        XCTAssertTrue(reads.returned.isEmpty)
        XCTAssertTrue(reads.coordinator.isRunning)
        // The full callback completes without supplying a successful snapshot.
        // That outcome must not schedule a self-perpetuating retry chain.
        reads.fullGates[0].release()
        await request.value
        for _ in 0..<10 { await Task.yield() }
        XCTAssertEqual(reads.quickCalls, 1)
        XCTAssertEqual(reads.fullCalls, 1)
        XCTAssertEqual(reads.peakActiveReads, 1)
        XCTAssertFalse(reads.coordinator.isRunning)
    }

    @MainActor
    func testStatusAutomaticFullTickCoalescesWithCurrentFullRead() async {
        let reads = StatusRefreshReadHarness()
        defer { reads.releaseAll() }
        let first = Task { await reads.request(.full, caller: "first") }
        let firstBegan = await reads.settle { reads.fullCalls == 1 }
        XCTAssertTrue(firstBegan)
        let automatic = Task { await reads.request(.full, caller: "automatic", coalescing: true) }
        let bothBegan = await reads.settle { reads.began.count == 2 }
        XCTAssertTrue(bothBegan)
        XCTAssertTrue(reads.returned.isEmpty)
        reads.fullGates[0].release()
        await first.value; await automatic.value
        XCTAssertEqual(reads.quickCalls, 0)
        XCTAssertEqual(reads.fullCalls, 1)
        XCTAssertEqual(reads.returned, reads.began)
        XCTAssertFalse(reads.coordinator.isRunning)
    }

    @MainActor
    func testStatusExplicitFullDuringFullAwaitsFreshReadAfterCurrentRead() async {
        let reads = StatusRefreshReadHarness()
        defer { reads.releaseAll() }
        let first = Task { await reads.request(.full, caller: "first") }
        let firstBegan = await reads.settle { reads.fullCalls == 1 }
        XCTAssertTrue(firstBegan)
        let writeA = Task { await reads.request(.full, caller: "write-a") }
        let writeB = Task { await reads.request(.full, caller: "write-b") }
        let automatic = Task { await reads.request(.full, caller: "automatic", coalescing: true) }
        let allBegan = await reads.settle { reads.began.count == 4 }
        XCTAssertTrue(allBegan)
        reads.fullGates[0].release()
        let followUpBegan = await reads.settle { reads.fullCalls == 2 }
        XCTAssertTrue(followUpBegan)
        XCTAssertTrue(reads.returned.isEmpty)
        XCTAssertTrue(reads.coordinator.isRunning)
        reads.fullGates[1].release()
        await first.value; await writeA.value; await writeB.value; await automatic.value
        XCTAssertEqual(reads.quickCalls, 0)
        XCTAssertEqual(reads.fullCalls, 2)
        XCTAssertEqual(reads.peakActiveReads, 1)
        XCTAssertEqual(reads.returned, reads.began)
        XCTAssertFalse(reads.coordinator.isRunning)
    }

}

@MainActor
private final class StatusRefreshReadGate {
    private var released = false
    private var continuation: CheckedContinuation<Void, Never>?

    func wait() async {
        guard !released else { return }
        await withCheckedContinuation { continuation = $0 }
    }

    func release() {
        released = true
        continuation?.resume()
        continuation = nil
    }
}

@MainActor
private final class StatusRefreshReadHarness {
    let coordinator = ProductionStatusRefreshReads()
    let quickGate = StatusRefreshReadGate()
    let fullGates = [StatusRefreshReadGate(), StatusRefreshReadGate()]
    private let needsFull: Bool
    private var activeReads = 0
    private(set) var peakActiveReads = 0
    private(set) var quickCalls = 0
    private(set) var fullCalls = 0
    private(set) var began: Set<String> = []
    private(set) var returned: Set<String> = []

    init(needsFull: Bool = false) { self.needsFull = needsFull }

    func request(_ kind: ProductionStatusRefreshReads.Kind, caller: String, coalescing: Bool = false) async {
        began.insert(caller)
        await coordinator.request(kind, coalescingFull: coalescing,
            quick: { [self] in
                quickCalls += 1
                enterRead()
                defer { activeReads -= 1 }
                await quickGate.wait()
                return needsFull
            },
            full: { [self] in
                let index = fullCalls
                fullCalls += 1
                enterRead()
                defer { activeReads -= 1 }
                if index < fullGates.count { await fullGates[index].wait() }
            })
        returned.insert(caller)
    }

    private func enterRead() {
        activeReads += 1
        peakActiveReads = max(peakActiveReads, activeReads)
    }

    func settle(until condition: @MainActor () -> Bool) async -> Bool {
        for _ in 0..<10_000 {
            if condition() { return true }
            await Task.yield()
        }
        return condition()
    }

    func releaseAll() {
        quickGate.release()
        fullGates.forEach { $0.release() }
    }
}
#endif
