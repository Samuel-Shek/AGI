import CoreFoundation
import Foundation

// MARK: - Phase 2 navigation and feature boundary

public enum Phase2Destination: String, CaseIterable, Identifiable, Sendable {
    case today
    case find
    case memory
    case settings
    case legacy

    public var id: Self { self }

    public var title: String {
        switch self {
        case .today: return "今天"
        case .find: return "找回"
        case .memory: return "记忆"
        case .settings: return "设置"
        case .legacy: return "旧版界面"
        }
    }
}

public enum ContextConsolePhase2FeatureFlag {
    public static let key = "context_console_phase2_ui_enabled"
    public static let defaultValue = false
    public static let maintenanceLocked = true

    public static func isEnabled(in defaults: UserDefaults) -> Bool {
        _ = defaults
        return false
    }
}

public struct Phase2RouteState: Equatable, Sendable {
    public private(set) var destination: Phase2Destination
    public private(set) var searchFocusGeneration: UInt64

    public init(
        destination: Phase2Destination = .today,
        searchFocusGeneration: UInt64 = 0
    ) {
        self.destination = destination
        self.searchFocusGeneration = searchFocusGeneration
    }

    public mutating func select(_ destination: Phase2Destination) {
        self.destination = destination
    }

    /// Command-K is deliberately app-window scoped. The App target owns the
    /// keyboard command and calls this pure reducer.
    public mutating func focusFind() {
        destination = .find
        searchFocusGeneration &+= 1
    }
}

/// Pure routing plan used by the App shell before it mutates SwiftUI
/// destination state. Every path away from Find therefore has one synchronous
/// place to revoke the timer, render token and feedback action capability;
/// entering (or explicitly restarting) Find starts exactly one new flow.
public struct Phase2DestinationTransition: Equatable, Sendable {
    public let destination: Phase2Destination
    public let shouldCancelFindBeforeSelection: Bool
    public let shouldBeginFindFlow: Bool

    public init(
        current: Phase2Destination,
        requested: Phase2Destination,
        restartFind: Bool = false
    ) {
        destination = requested
        shouldCancelFindBeforeSelection = current == .find && requested != .find
        shouldBeginFindFlow = requested == .find
            && (current != .find || restartFind)
    }
}

public enum Phase2SystemHealth: String, Equatable, Sendable {
    case normal
    case paused
    case error

    /// Shared three-state resolver for the menu bar, console sidebar and
    /// unified settings. An unreadable status is an error at the menu level;
    /// views retain `displayStateUnknown` so they can label it “状态未知”.
    public static func resolve(
        displayStateUnknown: Bool,
        status: String,
        statusIsDanger: Bool,
        hasKnownCaptureError: Bool,
        launchAgentLoaded: Bool,
        launchAgentRunning: Bool,
        captureFreshnessIsStale: Bool,
        captureBlockedBySession: Bool
    ) -> Self {
        let normalizedStatus = status.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if displayStateUnknown {
            return .error
        }
        if normalizedStatus == "error" || normalizedStatus == "session_state_unknown" {
            return .error
        }
        if statusIsDanger
            || hasKnownCaptureError
            || (launchAgentLoaded && !launchAgentRunning) {
            return .error
        }
        if !launchAgentLoaded && launchAgentRunning {
            return .error
        }
        if normalizedStatus == "paused"
            || normalizedStatus == "locked"
            || captureBlockedBySession
            || (normalizedStatus == "stopped" && !launchAgentLoaded && !launchAgentRunning) {
            return .paused
        }
        if normalizedStatus == "stopped" {
            return .error
        }
        if !launchAgentLoaded && normalizedStatus != "capturing" {
            return .paused
        }
        if captureFreshnessIsStale {
            return .error
        }
        if normalizedStatus == "capturing"
            || normalizedStatus == "ready"
            || normalizedStatus == "running" {
            return .normal
        }
        return .error
    }
}

public enum Phase2FindTimePreset: String, CaseIterable, Identifiable, Sendable {
    case retentionWindow
    case today
    case yesterday
    case sevenDays
    case thirtyDays

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .retentionWindow: return "最近 90 天"
        case .today: return "今天"
        case .yesterday: return "昨天"
        case .sevenDays: return "最近 7 天"
        case .thirtyDays: return "最近 30 天"
        }
    }

    /// Resolve local calendar-day presets into an explicit UTC half-open
    /// interval. Even the retention preset is explicit so a project-only
    /// search cannot silently fall back to Core's Today default.
    public func utcRange(
        now: Date = Date(),
        calendar: Calendar = .autoupdatingCurrent
    ) -> (startAt: String, endAt: String) {
        let startOfToday = calendar.startOfDay(for: now)
        let start: Date
        let end: Date
        switch self {
        case .retentionWindow:
            start = calendar.date(byAdding: .day, value: -89, to: startOfToday) ?? startOfToday
            end = calendar.date(byAdding: .day, value: 1, to: startOfToday) ?? now
        case .today:
            start = startOfToday
            end = calendar.date(byAdding: .day, value: 1, to: startOfToday) ?? now
        case .yesterday:
            start = calendar.date(byAdding: .day, value: -1, to: startOfToday) ?? startOfToday
            end = startOfToday
        case .sevenDays:
            start = calendar.date(byAdding: .day, value: -6, to: startOfToday) ?? startOfToday
            end = calendar.date(byAdding: .day, value: 1, to: startOfToday) ?? now
        case .thirtyDays:
            start = calendar.date(byAdding: .day, value: -29, to: startOfToday) ?? startOfToday
            end = calendar.date(byAdding: .day, value: 1, to: startOfToday) ?? now
        }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return (formatter.string(from: start), formatter.string(from: end))
    }
}

public struct QuickCaptureDraft: Equatable, Sendable {
    // The legacy confirmation row and Context graph are written atomically.
    // OwnerContextActionV1 deliberately allows a concise 180-character note.
    // Other registry labels retain their narrower Core limits.
    public static let maximumTextLength = 180

    public var text: String
    public var explicitlyConfirmedOwner: Bool

    public init(text: String = "", explicitlyConfirmedOwner: Bool = false) {
        self.text = text
        self.explicitlyConfirmedOwner = explicitlyConfirmedOwner
    }

    public var normalizedText: String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var validationReason: String? {
        let value = normalizedText
        guard !value.isEmpty else { return "note_empty" }
        guard value.count <= Self.maximumTextLength else { return "note_too_long" }
        guard Phase2ContractPolicy.safeUserText(value, maximumLength: Self.maximumTextLength) != nil else {
            return "note_contains_unsafe_control"
        }
        guard explicitlyConfirmedOwner else { return "owner_confirmation_required" }
        return nil
    }

    public var isSubmissionReady: Bool { validationReason == nil }
}

// MARK: - Core process boundary

public struct CoreCommandRequest: Equatable, Sendable {
    public let arguments: [String]
    public let standardInput: Data?
    public let timeoutSeconds: TimeInterval

    public init(
        arguments: [String],
        standardInput: Data?,
        timeoutSeconds: TimeInterval
    ) {
        self.arguments = arguments
        self.standardInput = standardInput
        self.timeoutSeconds = timeoutSeconds
    }
}

public struct CoreCommandResult: Equatable, Sendable {
    public let exitCode: Int32
    public let standardOutput: Data
    public let standardError: Data

    public init(exitCode: Int32, standardOutput: Data, standardError: Data) {
        self.exitCode = exitCode
        self.standardOutput = standardOutput
        self.standardError = standardError
    }
}

public protocol CoreCommandExecuting: Sendable {
    func execute(_ request: CoreCommandRequest) async throws -> CoreCommandResult
}

public enum CoreCommandBoundaryError: Error, Equatable, Sendable {
    case nonzeroExit(Int32)
    case emptyStandardOutput
    case oversizedStandardOutput
    case invalidJSON
    case nonObjectJSON
}

public enum CoreCommandJSONBoundary {
    public static let maximumResponseBytes = 512 * 1_024

    /// Decode only stdout. stderr is intentionally never treated as a JSON
    /// fallback because it may contain diagnostics or private local paths.
    public static func decodeObject(from result: CoreCommandResult) throws -> [String: Any] {
        guard result.exitCode == 0 else { throw CoreCommandBoundaryError.nonzeroExit(result.exitCode) }
        guard !result.standardOutput.isEmpty else { throw CoreCommandBoundaryError.emptyStandardOutput }
        guard result.standardOutput.count <= maximumResponseBytes else {
            throw CoreCommandBoundaryError.oversizedStandardOutput
        }
        let object: Any
        do {
            object = try JSONSerialization.jsonObject(with: result.standardOutput)
        } catch {
            throw CoreCommandBoundaryError.invalidJSON
        }
        guard let dictionary = object as? [String: Any] else {
            throw CoreCommandBoundaryError.nonObjectJSON
        }
        return dictionary
    }
}

// MARK: - Shared safe public projection

public enum Phase2ContractError: Error, Equatable, Sendable {
    case invalid(String)
}

public enum Phase2ReadState: String, Equatable, Sendable {
    case available
    case disabled
    case busy
    case unknown
    case unavailable
    case blocked

    public var isReadable: Bool { self == .available }
}

public enum Phase2PresentationCopy {
    /// Closed, reader-facing copy. Core reason codes remain diagnostic data
    /// and must never be interpolated into the ordinary console.
    public static func unavailableMessage(for state: Phase2ReadState) -> String {
        switch state {
        case .available:
            return ""
        case .disabled:
            return "上下文主链未启用。"
        case .busy:
            return "状态未知；可信内容已清空，请稍后刷新。"
        case .unknown:
            return "状态未知；可信内容已清空。"
        case .unavailable:
            return "可信状态暂时不可读；旧内容未被沿用。"
        case .blocked:
            return "安全校验未通过；可信内容已清空。"
        }
    }
}

public struct ContextLabelV1: Equatable, Sendable {
    public let ref: String
    public let displayLabel: String

    public init(ref: String, displayLabel: String) {
        self.ref = ref
        self.displayLabel = displayLabel
    }

    fileprivate init(
        dictionary: [String: Any],
        expectedPrefix: String
    ) throws {
        guard let ref = Phase2ContractPolicy.publicRef(dictionary["ref"], prefixes: [expectedPrefix]),
              let label = Phase2ContractPolicy.safeLabel(dictionary["label"])
        else {
            throw Phase2ContractError.invalid("invalid_public_label")
        }
        self.init(ref: ref, displayLabel: label)
    }
}

public struct ContextSourceSummaryV1: Equatable, Sendable {
    public let kind: String
    public let evidenceTier: String
    public let displayLabel: String

    public init(kind: String, evidenceTier: String, displayLabel: String) {
        self.kind = kind
        self.evidenceTier = evidenceTier
        self.displayLabel = displayLabel
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let kind = Phase2ContractPolicy.safeCode(dictionary["kind"], maximumLength: 48)
        else {
            throw Phase2ContractError.invalid("invalid_source_kind")
        }
        let tier = Phase2ContractPolicy.safeCode(dictionary["evidence_tier"], maximumLength: 24) ?? ""
        let display = Phase2ContractPolicy.safeLabel(dictionary["label"]) ?? kind
        self.init(kind: kind, evidenceTier: tier, displayLabel: display)
    }
}

public struct VerifiedContextSummaryV1: Equatable, Sendable {
    public let contextRef: String
    public let sourceRef: String
    public let state: String
    public let safeLabel: String
    public let sourceLabel: String
    public let occurredAt: String
    public let expiresAt: String
    public let revocationState: String
    public let privacyClass: String
    public let project: ContextLabelV1
    public let workstream: ContextLabelV1
    public let object: ContextLabelV1
    public let version: ContextLabelV1
    public let subject: ContextLabelV1
    public let source: ContextSourceSummaryV1

    public init(
        contextRef: String,
        sourceRef: String,
        state: String,
        safeLabel: String,
        sourceLabel: String,
        occurredAt: String,
        expiresAt: String,
        revocationState: String,
        privacyClass: String,
        project: ContextLabelV1,
        workstream: ContextLabelV1,
        object: ContextLabelV1,
        version: ContextLabelV1,
        subject: ContextLabelV1,
        source: ContextSourceSummaryV1
    ) {
        self.contextRef = contextRef
        self.sourceRef = sourceRef
        self.state = state
        self.safeLabel = safeLabel
        self.sourceLabel = sourceLabel
        self.occurredAt = occurredAt
        self.expiresAt = expiresAt
        self.revocationState = revocationState
        self.privacyClass = privacyClass
        self.project = project
        self.workstream = workstream
        self.object = object
        self.version = version
        self.subject = subject
        self.source = source
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let contextRef = Phase2ContractPolicy.publicRef(
            dictionary["context_ref"],
            prefixes: ["context_"]
        ),
        let sourceRef = Phase2ContractPolicy.publicRef(
            dictionary["source_ref"],
            prefixes: ["source_"]
        ),
        let state = Phase2ContractPolicy.safeCode(dictionary["state"], maximumLength: 48),
        let occurredAt = Phase2ContractPolicy.utcTimestamp(dictionary["occurred_at"]),
        let expiresAt = Phase2ContractPolicy.utcTimestamp(dictionary["expires_at"]),
        let revocationState = Phase2ContractPolicy.safeCode(
            dictionary["revocation_state"],
            maximumLength: 32
        ),
        let privacyClass = Phase2ContractPolicy.safeCode(dictionary["privacy_class"], maximumLength: 32),
        let projectRaw = dictionary["project"] as? [String: Any],
        let workstreamRaw = dictionary["workstream"] as? [String: Any],
        let objectRaw = dictionary["object"] as? [String: Any],
        let versionRaw = dictionary["version"] as? [String: Any],
        let subjectRaw = dictionary["subject"] as? [String: Any],
        let sourceRaw = dictionary["source"] as? [String: Any]
        else {
            throw Phase2ContractError.invalid("invalid_verified_context")
        }
        let project = try ContextLabelV1(dictionary: projectRaw, expectedPrefix: "project_")
        let workstream = try ContextLabelV1(dictionary: workstreamRaw, expectedPrefix: "workstream_")
        let object = try ContextLabelV1(dictionary: objectRaw, expectedPrefix: "object_")
        let version = try ContextLabelV1(dictionary: versionRaw, expectedPrefix: "version_")
        let subject = try ContextLabelV1(dictionary: subjectRaw, expectedPrefix: "subject_")
        let source = try ContextSourceSummaryV1(dictionary: sourceRaw)
        let explicitLabel = Phase2ContractPolicy.safeLabel(dictionary["safe_label"])
        let safeLabel = explicitLabel
            ?? [object.displayLabel, workstream.displayLabel, project.displayLabel]
                .first(where: { !$0.isEmpty })
            ?? "已验证上下文"
        self.init(
            contextRef: contextRef,
            sourceRef: sourceRef,
            state: state,
            safeLabel: safeLabel,
            sourceLabel: source.displayLabel,
            occurredAt: occurredAt,
            expiresAt: expiresAt,
            revocationState: revocationState,
            privacyClass: privacyClass,
            project: project,
            workstream: workstream,
            object: object,
            version: version,
            subject: subject,
            source: source
        )
    }
}

public struct UnverifiedClueSummaryV1: Equatable, Sendable {
    public let sourceRef: String
    public let safeLabel: String
    public let sourceLabel: String
    public let occurredAt: String
    public let project: ContextLabelV1

    public init(
        sourceRef: String,
        safeLabel: String,
        sourceLabel: String,
        occurredAt: String,
        project: ContextLabelV1
    ) {
        self.sourceRef = sourceRef
        self.safeLabel = safeLabel
        self.sourceLabel = sourceLabel
        self.occurredAt = occurredAt
        self.project = project
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let sourceRef = Phase2ContractPolicy.publicRef(
            dictionary["source_ref"],
            prefixes: ["source_"]
        ),
        let occurredAt = Phase2ContractPolicy.utcTimestamp(
            dictionary["observed_at"] ?? dictionary["occurred_at"]
        ),
        let sourceKind = Phase2ContractPolicy.safeCode(
            dictionary["clue_type"] ?? dictionary["source_kind"] ?? dictionary["kind"],
            maximumLength: 48
        ),
        let projectRaw = dictionary["project"] as? [String: Any],
        dictionary["verification_state"] as? String == "unverified"
        else {
            throw Phase2ContractError.invalid("invalid_unverified_clue")
        }
        let project = try ContextLabelV1(dictionary: projectRaw, expectedPrefix: "project_")
        let safeLabel = Phase2ContractPolicy.safeLabel(
            dictionary["safe_label"] ?? dictionary["label"]
        ) ?? "\(project.displayLabel) · 待核实线索"
        if let trust = dictionary["trust_partition"] {
            guard trust as? String == "unverified_clue" else {
                throw Phase2ContractError.invalid("clue_trust_partition_invalid")
            }
        }
        self.init(
            sourceRef: sourceRef,
            safeLabel: safeLabel,
            sourceLabel: sourceKind,
            occurredAt: occurredAt,
            project: project
        )
    }
}

public struct ContextBundleProjectionV1: Equatable, Sendable {
    public let bundleHash: String
    public let verifiedContexts: [VerifiedContextSummaryV1]
    public let unverifiedClues: [UnverifiedClueSummaryV1]

    public init(
        bundleHash: String,
        verifiedContexts: [VerifiedContextSummaryV1],
        unverifiedClues: [UnverifiedClueSummaryV1]
    ) {
        self.bundleHash = bundleHash
        self.verifiedContexts = verifiedContexts
        self.unverifiedClues = unverifiedClues
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard dictionary["contract_version"] as? String == "ContextBundleV1",
              Phase2ContractPolicy.strictBool(dictionary["raw_access"]) == false,
              dictionary["status"] as? String == "available",
              let hash = Phase2ContractPolicy.sha256(dictionary["bundle_hash"]),
              let verifiedRaw = dictionary["verified_contexts"] as? [[String: Any]],
              let cluesRaw = dictionary["unverified_clues"] as? [[String: Any]],
              verifiedRaw.count <= 200,
              cluesRaw.count <= 200
        else {
            throw Phase2ContractError.invalid("invalid_context_bundle")
        }
        let verified = try verifiedRaw.map(VerifiedContextSummaryV1.init(dictionary:))
        let clues = try cluesRaw.map(UnverifiedClueSummaryV1.init(dictionary:))
        let verifiedSources = Set(verified.map(\.sourceRef))
        let clueSources = Set(clues.map(\.sourceRef))
        guard verifiedSources.isDisjoint(with: clueSources) else {
            throw Phase2ContractError.invalid("bundle_partition_overlap")
        }
        self.init(bundleHash: hash, verifiedContexts: verified, unverifiedClues: clues)
    }
}

public struct ContextProjectSnapshotV1: Equatable, Sendable {
    public let project: ContextLabelV1
    public let verifiedItems: [VerifiedContextSummaryV1]
    public let compatibilityItems: [LegacyConfirmedSummaryV1]
    public let clues: [UnverifiedClueSummaryV1]
    public let nextStep: VerifiedContextSummaryV1?
    public let latestOccurredAt: String

    public init(
        project: ContextLabelV1,
        verifiedItems: [VerifiedContextSummaryV1],
        compatibilityItems: [LegacyConfirmedSummaryV1] = [],
        clues: [UnverifiedClueSummaryV1],
        nextStep: VerifiedContextSummaryV1?,
        latestOccurredAt: String = ""
    ) {
        self.project = project
        self.verifiedItems = verifiedItems
        self.compatibilityItems = compatibilityItems
        self.clues = clues
        self.nextStep = nextStep
        self.latestOccurredAt = latestOccurredAt
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let projectRaw = dictionary["project"] as? [String: Any],
              let verifiedRaw = dictionary["verified_contexts"] as? [[String: Any]],
              let compatibilityRaw = dictionary["compatibility_contexts"] as? [[String: Any]],
              let cluesRaw = dictionary["unverified_clues"] as? [[String: Any]],
              let latestOccurredAt = Phase2ContractPolicy.utcTimestamp(dictionary["latest_occurred_at"]),
              verifiedRaw.count <= 100,
              compatibilityRaw.count <= 100,
              cluesRaw.count <= 100
        else {
            throw Phase2ContractError.invalid("invalid_project_snapshot")
        }
        let project = try ContextLabelV1(dictionary: projectRaw, expectedPrefix: "project_")
        let verified = try verifiedRaw.map(VerifiedContextSummaryV1.init(dictionary:))
        let compatibility = try compatibilityRaw.map(LegacyConfirmedSummaryV1.init(dictionary:))
        let clues = try cluesRaw.map(UnverifiedClueSummaryV1.init(dictionary:))
        guard verified.allSatisfy({ $0.project.ref == project.ref }) else {
            throw Phase2ContractError.invalid("project_snapshot_mismatch")
        }
        guard compatibility.allSatisfy({ $0.project.ref == project.ref }) else {
            throw Phase2ContractError.invalid("compatibility_project_mismatch")
        }
        guard clues.allSatisfy({ $0.project.ref == project.ref }) else {
            throw Phase2ContractError.invalid("clue_project_mismatch")
        }
        let verifiedSourceRefs = Set(verified.map(\.sourceRef))
        guard verifiedSourceRefs.isDisjoint(with: Set(clues.map(\.sourceRef))) else {
            throw Phase2ContractError.invalid("project_trust_partition_overlap")
        }
        self.init(
            project: project,
            verifiedItems: verified,
            compatibilityItems: compatibility,
            clues: clues,
            // `in_progress` proves only current activity. It is not an
            // owner-confirmed future action and therefore cannot fill the
            // "next step" slot.
            nextStep: nil,
            latestOccurredAt: latestOccurredAt
        )
    }
}

public struct LegacyConfirmedSummaryV1: Equatable, Sendable {
    public let contextRef: String
    public let project: ContextLabelV1
    public let safeLabel: String
    public let occurredAt: String
    public let expiresAt: String
    public let state: String
    public let sourceLabel: String

    public init(
        contextRef: String,
        project: ContextLabelV1,
        safeLabel: String,
        occurredAt: String,
        expiresAt: String,
        state: String,
        sourceLabel: String
    ) {
        self.contextRef = contextRef
        self.project = project
        self.safeLabel = safeLabel
        self.occurredAt = occurredAt
        self.expiresAt = expiresAt
        self.state = state
        self.sourceLabel = sourceLabel
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let contextRef = Phase2ContractPolicy.publicRef(dictionary["context_ref"], prefixes: ["context_"]),
        let projectRaw = dictionary["project"] as? [String: Any],
        let safeLabel = Phase2ContractPolicy.safeLabel(dictionary["summary"]),
        let occurredAt = Phase2ContractPolicy.utcTimestamp(dictionary["occurred_at"]),
        let expiresAt = Phase2ContractPolicy.utcTimestamp(dictionary["expires_at"]),
        let state = Phase2ContractPolicy.safeCode(dictionary["state"], maximumLength: 48),
        let sourceRaw = dictionary["source"] as? [String: Any],
        let sourceLabel = Phase2ContractPolicy.safeLabel(sourceRaw["label"]),
        Phase2ContractPolicy.strictBool(dictionary["compatibility_source"]) == true,
        Phase2ContractPolicy.strictBool(dictionary["external_delivery_eligible"]) == false,
        Phase2ContractPolicy.strictBool(dictionary["memory_candidate_eligible"]) == false,
        Phase2ContractPolicy.strictBool(dictionary["raw_access"]) == false
        else {
            throw Phase2ContractError.invalid("invalid_legacy_confirmation")
        }
        self.init(
            contextRef: contextRef,
            project: try ContextLabelV1(dictionary: projectRaw, expectedPrefix: "project_"),
            safeLabel: safeLabel,
            occurredAt: occurredAt,
            expiresAt: expiresAt,
            state: state,
            sourceLabel: sourceLabel
        )
    }
}

public struct ConfirmationCardSummaryV1: Equatable, Sendable {
    public let confirmationRef: String
    public let question: String
    public let project: ContextLabelV1?
    public let revision: String
    public let answerable: Bool
    public let title: String
    public let evidence: String
    public let priority: String
    public let eventType: String
    public let app: String
    public let timeLabel: String
    public let observedStart: String
    public let observedEnd: String
    public let answerOptions: [String]

    public init(
        confirmationRef: String,
        question: String,
        project: ContextLabelV1?,
        revision: String,
        answerable: Bool,
        title: String = "",
        evidence: String = "",
        priority: String = "normal",
        eventType: String = "",
        app: String = "",
        timeLabel: String = "",
        observedStart: String = "",
        observedEnd: String = "",
        answerOptions: [String] = []
    ) {
        self.confirmationRef = confirmationRef
        self.question = question
        self.project = project
        self.revision = revision
        self.answerable = answerable
        self.title = title
        self.evidence = evidence
        self.priority = priority
        self.eventType = eventType
        self.app = app
        self.timeLabel = timeLabel
        self.observedStart = observedStart
        self.observedEnd = observedEnd
        self.answerOptions = answerOptions
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let confirmationRef = Phase2ContractPolicy.publicRef(
            dictionary["confirmation_ref"],
            prefixes: ["confirmation_", "acq_"]
        ),
        let question = Phase2ContractPolicy.safeUserText(dictionary["question"], maximumLength: 320),
        let revision = Phase2ContractPolicy.safeCode(dictionary["revision"], maximumLength: 128),
        let title = Phase2ContractPolicy.safeUserText(dictionary["title"], maximumLength: 160),
        let evidence = Phase2ContractPolicy.safeUserText(dictionary["evidence"], maximumLength: 320),
        let priority = Phase2ContractPolicy.safeCode(dictionary["priority"], maximumLength: 32),
        let eventType = Phase2ContractPolicy.safeCode(dictionary["event_type"], maximumLength: 48),
        let app = Phase2ContractPolicy.safeLabel(dictionary["app"]),
        let timeLabel = Phase2ContractPolicy.safeLabel(dictionary["time_label"]),
        let observedStart = Phase2ContractPolicy.utcTimestamp(dictionary["observed_start"]),
        let observedEnd = Phase2ContractPolicy.utcTimestamp(dictionary["observed_end"]),
        let answerOptions = try? Phase2ContractPolicy.safeStringArray(
            dictionary["answer_options"],
            maximumItems: 8,
            maximumLength: 64
        ),
        dictionary["verification_state"] as? String == "unverified",
        Phase2ContractPolicy.strictBool(dictionary["raw_access"]) == false,
        !question.localizedCaseInsensitiveContains("information_insufficient")
        else {
            throw Phase2ContractError.invalid("invalid_confirmation_question")
        }
        self.init(
            confirmationRef: confirmationRef,
            question: question,
            project: nil,
            revision: revision,
            answerable: true,
            title: title,
            evidence: evidence,
            priority: priority,
            eventType: eventType,
            app: app,
            timeLabel: timeLabel,
            observedStart: observedStart,
            observedEnd: observedEnd,
            answerOptions: answerOptions
        )
    }
}

public struct ContextSnapshotCapabilitiesV1: Equatable, Sendable {
    public let search: Bool
    public let quickCapture: Bool
    public let confirmation: Bool
    public let ownerAction: Bool
    public let localHandoff: Bool
    public let productFeedback: Bool

    public init(
        search: Bool,
        quickCapture: Bool,
        confirmation: Bool,
        ownerAction: Bool? = nil,
        localHandoff: Bool = false,
        productFeedback: Bool = false
    ) {
        self.search = search
        self.quickCapture = quickCapture
        self.confirmation = confirmation
        self.ownerAction = ownerAction ?? quickCapture
        self.localHandoff = localHandoff
        self.productFeedback = productFeedback
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let search = Phase2ContractPolicy.strictBool(dictionary["search"]),
              let ownerAction = Phase2ContractPolicy.strictBool(dictionary["owner_action"]),
              let localHandoff = Phase2ContractPolicy.strictBool(dictionary["local_handoff"]),
              let productFeedback = Phase2ContractPolicy.strictBool(dictionary["product_feedback"])
        else {
            throw Phase2ContractError.invalid("invalid_snapshot_capabilities")
        }
        self.init(
            search: search,
            quickCapture: ownerAction,
            confirmation: ownerAction,
            ownerAction: ownerAction,
            localHandoff: localHandoff,
            productFeedback: productFeedback
        )
    }
}

public struct ContextSnapshotCountsV1: Equatable, Sendable {
    public let verifiedContexts: Int
    public let unverifiedClues: Int
    public let legacyConfirmed: Int
    public let pendingConfirmations: Int

    public init(
        verifiedContexts: Int,
        unverifiedClues: Int,
        legacyConfirmed: Int,
        pendingConfirmations: Int
    ) {
        self.verifiedContexts = verifiedContexts
        self.unverifiedClues = unverifiedClues
        self.legacyConfirmed = legacyConfirmed
        self.pendingConfirmations = pendingConfirmations
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let verified = Phase2ContractPolicy.nonnegativeInt(dictionary["verified_contexts"]),
              let clues = Phase2ContractPolicy.nonnegativeInt(dictionary["unverified_clues"]),
              let legacy = Phase2ContractPolicy.nonnegativeInt(dictionary["legacy_confirmed"]),
              let pending = Phase2ContractPolicy.nonnegativeInt(dictionary["pending_confirmations"])
        else {
            throw Phase2ContractError.invalid("invalid_snapshot_counts")
        }
        self.init(
            verifiedContexts: verified,
            unverifiedClues: clues,
            legacyConfirmed: legacy,
            pendingConfirmations: pending
        )
    }
}

public struct ContextSnapshotV1: Equatable, Sendable {
    public let readState: Phase2ReadState
    public let observedAt: String
    public let lastSuccessfulReadAt: String?
    public let actionsEnabled: Bool
    public let counts: ContextSnapshotCountsV1?
    public let bundle: ContextBundleProjectionV1?
    public let projects: [ContextProjectSnapshotV1]?
    public let legacyConfirmed: [LegacyConfirmedSummaryV1]?
    public let confirmationCards: [ConfirmationCardSummaryV1]?
    public let capabilities: ContextSnapshotCapabilitiesV1?

    public init(
        readState: Phase2ReadState,
        observedAt: String,
        lastSuccessfulReadAt: String?,
        actionsEnabled: Bool,
        counts: ContextSnapshotCountsV1?,
        bundle: ContextBundleProjectionV1?,
        projects: [ContextProjectSnapshotV1]?,
        legacyConfirmed: [LegacyConfirmedSummaryV1]?,
        confirmationCards: [ConfirmationCardSummaryV1]?,
        capabilities: ContextSnapshotCapabilitiesV1?
    ) {
        self.readState = readState
        self.observedAt = observedAt
        self.lastSuccessfulReadAt = lastSuccessfulReadAt
        self.actionsEnabled = actionsEnabled
        self.counts = counts
        self.bundle = bundle
        self.projects = projects
        self.legacyConfirmed = legacyConfirmed
        self.confirmationCards = confirmationCards
        self.capabilities = capabilities
    }

    public init(data: Data) throws {
        try self.init(dictionary: Phase2ContractPolicy.dictionary(from: data))
    }

    public init(dictionary: [String: Any]) throws {
        try Phase2ContractPolicy.validatePublicProjection(dictionary)
        let envelope = try Phase2ReadEnvelope(
            dictionary: dictionary,
            contractVersion: "ContextSnapshotV1"
        )
        let contentKeys = [
            "counts", "context_bundle_v1", "projects", "legacy_confirmed",
            "confirmation_questions", "capabilities",
        ]
        guard envelope.readState == .available else {
            try Phase2ContractPolicy.requireNullOrAbsent(dictionary, keys: contentKeys)
            self.init(
                readState: envelope.readState,
                observedAt: envelope.observedAt,
                lastSuccessfulReadAt: envelope.lastSuccessfulReadAt,
                actionsEnabled: false,
                counts: nil,
                bundle: nil,
                projects: nil,
                legacyConfirmed: nil,
                confirmationCards: nil,
                capabilities: nil
            )
            return
        }
        guard let countsRaw = dictionary["counts"] as? [String: Any],
              let bundleRaw = dictionary["context_bundle_v1"] as? [String: Any],
              let projectsRaw = dictionary["projects"] as? [[String: Any]],
              let legacyRaw = dictionary["legacy_confirmed"] as? [[String: Any]],
              let questionsRaw = dictionary["confirmation_questions"] as? [[String: Any]],
              let capabilitiesRaw = dictionary["capabilities"] as? [String: Any],
              projectsRaw.count <= 50,
              legacyRaw.count <= 100,
              questionsRaw.count <= 50
        else {
            throw Phase2ContractError.invalid("available_snapshot_incomplete")
        }
        let counts = try ContextSnapshotCountsV1(dictionary: countsRaw)
        let projects = try projectsRaw.map(ContextProjectSnapshotV1.init(dictionary:))
        let legacy = try legacyRaw.map(LegacyConfirmedSummaryV1.init(dictionary:))
        let questions = try questionsRaw.map(ConfirmationCardSummaryV1.init(dictionary:))
        guard counts.legacyConfirmed == legacy.count,
              counts.pendingConfirmations >= questions.count
        else {
            throw Phase2ContractError.invalid("snapshot_count_mismatch")
        }
        let capabilities = try ContextSnapshotCapabilitiesV1(dictionary: capabilitiesRaw)
        self.init(
            readState: .available,
            observedAt: envelope.observedAt,
            lastSuccessfulReadAt: envelope.lastSuccessfulReadAt,
            actionsEnabled: envelope.actionsEnabled && capabilities.ownerAction,
            counts: counts,
            bundle: try ContextBundleProjectionV1(dictionary: bundleRaw),
            projects: projects,
            legacyConfirmed: legacy,
            confirmationCards: questions,
            capabilities: capabilities
        )
    }
}

// MARK: - Memory candidate projection

public struct MemoryCandidateSummaryV1: Equatable, Sendable {
    public let memoryCandidateRef: String
    public let contextRef: String
    public let safeSummary: String
    public let project: ContextLabelV1
    public let state: String
    public let sourceLabel: String
    public let expiresAt: String
    public let revoked: Bool

    public init(
        memoryCandidateRef: String,
        contextRef: String,
        safeSummary: String,
        project: ContextLabelV1,
        state: String,
        sourceLabel: String,
        expiresAt: String,
        revoked: Bool
    ) {
        self.memoryCandidateRef = memoryCandidateRef
        self.contextRef = contextRef
        self.safeSummary = safeSummary
        self.project = project
        self.state = state
        self.sourceLabel = sourceLabel
        self.expiresAt = expiresAt
        self.revoked = revoked
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let memoryRef = Phase2ContractPolicy.publicRef(
            dictionary["memory_candidate_ref"],
            prefixes: ["memory_"]
        ),
        let contextRef = Phase2ContractPolicy.publicRef(dictionary["context_ref"], prefixes: ["context_"]),
        let safeSummary = Phase2ContractPolicy.safeUserText(
            dictionary["safe_summary"],
            maximumLength: 320
        ),
        let projectRaw = dictionary["project"] as? [String: Any],
        let state = Phase2ContractPolicy.safeCode(dictionary["state"], maximumLength: 48),
        let expiresAt = Phase2ContractPolicy.utcTimestamp(dictionary["expires_at"]),
        let revocationState = Phase2ContractPolicy.safeCode(
            dictionary["revocation_state"],
            maximumLength: 32
        ),
        Phase2ContractPolicy.strictBool(dictionary["direct_write"]) == false,
        Phase2ContractPolicy.strictBool(dictionary["writer_enabled"]) == false
        else {
            throw Phase2ContractError.invalid("invalid_memory_candidate")
        }
        let sourceLabel = Phase2ContractPolicy.safeLabel(dictionary["source_label"])
            ?? Phase2ContractPolicy.safeCode(dictionary["source_kind"], maximumLength: 48)
            ?? "已验证上下文"
        self.init(
            memoryCandidateRef: memoryRef,
            contextRef: contextRef,
            safeSummary: safeSummary,
            project: try ContextLabelV1(dictionary: projectRaw, expectedPrefix: "project_"),
            state: state,
            sourceLabel: sourceLabel,
            expiresAt: expiresAt,
            revoked: revocationState != "active"
        )
    }
}

public struct MemoryCandidateListV1: Equatable, Sendable {
    public let readState: Phase2ReadState
    public let observedAt: String
    public let candidates: [MemoryCandidateSummaryV1]?
    public let actionsEnabled: Bool

    public init(
        readState: Phase2ReadState,
        observedAt: String,
        candidates: [MemoryCandidateSummaryV1]?,
        actionsEnabled: Bool
    ) {
        self.readState = readState
        self.observedAt = observedAt
        self.candidates = candidates
        self.actionsEnabled = actionsEnabled
    }

    public init(data: Data) throws {
        try self.init(dictionary: Phase2ContractPolicy.dictionary(from: data))
    }

    public init(dictionary: [String: Any]) throws {
        try Phase2ContractPolicy.validatePublicProjection(dictionary)
        let envelope = try Phase2ReadEnvelope(
            dictionary: dictionary,
            contractVersion: "MemoryCandidateListV1"
        )
        guard envelope.readState == .available else {
            try Phase2ContractPolicy.requireNullOrAbsent(dictionary, keys: ["candidates", "actions"])
            self.init(
                readState: envelope.readState,
                observedAt: envelope.observedAt,
                candidates: nil,
                actionsEnabled: false
            )
            return
        }
        guard let raw = dictionary["candidates"] as? [[String: Any]], raw.count <= 200,
              dictionary["actions"] == nil || dictionary["actions"] is NSNull,
              envelope.actionsEnabled == false
        else {
            throw Phase2ContractError.invalid("memory_actions_forbidden")
        }
        self.init(
            readState: .available,
            observedAt: envelope.observedAt,
            candidates: try raw.map(MemoryCandidateSummaryV1.init(dictionary:)),
            actionsEnabled: false
        )
    }
}

// MARK: - Delivery projection

public enum DeliveryStage: String, CaseIterable, Equatable, Sendable {
    case needsInput = "needs_input"
    case prepared
    case consumed
    case delivered
    case failed
}

public struct DeliveryProjectStatusV1: Equatable, Sendable {
    public let project: ContextLabelV1
    public let stage: DeliveryStage
    public let stageAt: String?
    public let withdrawn: Bool

    public init(
        project: ContextLabelV1,
        stage: DeliveryStage,
        stageAt: String?,
        withdrawn: Bool = false
    ) {
        self.project = project
        self.stage = stage
        self.stageAt = stageAt
        self.withdrawn = withdrawn
    }
}

public struct DailyReportDeliveryStatusV1: Equatable, Sendable {
    public let needsInput: Bool
    public let pendingConfirmationCount: Int
    public let currentState: DeliveryStage
    public let preparedAt: String?
    public let consumedAt: String?
    public let deliveredAt: String?
    public let failedAt: String?

    public init(
        needsInput: Bool,
        pendingConfirmationCount: Int,
        currentState: DeliveryStage,
        preparedAt: String?,
        consumedAt: String?,
        deliveredAt: String?,
        failedAt: String?
    ) {
        self.needsInput = needsInput
        self.pendingConfirmationCount = pendingConfirmationCount
        self.currentState = currentState
        self.preparedAt = preparedAt
        self.consumedAt = consumedAt
        self.deliveredAt = deliveredAt
        self.failedAt = failedAt
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let needsInput = Phase2ContractPolicy.strictBool(dictionary["needs_input"]),
              let pending = Phase2ContractPolicy.nonnegativeInt(dictionary["pending_confirmation_count"]),
              let stateText = dictionary["current_state"] as? String,
              let state = DeliveryStage(rawValue: stateText)
        else {
            throw Phase2ContractError.invalid("invalid_daily_report_status")
        }
        let prepared = try Phase2ContractPolicy.optionalUTCTimestamp(dictionary["prepared_at"])
        let consumed = try Phase2ContractPolicy.optionalUTCTimestamp(dictionary["consumed_at"])
        let delivered = try Phase2ContractPolicy.optionalUTCTimestamp(dictionary["delivered_at"])
        let failed = try Phase2ContractPolicy.optionalUTCTimestamp(dictionary["failed_at"])
        guard !(state == .delivered && delivered == nil),
              !(state == .failed && failed == nil),
              !(state == .consumed && consumed == nil),
              !(state == .prepared && prepared == nil),
              !(needsInput && state != .needsInput)
        else {
            throw Phase2ContractError.invalid("delivery_stage_receipt_missing")
        }
        self.init(
            needsInput: needsInput,
            pendingConfirmationCount: pending,
            currentState: state,
            preparedAt: prepared,
            consumedAt: consumed,
            deliveredAt: delivered,
            failedAt: failed
        )
    }
}

public struct DeliveryAttemptSummaryV1: Equatable, Sendable {
    public let deliveryRef: String
    public let bundleRef: String
    public let attemptNumber: Int
    public let project: ContextLabelV1
    public let target: ContextLabelV1
    public let stage: DeliveryStage
    public let preparedAt: String?
    public let consumedAt: String?
    public let deliveredAt: String?
    public let failedAt: String?
    public let failureCategory: String?
    public let withdrawn: Bool

    public var stageAt: String? {
        switch stage {
        case .needsInput: return nil
        case .prepared: return preparedAt
        case .consumed: return consumedAt
        case .delivered: return deliveredAt
        case .failed: return failedAt
        }
    }

    fileprivate init(dictionary: [String: Any]) throws {
        guard let deliveryRef = Phase2ContractPolicy.publicRef(
            dictionary["delivery_ref"],
            prefixes: ["delivery_"]
        ),
        let bundleRef = Phase2ContractPolicy.publicRef(dictionary["bundle_ref"], prefixes: ["bundle_"]),
        let attemptNumber = Phase2ContractPolicy.positiveInt(dictionary["attempt_no"]),
        let projectRaw = dictionary["project"] as? [String: Any],
        let targetRaw = dictionary["target"] as? [String: Any],
        let stateText = dictionary["state"] as? String,
        let stage = DeliveryStage(rawValue: stateText),
        let withdrawalState = Phase2ContractPolicy.safeCode(
            dictionary["withdrawal_state"],
            maximumLength: 32
        )
        else {
            throw Phase2ContractError.invalid("invalid_delivery_attempt")
        }
        let prepared = try Phase2ContractPolicy.optionalUTCTimestamp(dictionary["prepared_at"])
        let consumed = try Phase2ContractPolicy.optionalUTCTimestamp(dictionary["consumed_at"])
        let delivered = try Phase2ContractPolicy.optionalUTCTimestamp(dictionary["delivered_at"])
        let failed = try Phase2ContractPolicy.optionalUTCTimestamp(dictionary["failed_at"])
        let failure = try Phase2ContractPolicy.optionalSafeCode(
            dictionary["failure_category"],
            maximumLength: 48
        )
        guard !(stage == .delivered && delivered == nil),
              !(stage == .failed && failed == nil),
              !(stage == .consumed && consumed == nil),
              !(stage == .prepared && prepared == nil)
        else {
            throw Phase2ContractError.invalid("delivery_stage_receipt_missing")
        }
        self.deliveryRef = deliveryRef
        self.bundleRef = bundleRef
        self.attemptNumber = attemptNumber
        self.project = try ContextLabelV1(dictionary: projectRaw, expectedPrefix: "project_")
        self.target = try ContextLabelV1(dictionary: targetRaw, expectedPrefix: "target_")
        self.stage = stage
        self.preparedAt = prepared
        self.consumedAt = consumed
        self.deliveredAt = delivered
        self.failedAt = failed
        self.failureCategory = failure
        self.withdrawn = withdrawalState != "active"
    }
}

public struct DeliveryStatusV1: Equatable, Sendable {
    public let readState: Phase2ReadState
    public let observedAt: String
    public let dailyReport: DailyReportDeliveryStatusV1?
    public let deliveries: [DeliveryAttemptSummaryV1]?
    public let projects: [DeliveryProjectStatusV1]?

    public init(
        readState: Phase2ReadState,
        observedAt: String,
        dailyReport: DailyReportDeliveryStatusV1?,
        deliveries: [DeliveryAttemptSummaryV1]?,
        projects: [DeliveryProjectStatusV1]?
    ) {
        self.readState = readState
        self.observedAt = observedAt
        self.dailyReport = dailyReport
        self.deliveries = deliveries
        self.projects = projects
    }

    public init(data: Data) throws {
        try self.init(dictionary: Phase2ContractPolicy.dictionary(from: data))
    }

    public init(dictionary: [String: Any]) throws {
        try Phase2ContractPolicy.validatePublicProjection(dictionary)
        let envelope = try Phase2ReadEnvelope(
            dictionary: dictionary,
            contractVersion: "DeliveryStatusV1"
        )
        guard envelope.readState == .available else {
            try Phase2ContractPolicy.requireNullOrAbsent(
                dictionary,
                keys: ["daily_report", "deliveries"]
            )
            self.init(
                readState: envelope.readState,
                observedAt: envelope.observedAt,
                dailyReport: nil,
                deliveries: nil,
                projects: nil
            )
            return
        }
        guard let dailyRaw = dictionary["daily_report"] as? [String: Any],
              let raw = dictionary["deliveries"] as? [[String: Any]],
              raw.count <= 200
        else {
            throw Phase2ContractError.invalid("delivery_status_incomplete")
        }
        let attempts = try raw.map(DeliveryAttemptSummaryV1.init(dictionary:))
        let latestByProject = Dictionary(grouping: attempts, by: \.project.ref)
            .compactMap { _, entries -> DeliveryProjectStatusV1? in
                guard let latest = entries.max(by: { $0.attemptNumber < $1.attemptNumber }) else {
                    return nil
                }
                return DeliveryProjectStatusV1(
                    project: latest.project,
                    stage: latest.stage,
                    stageAt: latest.stageAt,
                    withdrawn: latest.withdrawn
                )
            }
            .sorted { $0.project.displayLabel.localizedStandardCompare($1.project.displayLabel) == .orderedAscending }
        self.init(
            readState: .available,
            observedAt: envelope.observedAt,
            dailyReport: try DailyReportDeliveryStatusV1(dictionary: dailyRaw),
            deliveries: attempts,
            projects: latestByProject
        )
    }
}

// MARK: - Verified-only local handoff

public struct HandoffSourceV1: Equatable, Sendable {
    public let contextRef: String
    public let safeLabel: String
    public let sourceLabel: String

    public init(contextRef: String, safeLabel: String, sourceLabel: String) {
        self.contextRef = contextRef
        self.safeLabel = safeLabel
        self.sourceLabel = sourceLabel
    }
}

public struct LocalHandoffV1: Equatable, Sendable {
    public let readState: Phase2ReadState
    public let observedAt: String
    public let handoffRef: String?
    public let bundleHash: String?
    public let project: ContextLabelV1?
    public let objective: String?
    public let results: [String]?
    public let blockers: [String]?
    public let nextSteps: [String]?
    public let sources: [HandoffSourceV1]?
    public let unresolvedQuestions: [String]?
    public let expiresAt: String?
    public let revoked: Bool

    public init(
        readState: Phase2ReadState,
        observedAt: String,
        handoffRef: String?,
        bundleHash: String?,
        project: ContextLabelV1?,
        objective: String?,
        results: [String]?,
        blockers: [String]?,
        nextSteps: [String]?,
        sources: [HandoffSourceV1]?,
        unresolvedQuestions: [String]?,
        expiresAt: String?,
        revoked: Bool
    ) {
        self.readState = readState
        self.observedAt = observedAt
        self.handoffRef = handoffRef
        self.bundleHash = bundleHash
        self.project = project
        self.objective = objective
        self.results = results
        self.blockers = blockers
        self.nextSteps = nextSteps
        self.sources = sources
        self.unresolvedQuestions = unresolvedQuestions
        self.expiresAt = expiresAt
        self.revoked = revoked
    }

    public init(data: Data) throws {
        try self.init(dictionary: Phase2ContractPolicy.dictionary(from: data))
    }

    public init(dictionary: [String: Any]) throws {
        try Phase2ContractPolicy.validatePublicProjection(dictionary)
        let envelope = try Phase2ReadEnvelope(
            dictionary: dictionary,
            contractVersion: "LocalHandoffV1"
        )
        let contentKeys = [
            "handoff_ref", "bundle_hash", "project", "verified_contexts",
            "unresolved_questions", "expires_at", "revocation_state",
        ]
        guard envelope.readState == .available else {
            try Phase2ContractPolicy.requireNullOrAbsent(dictionary, keys: contentKeys)
            self.init(
                readState: envelope.readState,
                observedAt: envelope.observedAt,
                handoffRef: nil,
                bundleHash: nil,
                project: nil,
                objective: nil,
                results: nil,
                blockers: nil,
                nextSteps: nil,
                sources: nil,
                unresolvedQuestions: nil,
                expiresAt: nil,
                revoked: false
            )
            return
        }
        guard let handoffRef = Phase2ContractPolicy.publicRef(
            dictionary["handoff_ref"],
            prefixes: ["bundle_", "handoff_"]
        ),
        let bundleHash = Phase2ContractPolicy.sha256(dictionary["bundle_hash"]),
        let projectRaw = dictionary["project"] as? [String: Any],
        let verifiedRaw = dictionary["verified_contexts"] as? [[String: Any]],
        let unresolved = try? Phase2ContractPolicy.safeStringArray(
            dictionary["unresolved_questions"],
            maximumItems: 50,
            maximumLength: 320
        ),
        let expiresAt = Phase2ContractPolicy.utcTimestamp(dictionary["expires_at"]),
        let revocationState = Phase2ContractPolicy.safeCode(
            dictionary["revocation_state"],
            maximumLength: 32
        )
        else {
            throw Phase2ContractError.invalid("local_handoff_incomplete")
        }
        let verified = try verifiedRaw.map(VerifiedContextSummaryV1.init(dictionary:))
        let project = try ContextLabelV1(dictionary: projectRaw, expectedPrefix: "project_")
        guard verified.allSatisfy({ $0.project.ref == project.ref }) else {
            throw Phase2ContractError.invalid("handoff_project_mismatch")
        }
        let objective = verified.first(where: { $0.state == "intent_observed" })?.safeLabel
            ?? "尚无已验证目标"
        let results = verified
            .filter { ["completed_confirmed", "external_delivered", "artifact_committed", "artifact_saved"].contains($0.state) }
            .map(\.safeLabel)
        let blockers = verified.filter { $0.state == "blocked" }.map(\.safeLabel)
        // No current Phase 2 state represents an explicitly confirmed future
        // action. Keep this partition empty instead of relabeling progress.
        let nextSteps: [String] = []
        let sources = verified.map {
            HandoffSourceV1(
                contextRef: $0.contextRef,
                safeLabel: $0.safeLabel,
                sourceLabel: $0.sourceLabel
            )
        }
        self.init(
            readState: .available,
            observedAt: envelope.observedAt,
            handoffRef: handoffRef,
            bundleHash: bundleHash,
            project: project,
            objective: objective,
            results: results,
            blockers: blockers,
            nextSteps: nextSteps,
            sources: sources,
            unresolvedQuestions: unresolved,
            expiresAt: expiresAt,
            revoked: revocationState != "active"
        )
    }
}

// MARK: - Owner action and feedback contracts

public enum OwnerContextActionKind: String, Equatable, Sendable {
    case confirmCard = "confirm_card"
    case manualNote = "manual_note"
}

public enum OwnerConfirmationAnswer: String, Equatable, Sendable {
    case occurred
    case notOccurred = "not_occurred"
    case ignored
    case deferred = "defer"
}

public struct OwnerContextActionV1: Equatable, Sendable {
    public static let contractVersion = "OwnerContextActionV1"
    public static let explicitConfirmation = "CONFIRM_OWNER_CONTEXT_ACTION"

    public let action: OwnerContextActionKind
    public let idempotencyKey: String
    public let explicitlyConfirmedOwner: Bool
    public let confirmationRef: String?
    public let expectedRevision: String?
    public let answer: OwnerConfirmationAnswer?
    public let reason: String?
    public let safeLabel: String?
    public let workstream: String?
    public let outcome: String?
    public let startedAt: String?
    public let endedAt: String?
    public let projectRef: String?
    public let projectLabel: String?
    public let workstreamRef: String?
    public let objectRef: String?
    public let versionRef: String?
    public let subjectRef: String?
    public let privacyClass: String?
    public let contextState: String?

    public init(
        action: OwnerContextActionKind,
        idempotencyKey: String,
        explicitlyConfirmedOwner: Bool,
        confirmationRef: String? = nil,
        expectedRevision: String? = nil,
        answer: OwnerConfirmationAnswer? = nil,
        reason: String? = nil,
        safeLabel: String? = nil,
        workstream: String? = nil,
        outcome: String? = nil,
        startedAt: String? = nil,
        endedAt: String? = nil,
        projectRef: String? = nil,
        projectLabel: String? = nil,
        workstreamRef: String? = nil,
        objectRef: String? = nil,
        versionRef: String? = nil,
        subjectRef: String? = nil,
        privacyClass: String? = nil,
        contextState: String? = nil
    ) {
        self.action = action
        self.idempotencyKey = idempotencyKey
        self.explicitlyConfirmedOwner = explicitlyConfirmedOwner
        self.confirmationRef = confirmationRef
        self.expectedRevision = expectedRevision
        self.answer = answer
        self.reason = reason
        self.safeLabel = safeLabel
        self.workstream = workstream
        self.outcome = outcome
        self.startedAt = startedAt
        self.endedAt = endedAt
        self.projectRef = projectRef
        self.projectLabel = projectLabel
        self.workstreamRef = workstreamRef
        self.objectRef = objectRef
        self.versionRef = versionRef
        self.subjectRef = subjectRef
        self.privacyClass = privacyClass
        self.contextState = contextState
    }

    public func encodedStandardInput() throws -> Data {
        guard Phase2ContractPolicy.publicRef(idempotencyKey, prefixes: ["idem_"]) != nil,
              explicitlyConfirmedOwner
        else {
            throw Phase2ContractError.invalid("owner_action_not_confirmed")
        }
        var payload: [String: Any] = [
            "contract_version": Self.contractVersion,
            "action": action.rawValue,
            "idempotency_key": idempotencyKey,
            "confirmation_action": Self.explicitConfirmation,
        ]
        switch action {
        case .confirmCard:
            guard let confirmationRef = Phase2ContractPolicy.publicRef(
                confirmationRef,
                prefixes: ["confirmation_", "acq_"]
            ),
            let expectedRevision = Phase2ContractPolicy.safeCode(expectedRevision, maximumLength: 128),
            let answer
            else {
                throw Phase2ContractError.invalid("confirmation_action_incomplete")
            }
            payload["confirmation_ref"] = confirmationRef
            payload["expected_revision"] = expectedRevision
            payload["answer"] = answer.rawValue
            if let reason {
                guard let safeReason = Phase2ContractPolicy.safeCode(reason, maximumLength: 64) else {
                    throw Phase2ContractError.invalid("invalid_confirmation_reason")
                }
                payload["reason"] = safeReason
            }
        case .manualNote:
            guard let safeLabel = Phase2ContractPolicy.safeUserText(
                safeLabel,
                maximumLength: QuickCaptureDraft.maximumTextLength
            ),
            let startedAt = Phase2ContractPolicy.utcTimestamp(startedAt),
            let endedAt = Phase2ContractPolicy.utcTimestamp(endedAt)
            else {
                throw Phase2ContractError.invalid("manual_note_incomplete")
            }
            payload["safe_label"] = safeLabel
            payload["started_at"] = startedAt
            payload["ended_at"] = endedAt
            guard let outcome = Phase2ContractPolicy.safeCode(outcome, maximumLength: 32),
                  ["completed", "progressing", "blocked", "preparation_only", "unknown"]
                    .contains(outcome)
            else {
                throw Phase2ContractError.invalid("invalid_outcome")
            }
            payload["outcome"] = outcome
            if let projectRef {
                guard let ref = Phase2ContractPolicy.publicRef(
                    projectRef,
                    prefixes: ["project_"]
                ) else {
                    throw Phase2ContractError.invalid("invalid_project_ref")
                }
                payload["project_ref"] = ref
            }
            if let projectLabel {
                guard let label = Phase2ContractPolicy.safeUserText(
                    projectLabel,
                    maximumLength: 80
                ) else {
                    throw Phase2ContractError.invalid("invalid_project_label")
                }
                payload["project_label"] = label
            }
        }
        return try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    }

    public func coreCommandRequest() throws -> CoreCommandRequest {
        try ContextConsoleCoreCommands.ownerAction(self)
    }
}

public struct OwnerContextActionResultV1: Equatable, Sendable {
    public let status: String
    public let actionRef: String?
    public let confirmationRef: String?
    public let contextRef: String?
    public let verificationState: String?

    public init(data: Data) throws {
        let dictionary = try Phase2ContractPolicy.dictionary(from: data)
        try Phase2ContractPolicy.validatePublicProjection(dictionary)
        guard dictionary["contract_version"] as? String == "OwnerContextActionV1",
              Phase2ContractPolicy.strictBool(dictionary["raw_access"]) == false,
              Phase2ContractPolicy.strictBool(dictionary["memory_write_actions"]) == false,
              let status = Phase2ContractPolicy.safeCode(dictionary["status"], maximumLength: 32)
        else {
            throw Phase2ContractError.invalid("invalid_owner_action_result")
        }
        self.status = status
        self.actionRef = try Phase2ContractPolicy.optionalPublicRef(
            dictionary["action_ref"],
            prefixes: ["action_"]
        )
        self.confirmationRef = try Phase2ContractPolicy.optionalPublicRef(
            dictionary["confirmation_ref"],
            prefixes: ["confirmation_", "acq_"]
        )
        self.contextRef = try Phase2ContractPolicy.optionalPublicRef(
            dictionary["context_ref"],
            prefixes: ["context_"]
        )
        self.verificationState = try Phase2ContractPolicy.optionalSafeCode(
            dictionary["verification_state"],
            maximumLength: 48
        )
    }
}

public struct ProductFeedbackRequestV1: Equatable, Sendable {
    public enum SourceKind: String, Equatable, Sendable {
        case bundle
        case localHandoff = "local_handoff"
    }

    public let idempotencyKey: String
    public let sourceKind: SourceKind
    public let sourceRef: String
    public let projectRef: String
    public let useful: Bool
    public let resumeSeconds: Double?
    public let experimentRef: String?

    public init(
        idempotencyKey: String,
        sourceKind: SourceKind,
        sourceRef: String,
        projectRef: String,
        useful: Bool,
        resumeSeconds: Double? = nil,
        experimentRef: String? = nil
    ) {
        self.idempotencyKey = idempotencyKey
        self.sourceKind = sourceKind
        self.sourceRef = sourceRef
        self.projectRef = projectRef
        self.useful = useful
        self.resumeSeconds = resumeSeconds
        self.experimentRef = experimentRef
    }

    public func encodedStandardInput() throws -> Data {
        guard let idempotencyKey = Phase2ContractPolicy.publicRef(idempotencyKey, prefixes: ["idem_"]),
              let sourceRef = Phase2ContractPolicy.publicRef(
                  sourceRef,
                  prefixes: sourceKind == .bundle ? ["bundle_"] : ["bundle_", "handoff_"]
              ),
              let projectRef = Phase2ContractPolicy.publicRef(projectRef, prefixes: ["project_"])
        else {
            throw Phase2ContractError.invalid("invalid_feedback_binding")
        }
        var payload: [String: Any] = [
            "contract_version": "ProductFeedbackV1",
            "idempotency_key": idempotencyKey,
            "source_kind": sourceKind.rawValue,
            "source_ref": sourceRef,
            "project_ref": projectRef,
            "useful": useful,
        ]
        if let resumeSeconds {
            guard resumeSeconds.isFinite, resumeSeconds >= 0, resumeSeconds <= 86_400 else {
                throw Phase2ContractError.invalid("invalid_resume_seconds")
            }
            payload["resume_seconds"] = resumeSeconds
        }
        if let experimentRef {
            guard let value = Phase2ContractPolicy.publicRef(experimentRef, prefixes: ["experiment_"]) else {
                throw Phase2ContractError.invalid("invalid_experiment_ref")
            }
            payload["experiment_ref"] = value
        }
        return try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    }
}

public struct ProductFeedbackV1: Equatable, Sendable {
    public let status: String
    public let feedbackRef: String?
    public let sourceKind: ProductFeedbackRequestV1.SourceKind?
    public let sourceRef: String?
    public let useful: Bool?
    public let winRecorded: Bool
    public let experimentRecorded: Bool

    public init(data: Data) throws {
        let dictionary = try Phase2ContractPolicy.dictionary(from: data)
        try Phase2ContractPolicy.validatePublicProjection(dictionary)
        guard dictionary["contract_version"] as? String == "ProductFeedbackV1",
              Phase2ContractPolicy.strictBool(dictionary["raw_access"]) == false,
              let status = Phase2ContractPolicy.safeCode(dictionary["status"], maximumLength: 32)
        else {
            throw Phase2ContractError.invalid("invalid_product_feedback")
        }
        let sourceKind = (dictionary["source_kind"] as? String)
            .flatMap(ProductFeedbackRequestV1.SourceKind.init(rawValue:))
        let sourcePrefixes = sourceKind == .bundle ? ["bundle_"] : ["bundle_", "handoff_"]
        let useful = Phase2ContractPolicy.strictBool(dictionary["useful"])
        let winRecorded = Phase2ContractPolicy.strictBool(dictionary["win_recorded"]) ?? false
        guard useful != false || !winRecorded else {
            throw Phase2ContractError.invalid("negative_feedback_cannot_record_win")
        }
        self.status = status
        self.feedbackRef = try Phase2ContractPolicy.optionalPublicRef(
            dictionary["feedback_ref"],
            prefixes: ["feedback_"]
        )
        self.sourceKind = sourceKind
        self.sourceRef = try Phase2ContractPolicy.optionalPublicRef(
            dictionary["source_ref"],
            prefixes: Set(sourcePrefixes)
        )
        self.useful = useful
        self.winRecorded = winRecorded
        self.experimentRecorded = Phase2ContractPolicy.strictBool(dictionary["experiment_recorded"]) ?? false
    }
}

// MARK: - Content-free product experiment status

public enum ProductExperimentPhase: String, Equatable, Sendable {
    case notStarted = "not_started"
    case collecting
    case incomplete
    case extended
    case passed
    case notPassed = "not_passed"
    case insufficient
    case unknown

    public var isRunning: Bool {
        switch self {
        case .collecting, .incomplete, .extended:
            return true
        case .notStarted, .passed, .notPassed, .insufficient, .unknown:
            return false
        }
    }
}

/// Strict, content-free projection used only to bind Find feedback to the
/// currently running natural-day experiment. Diagnostic reason codes, source
/// bodies and private database identifiers are deliberately not accepted.
public struct ProductExperimentStatusV1: Equatable, Sendable {
    public let readState: Phase2ReadState
    public let observedAt: String
    public let phase: ProductExperimentPhase?
    public let experimentStarted: Bool?
    public let experimentRef: String?
    public let attemptCount: Int?
    public let completeAttemptCount: Int?
    public let usefulRate: Double?
    public let medianResumeSeconds: Double?

    public var runningExperimentRef: String? {
        guard readState == .available,
              experimentStarted == true,
              phase?.isRunning == true
        else { return nil }
        return experimentRef
    }

    public init(data: Data) throws {
        let dictionary = try Phase2ContractPolicy.dictionary(from: data)
        let allowedKeys: Set<String> = [
            "contract_version", "read_state", "observed_at", "status",
            "experiment_started", "experiment_ref", "attempt_count",
            "complete_attempt_count", "useful_rate", "median_resume_seconds",
            "raw_access",
        ]
        guard Set(dictionary.keys).isSubset(of: allowedKeys),
              dictionary["contract_version"] as? String == "ProductExperimentStatusV1",
              Phase2ContractPolicy.strictBool(dictionary["raw_access"]) == false,
              let readStateCode = Phase2ContractPolicy.safeCode(
                  dictionary["read_state"],
                  maximumLength: 32
              ),
              let readState = Phase2ReadState(rawValue: readStateCode),
              let observedAt = Phase2ContractPolicy.utcTimestamp(dictionary["observed_at"])
        else {
            throw Phase2ContractError.invalid("invalid_product_experiment_envelope")
        }
        try Phase2ContractPolicy.validatePublicProjection(dictionary)

        let contentKeys = [
            "status", "experiment_started", "experiment_ref", "attempt_count",
            "complete_attempt_count", "useful_rate", "median_resume_seconds",
        ]
        guard readState == .available else {
            try Phase2ContractPolicy.requireNullOrAbsent(dictionary, keys: contentKeys)
            self.readState = readState
            self.observedAt = observedAt
            self.phase = nil
            self.experimentStarted = nil
            self.experimentRef = nil
            self.attemptCount = nil
            self.completeAttemptCount = nil
            self.usefulRate = nil
            self.medianResumeSeconds = nil
            return
        }

        guard let phaseCode = Phase2ContractPolicy.safeCode(
            dictionary["status"],
            maximumLength: 32
        ),
        let phase = ProductExperimentPhase(rawValue: phaseCode)
        else {
            throw Phase2ContractError.invalid("invalid_product_experiment_status")
        }

        if phase == .unknown {
            try Phase2ContractPolicy.requireNullOrAbsent(
                dictionary,
                keys: [
                    "experiment_started", "experiment_ref", "attempt_count",
                    "complete_attempt_count", "useful_rate", "median_resume_seconds",
                ]
            )
            self.readState = readState
            self.observedAt = observedAt
            self.phase = phase
            self.experimentStarted = nil
            self.experimentRef = nil
            self.attemptCount = nil
            self.completeAttemptCount = nil
            self.usefulRate = nil
            self.medianResumeSeconds = nil
            return
        }

        guard let experimentStarted = Phase2ContractPolicy.strictBool(
            dictionary["experiment_started"]
        ),
        let attemptCount = Phase2ContractPolicy.nonnegativeInt(dictionary["attempt_count"]),
        let completeAttemptCount = Phase2ContractPolicy.nonnegativeInt(
            dictionary["complete_attempt_count"]
        ),
        completeAttemptCount <= attemptCount
        else {
            throw Phase2ContractError.invalid("invalid_product_experiment_counts")
        }
        let experimentRef = try Phase2ContractPolicy.optionalPublicRef(
            dictionary["experiment_ref"],
            prefixes: ["experiment_"]
        )
        let usefulRate = try Phase2ContractPolicy.optionalFiniteDouble(
            dictionary["useful_rate"],
            minimum: 0,
            maximum: 1
        )
        let medianResumeSeconds = try Phase2ContractPolicy.optionalFiniteDouble(
            dictionary["median_resume_seconds"],
            minimum: 0,
            maximum: 86_400
        )
        guard attemptCount > 0 || (usefulRate == nil && medianResumeSeconds == nil) else {
            throw Phase2ContractError.invalid("empty_experiment_has_metrics")
        }
        let hasCompleteMetrics = usefulRate != nil && medianResumeSeconds != nil
        if attemptCount == 0 {
            guard completeAttemptCount == 0,
                  usefulRate == nil,
                  medianResumeSeconds == nil
            else {
                throw Phase2ContractError.invalid("empty_experiment_has_evidence")
            }
        } else if completeAttemptCount == attemptCount {
            guard hasCompleteMetrics else {
                throw Phase2ContractError.invalid("complete_experiment_missing_metrics")
            }
        } else if hasCompleteMetrics {
            throw Phase2ContractError.invalid("partial_experiment_has_complete_metrics")
        }

        switch phase {
        case .notStarted:
            guard !experimentStarted,
                  experimentRef == nil,
                  attemptCount == 0,
                  completeAttemptCount == 0,
                  usefulRate == nil,
                  medianResumeSeconds == nil
            else {
                throw Phase2ContractError.invalid("not_started_experiment_has_evidence")
            }
        case .passed:
            guard experimentStarted,
                  experimentRef == nil,
                  attemptCount >= 10,
                  completeAttemptCount == attemptCount,
                  let usefulRate,
                  let medianResumeSeconds,
                  usefulRate >= 0.80,
                  medianResumeSeconds < 30.0
            else {
                throw Phase2ContractError.invalid("terminal_experiment_missing_evidence")
            }
        case .notPassed:
            guard experimentStarted,
                  experimentRef == nil,
                  attemptCount >= 10,
                  completeAttemptCount == attemptCount,
                  let usefulRate,
                  let medianResumeSeconds,
                  usefulRate < 0.80 || medianResumeSeconds >= 30.0
            else {
                throw Phase2ContractError.invalid("terminal_experiment_missing_evidence")
            }
        case .extended, .insufficient:
            guard experimentStarted,
                  completeAttemptCount == attemptCount,
                  attemptCount < 10,
                  attemptCount == 0 || hasCompleteMetrics
            else {
                throw Phase2ContractError.invalid("insufficient_experiment_evidence_invalid")
            }
        case .incomplete:
            guard experimentStarted,
                  attemptCount > 0,
                  completeAttemptCount < attemptCount
            else {
                throw Phase2ContractError.invalid("incomplete_experiment_evidence_invalid")
            }
        case .collecting:
            guard experimentStarted else {
                throw Phase2ContractError.invalid("collecting_experiment_not_started")
            }
        case .unknown:
            throw Phase2ContractError.invalid("unexpected_unknown_experiment_status")
        }

        if phase.isRunning {
            guard experimentStarted, experimentRef != nil else {
                throw Phase2ContractError.invalid("running_experiment_missing_ref")
            }
        } else if phase != .notStarted {
            guard experimentStarted, experimentRef == nil else {
                throw Phase2ContractError.invalid("terminal_experiment_ref_forbidden")
            }
        }

        self.readState = readState
        self.observedAt = observedAt
        self.phase = phase
        self.experimentStarted = experimentStarted
        self.experimentRef = experimentRef
        self.attemptCount = attemptCount
        self.completeAttemptCount = completeAttemptCount
        self.usefulRate = usefulRate
        self.medianResumeSeconds = medianResumeSeconds
    }
}

public struct Phase2ResumeMeasurement: Equatable, Sendable {
    public let sourceRef: String
    public let projectRef: String
    public let resumeSeconds: Double

    public init(sourceRef: String, projectRef: String, resumeSeconds: Double) {
        self.sourceRef = sourceRef
        self.projectRef = projectRef
        self.resumeSeconds = resumeSeconds
    }
}

public enum Phase2ProductFeedbackChoice: String, Equatable, Sendable {
    case useful
    case notUseful = "not_useful"

    public var useful: Bool { self == .useful }

    public var title: String {
        switch self {
        case .useful: return "有用，继续工作"
        case .notUseful: return "没帮助"
        }
    }
}

/// A content-free capability bound to the exact visible handoff and monotonic
/// Find attempt that created it. SwiftUI button closures capture this value so
/// a queued action from an older preview cannot be reinterpreted as feedback
/// for whichever preview happens to be current when the task executes.
public struct Phase2FeedbackActionAuthorization: Equatable, Sendable {
    public let sourceRef: String
    public let projectRef: String
    public let timerGeneration: UInt64

    public init?(
        sourceRef: String,
        projectRef: String,
        timerGeneration: UInt64
    ) {
        guard timerGeneration > 0,
              let safeSourceRef = Phase2ContractPolicy.publicRef(
                  sourceRef,
                  prefixes: ["bundle_", "handoff_"]
              ),
              let safeProjectRef = Phase2ContractPolicy.publicRef(
                  projectRef,
                  prefixes: ["project_"]
              )
        else { return nil }
        self.sourceRef = safeSourceRef
        self.projectRef = safeProjectRef
        self.timerGeneration = timerGeneration
    }

    public func matches(
        sourceRef: String,
        projectRef: String,
        timerGeneration: UInt64
    ) -> Bool {
        self.sourceRef == sourceRef
            && self.projectRef == projectRef
            && self.timerGeneration == timerGeneration
    }
}

/// Pure action-time authorization reducer. Invalidating it is synchronous and
/// deliberately separate from cancelling an async request: starting another
/// search, switching projects or leaving Find can revoke an old button before
/// that button's queued Task reaches the coordinator.
public struct Phase2FeedbackActionAuthorizationState: Equatable, Sendable {
    public private(set) var current: Phase2FeedbackActionAuthorization?

    public init() {
        current = nil
    }

    @discardableResult
    public mutating func bind(
        sourceRef: String,
        projectRef: String,
        timerGeneration: UInt64
    ) -> Phase2FeedbackActionAuthorization? {
        current = Phase2FeedbackActionAuthorization(
            sourceRef: sourceRef,
            projectRef: projectRef,
            timerGeneration: timerGeneration
        )
        return current
    }

    public mutating func invalidate() {
        current = nil
    }

    public func authorizes(
        _ expected: Phase2FeedbackActionAuthorization,
        currentSourceRef: String,
        currentProjectRef: String,
        currentTimerGeneration: UInt64
    ) -> Bool {
        current == expected
            && expected.matches(
                sourceRef: currentSourceRef,
                projectRef: currentProjectRef,
                timerGeneration: currentTimerGeneration
            )
    }
}

/// A one-shot capability for the exact SwiftUI handoff render that is allowed
/// to freeze a Find measurement. Public refs keep it safe to cross the model/UI
/// boundary; the monotonic generation prevents an older same-ref `onAppear`
/// callback from consuming a replacement handoff's timer.
public struct Phase2HandoffRenderToken: Equatable, Hashable, Sendable {
    public let sourceRef: String
    public let projectRef: String
    public let timerGeneration: UInt64

    public init?(
        sourceRef: String,
        projectRef: String,
        timerGeneration: UInt64
    ) {
        guard timerGeneration > 0,
              let safeSourceRef = Phase2ContractPolicy.publicRef(
                  sourceRef,
                  prefixes: ["bundle_", "handoff_"]
              ),
              let safeProjectRef = Phase2ContractPolicy.publicRef(
                  projectRef,
                  prefixes: ["project_"]
              )
        else { return nil }
        self.sourceRef = safeSourceRef
        self.projectRef = safeProjectRef
        self.timerGeneration = timerGeneration
    }
}

/// Pure render-generation reducer used by the coordinator and tests. Staging a
/// replacement revokes the previous token synchronously. A failed consume does
/// not remove the current token, so a delayed old callback cannot deny the new
/// handoff its own later `onAppear`.
public struct Phase2HandoffRenderCheckpointState: Equatable, Sendable {
    public private(set) var pending: Phase2HandoffRenderToken?

    public init() {
        pending = nil
    }

    @discardableResult
    public mutating func stage(
        sourceRef: String,
        projectRef: String,
        timerGeneration: UInt64
    ) -> Phase2HandoffRenderToken? {
        pending = Phase2HandoffRenderToken(
            sourceRef: sourceRef,
            projectRef: projectRef,
            timerGeneration: timerGeneration
        )
        return pending
    }

    public mutating func consume(
        _ expected: Phase2HandoffRenderToken,
        currentSourceRef: String,
        currentProjectRef: String,
        currentTimerGeneration: UInt64
    ) -> Bool {
        guard pending == expected,
              expected.sourceRef == currentSourceRef,
              expected.projectRef == currentProjectRef,
              expected.timerGeneration == currentTimerGeneration
        else { return false }
        pending = nil
        return true
    }

    public mutating func cancel() {
        pending = nil
    }
}

public struct Phase2PendingProductFeedback: Equatable, Sendable {
    public let request: ProductFeedbackRequestV1
    public let choice: Phase2ProductFeedbackChoice

    public init(
        request: ProductFeedbackRequestV1,
        choice: Phase2ProductFeedbackChoice
    ) {
        self.request = request
        self.choice = choice
    }
}

public enum Phase2FeedbackSubmissionCheckpoint: String, Equatable, Sendable {
    case idle
    case submitting
    case transportUnknown = "transport_unknown"
    case statusRefreshPending = "status_refresh_pending"
}

/// Content-free state for the experiment/feedback binding. The checkpoint is
/// entered before the service call, so presentation navigation can never reopen
/// another source while Core commit status is unresolved. A transport error
/// permits only the exact stored request to be retried. A semantically valid
/// receipt then remains globally closed until a current experiment-status
/// response is applied.
public struct Phase2ExperimentFeedbackBindingState: Equatable, Sendable {
    public private(set) var runningExperimentRef: String?
    public private(set) var recordedSourceRef: String?
    public private(set) var pendingRetry: Phase2PendingProductFeedback?
    public private(set) var submissionCheckpoint: Phase2FeedbackSubmissionCheckpoint
    private var checkpointExperimentRef: String?

    public var statusRefreshPending: Bool {
        submissionCheckpoint == .statusRefreshPending
    }

    public var submissionInFlight: Bool {
        submissionCheckpoint == .submitting
    }

    public init() {
        runningExperimentRef = nil
        recordedSourceRef = nil
        pendingRetry = nil
        submissionCheckpoint = .idle
        checkpointExperimentRef = nil
    }

    @discardableResult
    public mutating func applyExperimentStatus(
        _ status: ProductExperimentStatusV1
    ) -> Bool {
        guard status.readState == .available,
              status.phase != .unknown
        else {
            loseTrustPreservingSubmission()
            return false
        }
        let nextRef = status.runningExperimentRef
        if submissionCheckpoint == .statusRefreshPending {
            if nextRef != checkpointExperimentRef {
                recordedSourceRef = nil
            }
            submissionCheckpoint = .idle
            checkpointExperimentRef = nil
        } else if submissionCheckpoint == .idle,
                  nextRef != runningExperimentRef {
            recordedSourceRef = nil
        }
        runningExperimentRef = nextRef
        if nextRef == nil, submissionCheckpoint == .idle {
            recordedSourceRef = nil
            pendingRetry = nil
        }
        return nextRef != nil
    }

    /// Stage a first submission or return the exact stored request after an
    /// ambiguous transport failure. A different choice or payload is refused
    /// until the original flow is abandoned explicitly.
    public mutating func requestForSubmission(
        _ candidate: ProductFeedbackRequestV1,
        choice: Phase2ProductFeedbackChoice
    ) -> ProductFeedbackRequestV1? {
        let proposed = Phase2PendingProductFeedback(
            request: candidate,
            choice: choice
        )
        switch submissionCheckpoint {
        case .idle:
            guard let experimentRef = candidate.experimentRef,
                  experimentRef == runningExperimentRef,
                  recordedSourceRef != candidate.sourceRef
            else { return nil }
            pendingRetry = proposed
            submissionCheckpoint = .submitting
            checkpointExperimentRef = experimentRef
            return candidate
        case .transportUnknown:
            guard let pendingRetry,
                  pendingRetry.choice == choice,
                  pendingRetry.request.sourceKind == candidate.sourceKind,
                  pendingRetry.request.sourceRef == candidate.sourceRef,
                  pendingRetry.request.projectRef == candidate.projectRef,
                  pendingRetry.request.useful == candidate.useful,
                  pendingRetry.request.experimentRef == candidate.experimentRef
            else { return nil }
            submissionCheckpoint = .submitting
            return pendingRetry.request
        case .submitting, .statusRefreshPending:
            return nil
        }
    }

    /// Resolve the experiment ref to use for a new submission or an exact retry.
    /// A transport-unknown retry deliberately uses the stored experiment even
    /// if a later status projection has already become terminal.
    public func experimentRefForSubmission(sourceRef: String) -> String? {
        if submissionCheckpoint == .transportUnknown,
           pendingRetry?.request.sourceRef == sourceRef {
            return pendingRetry?.request.experimentRef
        }
        return submissionCheckpoint == .idle ? runningExperimentRef : nil
    }

    /// A recorded/replayed receipt is authoritative even if the presentation
    /// token was invalidated while the service call was in flight.
    @discardableResult
    public mutating func markRecordedAwaitingStatusRefresh(
        request: ProductFeedbackRequestV1
    ) -> Bool {
        guard submissionCheckpoint == .submitting,
              pendingRetry?.request == request
        else { return false }
        recordedSourceRef = request.sourceRef
        pendingRetry = nil
        submissionCheckpoint = .statusRefreshPending
        return true
    }

    /// A thrown service boundary is ambiguous: Core may already have committed.
    /// Preserve the exact request and block every other source until replay.
    @discardableResult
    public mutating func markTransportUnknown(
        request: ProductFeedbackRequestV1
    ) -> Bool {
        guard submissionCheckpoint == .submitting,
              pendingRetry?.request == request
        else { return false }
        submissionCheckpoint = .transportUnknown
        return true
    }

    /// Clear trusted experiment state without abandoning an in-flight,
    /// transport-unknown or recorded-awaiting-status checkpoint.
    public mutating func loseTrustPreservingSubmission() {
        runningExperimentRef = nil
        if submissionCheckpoint == .idle {
            recordedSourceRef = nil
            pendingRetry = nil
            checkpointExperimentRef = nil
        }
    }

    public mutating func failClosed() {
        runningExperimentRef = nil
        recordedSourceRef = nil
        pendingRetry = nil
        submissionCheckpoint = .idle
        checkpointExperimentRef = nil
    }

    public mutating func clear() {
        failClosed()
    }

    public func canSubmit(sourceRef: String?) -> Bool {
        switch submissionCheckpoint {
        case .idle:
            return runningExperimentRef != nil
                && sourceRef != nil
                && recordedSourceRef != sourceRef
        case .transportUnknown:
            return sourceRef != nil && pendingRetry?.request.sourceRef == sourceRef
        case .submitting, .statusRefreshPending:
            return false
        }
    }
}

/// Root generation for a multi-request refresh chain. Per-channel tokens stop
/// stale responses within one channel; this token additionally prevents an
/// older refresh from starting later channels after a newer refresh begins.
public struct Phase2RefreshGenerationState: Equatable, Sendable {
    public private(set) var generation: UInt64 = 0

    public init() {}

    @discardableResult
    public mutating func begin() -> UInt64 {
        generation &+= 1
        return generation
    }

    public func isCurrent(_ candidate: UInt64) -> Bool {
        candidate == generation
    }
}

/// Pure generation-bound timer state. Callers provide uptime nanoseconds from
/// a monotonic clock; wall-clock corrections can therefore never change a
/// recorded resume duration.
public struct Phase2ResumeTimerState: Equatable, Sendable {
    public private(set) var generation: UInt64 = 0
    public private(set) var selectedProjectRef: String?
    public private(set) var measurement: Phase2ResumeMeasurement?
    private var startedAtUptimeNanoseconds: UInt64?
    private var findEntryAwaitingFirstSearch = false

    public init() {}

    public var isActive: Bool {
        startedAtUptimeNanoseconds != nil
    }

    @discardableResult
    public mutating func begin(atUptimeNanoseconds now: UInt64) -> UInt64 {
        generation &+= 1
        startedAtUptimeNanoseconds = now
        selectedProjectRef = nil
        measurement = nil
        findEntryAwaitingFirstSearch = false
        return generation
    }

    /// Start at the user-visible entry into Find (Command-K or selecting the
    /// Find destination). This start may be preserved by exactly one search so
    /// the measured duration includes query-entry time.
    @discardableResult
    public mutating func beginFindEntry(atUptimeNanoseconds now: UInt64) -> UInt64 {
        let newGeneration = begin(atUptimeNanoseconds: now)
        findEntryAwaitingFirstSearch = true
        return newGeneration
    }

    /// Bind one search execution to a monotonic generation. The first search
    /// after entering Find preserves the entry timestamp; every later search
    /// cancels the previous attempt and starts at the new execution time.
    @discardableResult
    public mutating func beginSearch(atUptimeNanoseconds now: UInt64) -> UInt64 {
        if findEntryAwaitingFirstSearch, startedAtUptimeNanoseconds != nil {
            findEntryAwaitingFirstSearch = false
            return generation
        }
        return begin(atUptimeNanoseconds: now)
    }

    /// The first project selection belongs to the active Find attempt. A
    /// subsequent project switch invalidates that attempt instead of silently
    /// attributing its elapsed time to a different project.
    @discardableResult
    public mutating func select(projectRef: String) -> Bool {
        guard Phase2ContractPolicy.publicRef(projectRef, prefixes: ["project_"]) != nil,
              startedAtUptimeNanoseconds != nil
        else {
            cancel()
            return false
        }
        guard let selectedProjectRef else {
            self.selectedProjectRef = projectRef
            return true
        }
        guard selectedProjectRef == projectRef else {
            cancel()
            return false
        }
        return true
    }

    @discardableResult
    public mutating func freeze(
        sourceRef: String,
        projectRef: String,
        atUptimeNanoseconds now: UInt64,
        generation expectedGeneration: UInt64
    ) -> Phase2ResumeMeasurement? {
        guard generation == expectedGeneration,
              selectedProjectRef == projectRef,
              let startedAtUptimeNanoseconds,
              now >= startedAtUptimeNanoseconds,
              Phase2ContractPolicy.publicRef(
                  sourceRef,
                  prefixes: ["bundle_", "handoff_"]
              ) != nil
        else { return nil }
        let seconds = Double(now - startedAtUptimeNanoseconds) / 1_000_000_000
        guard seconds.isFinite, seconds <= 86_400 else {
            cancel()
            return nil
        }
        let result = Phase2ResumeMeasurement(
            sourceRef: sourceRef,
            projectRef: projectRef,
            resumeSeconds: seconds
        )
        measurement = result
        self.startedAtUptimeNanoseconds = nil
        return result
    }

    public mutating func cancel() {
        generation &+= 1
        startedAtUptimeNanoseconds = nil
        selectedProjectRef = nil
        measurement = nil
        findEntryAwaitingFirstSearch = false
    }
}

/// Binds visible Find results to the monotonic timer generation that produced
/// them. Beginning or cancelling a search immediately makes every prior card
/// non-selectable, even while an older SwiftUI button action is still queued.
public struct Phase2SearchResultSelectionState: Equatable, Sendable {
    public private(set) var acceptedTimerGeneration: UInt64?
    public private(set) var projectRefs: Set<String>

    public init() {
        acceptedTimerGeneration = nil
        projectRefs = []
    }

    public mutating func beginSearch() {
        acceptedTimerGeneration = nil
        projectRefs.removeAll()
    }

    public mutating func accept(
        timerGeneration: UInt64,
        projectRefs incomingProjectRefs: Set<String>
    ) {
        acceptedTimerGeneration = timerGeneration
        projectRefs = Set(incomingProjectRefs.compactMap {
            Phase2ContractPolicy.publicRef($0, prefixes: ["project_"])
        })
    }

    public mutating func cancel() {
        beginSearch()
    }

    public func canSelect(timerGeneration: UInt64, projectRef: String) -> Bool {
        acceptedTimerGeneration == timerGeneration
            && projectRefs.contains(projectRef)
    }
}

// MARK: - Service boundary and request model

public struct ContextSnapshotQuery: Equatable, Sendable {
    public let projectRef: String?
    public let startAt: String?
    public let endAt: String?
    public let query: String?
    public let includeClues: Bool
    public let limit: Int

    public init(
        projectRef: String? = nil,
        startAt: String? = nil,
        endAt: String? = nil,
        query: String? = nil,
        includeClues: Bool = true,
        limit: Int = 100
    ) throws {
        if projectRef != nil,
           Phase2ContractPolicy.publicRef(projectRef, prefixes: ["project_"]) == nil {
            throw Phase2ContractError.invalid("invalid_project_ref")
        }
        if startAt != nil, Phase2ContractPolicy.utcTimestamp(startAt) == nil {
            throw Phase2ContractError.invalid("invalid_start_at")
        }
        if endAt != nil, Phase2ContractPolicy.utcTimestamp(endAt) == nil {
            throw Phase2ContractError.invalid("invalid_end_at")
        }
        if query != nil,
           Phase2ContractPolicy.safeUserText(query, maximumLength: 80) == nil {
            throw Phase2ContractError.invalid("invalid_search_query")
        }
        guard (1...200).contains(limit) else {
            throw Phase2ContractError.invalid("invalid_limit")
        }
        self.projectRef = projectRef
        self.startAt = startAt
        self.endAt = endAt
        self.query = query
        self.includeClues = includeClues
        self.limit = limit
    }

    public func encodedStandardInput() throws -> Data {
        var payload: [String: Any] = [
            "include_clues": includeClues,
            "limit": limit,
        ]
        if let projectRef { payload["project_ref"] = projectRef }
        if let startAt { payload["start_at"] = startAt }
        if let endAt { payload["end_at"] = endAt }
        if let query { payload["query"] = query }
        return try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    }
}

public enum ContextConsoleCoreCommands {
    public static let readTimeoutSeconds: TimeInterval = 3
    public static let writeTimeoutSeconds: TimeInterval = 5

    public static func snapshot(_ query: ContextSnapshotQuery) throws -> CoreCommandRequest {
        CoreCommandRequest(
            arguments: ["context-console", "snapshot", "--payload-stdin"],
            standardInput: try query.encodedStandardInput(),
            timeoutSeconds: readTimeoutSeconds
        )
    }

    public static func memoryCandidates() -> CoreCommandRequest {
        CoreCommandRequest(
            arguments: ["context-console", "memory-candidates", "--payload-stdin"],
            standardInput: Data("{}".utf8),
            timeoutSeconds: readTimeoutSeconds
        )
    }

    public static func deliveryStatus() -> CoreCommandRequest {
        CoreCommandRequest(
            arguments: ["context-console", "delivery-status", "--payload-stdin"],
            standardInput: Data("{}".utf8),
            timeoutSeconds: readTimeoutSeconds
        )
    }

    public static func productExperimentStatus() -> CoreCommandRequest {
        CoreCommandRequest(
            arguments: ["context-console", "experiment-status"],
            standardInput: nil,
            timeoutSeconds: readTimeoutSeconds
        )
    }

    public static func localHandoff(projectRef: String) throws -> CoreCommandRequest {
        guard let ref = Phase2ContractPolicy.publicRef(projectRef, prefixes: ["project_"]) else {
            throw Phase2ContractError.invalid("invalid_project_ref")
        }
        let input = try JSONSerialization.data(
            withJSONObject: ["project_ref": ref],
            options: [.sortedKeys]
        )
        return CoreCommandRequest(
            arguments: ["context-console", "local-handoff", "--payload-stdin"],
            standardInput: input,
            timeoutSeconds: readTimeoutSeconds
        )
    }

    public static func productFeedback(
        _ request: ProductFeedbackRequestV1
    ) throws -> CoreCommandRequest {
        CoreCommandRequest(
            arguments: ["context-console", "product-feedback", "--payload-stdin"],
            standardInput: try request.encodedStandardInput(),
            timeoutSeconds: writeTimeoutSeconds
        )
    }

    public static func ownerAction(
        _ action: OwnerContextActionV1
    ) throws -> CoreCommandRequest {
        CoreCommandRequest(
            arguments: ["context-console", "owner-action", "--payload-stdin"],
            standardInput: try action.encodedStandardInput(),
            timeoutSeconds: writeTimeoutSeconds
        )
    }
}

public protocol ContextConsoleServicing: Sendable {
    func loadSnapshot(query: ContextSnapshotQuery) async throws -> ContextSnapshotV1
    func loadMemoryCandidates() async throws -> MemoryCandidateListV1
    func loadDeliveryStatus() async throws -> DeliveryStatusV1
    func loadProductExperimentStatus() async throws -> ProductExperimentStatusV1
    func buildLocalHandoff(projectRef: String) async throws -> LocalHandoffV1
    func submitProductFeedback(_ request: ProductFeedbackRequestV1) async throws -> ProductFeedbackV1
    func submitOwnerAction(_ action: OwnerContextActionV1) async throws -> OwnerContextActionResultV1
}

// MARK: - Stale-response-safe view state

public enum Phase2RequestChannel: String, CaseIterable, Hashable, Sendable {
    case today
    case search
    case memory
    case delivery
    case experiment
    case handoff
    case ownerAction
    case feedback
}

public struct Phase2RequestToken: Equatable, Hashable, Sendable {
    public let channel: Phase2RequestChannel
    public let generation: UInt64

    fileprivate init(channel: Phase2RequestChannel, generation: UInt64) {
        self.channel = channel
        self.generation = generation
    }
}

public struct Phase2ConsoleViewState: Equatable, Sendable {
    public private(set) var readState: Phase2ReadState
    public private(set) var observedAt: String
    public private(set) var actionsEnabled: Bool
    public private(set) var snapshot: ContextSnapshotV1?
    public private(set) var searchSnapshot: ContextSnapshotV1?
    public private(set) var memoryCandidates: MemoryCandidateListV1?
    public private(set) var deliveryStatus: DeliveryStatusV1?
    public private(set) var productExperimentStatus: ProductExperimentStatusV1?
    public private(set) var localHandoff: LocalHandoffV1?

    private var nextGeneration: UInt64
    private var activeGenerations: [Phase2RequestChannel: UInt64]

    public init() {
        readState = .unknown
        observedAt = ""
        actionsEnabled = false
        snapshot = nil
        searchSnapshot = nil
        memoryCandidates = nil
        deliveryStatus = nil
        productExperimentStatus = nil
        localHandoff = nil
        nextGeneration = 0
        activeGenerations = [:]
    }

    public mutating func beginRequest(for channel: Phase2RequestChannel) -> Phase2RequestToken {
        nextGeneration &+= 1
        activeGenerations[channel] = nextGeneration
        return Phase2RequestToken(channel: channel, generation: nextGeneration)
    }

    public mutating func invalidateAllRequests() {
        nextGeneration &+= 1
        activeGenerations.removeAll()
    }

    public mutating func invalidateRequests(_ channels: Set<Phase2RequestChannel>) {
        nextGeneration &+= 1
        for channel in channels {
            activeGenerations[channel] = nil
        }
    }

    /// A trusted runtime status may degrade independently of an in-flight
    /// Context command (for example the Core's periodic status read becomes
    /// busy or unavailable). Treat that transition as globally authoritative:
    /// clear every surface and invalidate every response that started before
    /// it. Recovery always requires a fresh Context refresh.
    @discardableResult
    public mutating func applyRuntimeTrustLoss(
        _ readState: Phase2ReadState,
        observedAt: String
    ) -> Bool {
        guard readState != .available else { return false }
        clearTrustedContent(readState: readState, observedAt: observedAt)
        return true
    }

    @discardableResult
    public mutating func cancel(_ token: Phase2RequestToken) -> Bool {
        guard tokenIsCurrent(token) else { return false }
        activeGenerations[token.channel] = nil
        return true
    }

    @discardableResult
    public mutating func apply(
        snapshot incoming: ContextSnapshotV1,
        for channel: Phase2RequestChannel,
        token: Phase2RequestToken
    ) -> Bool {
        guard token.channel == channel, tokenIsCurrent(token) else { return false }
        activeGenerations[channel] = nil
        guard incoming.readState == .available else {
            clearTrustedContent(
                readState: incoming.readState,
                observedAt: incoming.observedAt
            )
            return true
        }
        readState = .available
        observedAt = incoming.observedAt
        actionsEnabled = incoming.actionsEnabled
        if channel == .search {
            searchSnapshot = incoming
        } else {
            snapshot = incoming
        }
        return true
    }

    @discardableResult
    public mutating func apply(
        memoryCandidates incoming: MemoryCandidateListV1,
        token: Phase2RequestToken
    ) -> Bool {
        guard token.channel == .memory, tokenIsCurrent(token) else { return false }
        activeGenerations[.memory] = nil
        guard incoming.readState == .available else {
            clearTrustedContent(readState: incoming.readState, observedAt: incoming.observedAt)
            return true
        }
        readState = .available
        observedAt = incoming.observedAt
        memoryCandidates = incoming
        return true
    }

    @discardableResult
    public mutating func apply(
        deliveryStatus incoming: DeliveryStatusV1,
        token: Phase2RequestToken
    ) -> Bool {
        guard token.channel == .delivery, tokenIsCurrent(token) else { return false }
        activeGenerations[.delivery] = nil
        guard incoming.readState == .available else {
            clearTrustedContent(readState: incoming.readState, observedAt: incoming.observedAt)
            return true
        }
        readState = .available
        observedAt = incoming.observedAt
        deliveryStatus = incoming
        return true
    }

    @discardableResult
    public mutating func apply(
        productExperimentStatus incoming: ProductExperimentStatusV1,
        token: Phase2RequestToken
    ) -> Bool {
        guard token.channel == .experiment, tokenIsCurrent(token) else { return false }
        activeGenerations[.experiment] = nil
        guard incoming.readState == .available,
              incoming.phase != .unknown
        else {
            clearTrustedContent(
                readState: incoming.readState == .available ? .unknown : incoming.readState,
                observedAt: incoming.observedAt
            )
            return true
        }
        readState = .available
        observedAt = incoming.observedAt
        productExperimentStatus = incoming
        return true
    }

    @discardableResult
    public mutating func apply(
        localHandoff incoming: LocalHandoffV1,
        token: Phase2RequestToken
    ) -> Bool {
        guard token.channel == .handoff, tokenIsCurrent(token) else { return false }
        activeGenerations[.handoff] = nil
        if incoming.readState == .blocked {
            // A semantic handoff rejection (for example, a compatibility-only
            // project or contexts expiring after search) is local to the
            // preview. The already-reviewed Today/Search/Memory/Delivery
            // snapshots remain valid.
            localHandoff = nil
            return true
        }
        guard incoming.readState == .available else {
            clearTrustedContent(readState: incoming.readState, observedAt: incoming.observedAt)
            return true
        }
        readState = .available
        observedAt = incoming.observedAt
        localHandoff = incoming
        return true
    }

    @discardableResult
    public mutating func applyFailure(
        _ readState: Phase2ReadState,
        observedAt: String,
        token: Phase2RequestToken
    ) -> Bool {
        guard tokenIsCurrent(token), readState != .available else { return false }
        activeGenerations[token.channel] = nil
        clearTrustedContent(readState: readState, observedAt: observedAt)
        return true
    }

    private func tokenIsCurrent(_ token: Phase2RequestToken) -> Bool {
        activeGenerations[token.channel] == token.generation
    }

    private mutating func clearTrustedContent(
        readState: Phase2ReadState,
        observedAt: String
    ) {
        // A trust-degrading transition is global. No response that began
        // before it, even on another surface, may repopulate trusted content.
        activeGenerations.removeAll()
        self.readState = readState
        self.observedAt = observedAt
        actionsEnabled = false
        snapshot = nil
        searchSnapshot = nil
        memoryCandidates = nil
        deliveryStatus = nil
        productExperimentStatus = nil
        localHandoff = nil
    }
}

// MARK: - Private validation primitives

private struct Phase2ReadEnvelope {
    let readState: Phase2ReadState
    let observedAt: String
    let lastSuccessfulReadAt: String?
    let actionsEnabled: Bool

    init(dictionary: [String: Any], contractVersion: String) throws {
        guard dictionary["contract_version"] as? String == contractVersion,
              Phase2ContractPolicy.strictBool(dictionary["raw_access"]) == false,
              let status = dictionary["status"] as? String,
              let observedAt = Phase2ContractPolicy.utcTimestamp(dictionary["observed_at"])
        else {
            throw Phase2ContractError.invalid("invalid_read_envelope")
        }
        let lastSuccessful = try Phase2ContractPolicy.optionalUTCTimestamp(
            dictionary["last_successful_read_at"]
        )
        let normalized: Phase2ReadState
        switch status {
        case "available": normalized = .available
        case "disabled": normalized = .disabled
        case "rejected": normalized = .blocked
        case "blocked": normalized = .blocked
        case "busy": normalized = .busy
        case "unknown": normalized = .unknown
        case "unavailable": normalized = .unavailable
        default: throw Phase2ContractError.invalid("unknown_read_status")
        }
        let actions: Bool
        if normalized == .available {
            if dictionary["actions_enabled"] == nil || dictionary["actions_enabled"] is NSNull {
                actions = false
            } else if let supplied = Phase2ContractPolicy.strictBool(dictionary["actions_enabled"]) {
                actions = supplied
            } else {
                throw Phase2ContractError.invalid("actions_enabled_invalid")
            }
        } else {
            if dictionary["actions_enabled"] != nil,
               !(dictionary["actions_enabled"] is NSNull),
               Phase2ContractPolicy.strictBool(dictionary["actions_enabled"]) != false {
                throw Phase2ContractError.invalid("unreadable_actions_must_be_false")
            }
            actions = false
        }
        self.readState = normalized
        self.observedAt = observedAt
        self.lastSuccessfulReadAt = lastSuccessful
        self.actionsEnabled = actions
    }
}

private enum Phase2ContractPolicy {
    static let maximumContractBytes = 512 * 1_024
    static let forbiddenKeys: Set<String> = [
        "verified_ref", "candidate_ref", "observation_ref", "evidence_ref",
        "receipt_ref", "provider_receipt_ref", "consumer_receipt_ref",
        "provenance_ref", "rowid", "absolute_path", "url", "raw_json",
        "diagnostic_json", "reason_code", "body", "frame", "deletion_group", "prompt",
        "chat_body", "tool_arguments", "diff", "cookie", "token", "password",
    ]
    static let internalReferencePrefixes = [
        "verified_", "candidate_", "observation_", "evidence_",
        "receipt_", "withdrawal_",
    ]

    static func dictionary(from data: Data) throws -> [String: Any] {
        guard !data.isEmpty, data.count <= maximumContractBytes else {
            throw Phase2ContractError.invalid("contract_size_invalid")
        }
        let object: Any
        do {
            object = try JSONSerialization.jsonObject(with: data)
        } catch {
            throw Phase2ContractError.invalid("invalid_json")
        }
        guard let dictionary = object as? [String: Any] else {
            throw Phase2ContractError.invalid("top_level_object_required")
        }
        return dictionary
    }

    static func validatePublicProjection(_ value: Any, depth: Int = 0) throws {
        guard depth <= 8 else {
            throw Phase2ContractError.invalid("projection_too_deep")
        }
        if let dictionary = value as? [String: Any] {
            guard dictionary.count <= 128 else {
                throw Phase2ContractError.invalid("projection_too_wide")
            }
            for (key, child) in dictionary {
                let normalized = key.lowercased()
                guard !forbiddenKeys.contains(normalized),
                      !normalized.hasSuffix("_rowid"),
                      normalized == "idempotency_key" || !normalized.hasSuffix("_id")
                else {
                    throw Phase2ContractError.invalid("internal_field_forbidden")
                }
                if normalized == "raw_access",
                   strictBool(child) != false {
                    throw Phase2ContractError.invalid("raw_access_must_be_false")
                }
                try validatePublicProjection(child, depth: depth + 1)
            }
            return
        }
        if let array = value as? [Any] {
            guard array.count <= 500 else {
                throw Phase2ContractError.invalid("projection_array_too_large")
            }
            for child in array {
                try validatePublicProjection(child, depth: depth + 1)
            }
            return
        }
        if let string = value as? String {
            guard safeScalarString(string, maximumLength: 4_096),
                  !containsPathOrURL(string),
                  !containsSecretLikeValue(string),
                  !looksLikeJSONDocument(string)
            else {
                throw Phase2ContractError.invalid("unsafe_projection_string")
            }
            if internalReferencePrefixes.contains(where: { prefix in
                string.hasPrefix(prefix)
                    && string.dropFirst(prefix.count).count >= 8
                    && string.dropFirst(prefix.count).allSatisfy(\.isHexDigit)
            }) {
                throw Phase2ContractError.invalid("internal_reference_forbidden")
            }
            return
        }
        if value is NSNumber || value is NSNull {
            return
        }
        throw Phase2ContractError.invalid("unsupported_projection_value")
    }

    static func requireNullOrAbsent(_ dictionary: [String: Any], keys: [String]) throws {
        for key in keys {
            if let value = dictionary[key], !(value is NSNull) {
                throw Phase2ContractError.invalid("unreadable_content_must_be_null")
            }
        }
    }

    static func strictBool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID()
        else {
            return nil
        }
        return number.boolValue
    }

    static func nonnegativeInt(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              let integer = value as? Int,
              integer >= 0
        else {
            return nil
        }
        return integer
    }

    static func optionalFiniteDouble(
        _ value: Any?,
        minimum: Double,
        maximum: Double
    ) throws -> Double? {
        guard value != nil, !(value is NSNull) else { return nil }
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID()
        else {
            throw Phase2ContractError.invalid("invalid_finite_double")
        }
        let result = number.doubleValue
        guard result.isFinite, result >= minimum, result <= maximum else {
            throw Phase2ContractError.invalid("invalid_finite_double")
        }
        return result
    }

    static func positiveInt(_ value: Any?) -> Int? {
        guard let value = nonnegativeInt(value), value > 0 else { return nil }
        return value
    }

    static func publicRef(_ value: Any?, prefixes: Set<String>) -> String? {
        guard let string = value as? String, string.count <= 96 else { return nil }
        for prefix in prefixes where string.hasPrefix(prefix) {
            let suffix = string.dropFirst(prefix.count)
            guard (8...64).contains(suffix.count),
                  suffix.allSatisfy(\.isHexDigit),
                  suffix.allSatisfy({ !$0.isUppercase })
            else {
                return nil
            }
            return string
        }
        return nil
    }

    static func optionalPublicRef(_ value: Any?, prefixes: Set<String>) throws -> String? {
        guard value != nil, !(value is NSNull) else { return nil }
        guard let ref = publicRef(value, prefixes: prefixes) else {
            throw Phase2ContractError.invalid("invalid_public_ref")
        }
        return ref
    }

    static func safeCode(_ value: Any?, maximumLength: Int) -> String? {
        guard let value = value as? String,
              !value.isEmpty,
              value.count <= maximumLength,
              value.unicodeScalars.allSatisfy({
                  CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
                      .contains($0)
              })
        else {
            return nil
        }
        return value
    }

    static func optionalSafeCode(_ value: Any?, maximumLength: Int) throws -> String? {
        guard value != nil, !(value is NSNull) else { return nil }
        guard let result = safeCode(value, maximumLength: maximumLength) else {
            throw Phase2ContractError.invalid("invalid_safe_code")
        }
        return result
    }

    static func safeLabel(_ value: Any?) -> String? {
        safeUserText(value, maximumLength: 180)
    }

    static func safeUserText(_ value: Any?, maximumLength: Int) -> String? {
        guard let value = value as? String else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              trimmed.count <= maximumLength,
              safeScalarString(trimmed, maximumLength: maximumLength),
              !containsPathOrURL(trimmed),
              !containsSecretLikeValue(trimmed),
              !looksLikeJSONDocument(trimmed)
        else {
            return nil
        }
        return trimmed
    }

    static func safeStringArray(
        _ value: Any?,
        maximumItems: Int,
        maximumLength: Int
    ) throws -> [String] {
        guard let values = value as? [String], values.count <= maximumItems else {
            throw Phase2ContractError.invalid("invalid_safe_string_array")
        }
        return try values.map {
            guard let safe = safeUserText($0, maximumLength: maximumLength) else {
                throw Phase2ContractError.invalid("invalid_safe_string_array")
            }
            return safe
        }
    }

    static func sha256(_ value: Any?) -> String? {
        guard let value = value as? String, value.hasPrefix("sha256:") else { return nil }
        let digest = value.dropFirst("sha256:".count)
        guard digest.count == 64,
              digest.allSatisfy(\.isHexDigit),
              digest.allSatisfy({ !$0.isUppercase })
        else {
            return nil
        }
        return value
    }

    static func utcTimestamp(_ value: Any?) -> String? {
        guard let value = value as? String,
              value.hasSuffix("Z") || value.hasSuffix("+00:00")
        else {
            return nil
        }
        let formatter = ISO8601DateFormatter()
        if formatter.date(from: value) != nil { return value }
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value) == nil ? nil : value
    }

    static func optionalUTCTimestamp(_ value: Any?) throws -> String? {
        guard value != nil, !(value is NSNull) else { return nil }
        guard let timestamp = utcTimestamp(value) else {
            throw Phase2ContractError.invalid("invalid_utc_timestamp")
        }
        return timestamp
    }

    static func safeScalarString(_ value: String, maximumLength: Int) -> Bool {
        guard value.count <= maximumLength else { return false }
        return value.unicodeScalars.allSatisfy {
            switch $0.properties.generalCategory {
            case .control, .format:
                return false
            default:
                return true
            }
        }
    }

    static func containsPathOrURL(_ value: String) -> Bool {
        let lowered = value.lowercased()
        if lowered.contains("http://") || lowered.contains("https://") || lowered.contains("file://")
            || lowered.contains("~/") || lowered.contains("\\\\") {
            return true
        }
        if value.hasPrefix("/") || value.range(of: #"\s/[^\s]+"#, options: .regularExpression) != nil {
            return true
        }
        return value.range(of: #"\b[A-Za-z]:[\\/]"#, options: .regularExpression) != nil
    }

    static func containsSecretLikeValue(_ value: String) -> Bool {
        value.range(
            of: #"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{12,}|\b(token|secret|password|cookie)\s*[:=])"#,
            options: .regularExpression
        ) != nil
    }

    static func looksLikeJSONDocument(_ value: String) -> Bool {
        let candidate = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard candidate.first == "{" || candidate.first == "[",
              let data = candidate.data(using: .utf8),
              let decoded = try? JSONSerialization.jsonObject(with: data)
        else {
            return false
        }
        return decoded is [String: Any] || decoded is [Any]
    }
}
