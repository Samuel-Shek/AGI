import Foundation
import CoreFoundation

public enum AppTab: Hashable, CaseIterable, Identifiable {
    case today
    case reflection
    case evolution
    case confirmations
    case fileHistory
    case review
    case memoryReview
    case systemStatus
    case privacy
    case settings

    public var id: Self { self }

    public var title: String {
        switch self {
        case .today: return "今天"
        case .reflection: return "思考与反思"
        case .evolution: return "自主进化"
        case .confirmations: return "需要你决定"
        case .fileHistory: return "记录"
        case .review: return "回顾"
        case .memoryReview: return "记忆库"
        case .systemStatus: return "系统状态"
        case .privacy: return "隐私与数据"
        case .settings: return "设置"
        }
    }

    public var systemImage: String {
        switch self {
        case .today: return "sun.max"
        case .reflection: return "lightbulb"
        case .evolution: return "arrow.triangle.2.circlepath"
        case .confirmations: return "questionmark.bubble"
        case .fileHistory: return "clock.arrow.circlepath"
        case .review: return "doc.text.magnifyingglass"
        case .memoryReview: return "brain"
        case .systemStatus: return "stethoscope"
        case .privacy: return "hand.raised"
        case .settings: return "gearshape"
        }
    }

    public var navigationGroup: AppNavigationGroup {
        switch self {
        case .today, .reflection, .evolution, .confirmations, .fileHistory, .review, .memoryReview:
            return .daily
        case .systemStatus, .privacy, .settings:
            return .management
        }
    }

    public static var dailyTabs: [AppTab] {
        [.today, .reflection, .confirmations, .fileHistory, .memoryReview, .evolution]
    }

    public static var managementTabs: [AppTab] {
        [.systemStatus, .settings]
    }
}

public enum AppNavigationGroup: String, CaseIterable, Identifiable {
    case daily
    case management

    public var id: Self { self }

    public var title: String {
        switch self {
        case .daily: return "日常"
        case .management: return "管理"
        }
    }
}

public enum ActivityConfirmationBadgePresentation {
    public static func visualText(pendingCount: Int) -> String? {
        let count = max(0, pendingCount)
        guard count > 0 else { return nil }
        return count > 99 ? "99+" : "\(count)"
    }

    public static func accessibilityLabel(tabTitle: String, pendingCount: Int) -> String {
        let count = max(0, pendingCount)
        guard count > 0 else { return tabTitle }
        return "\(tabTitle)，\(count) 条待确认"
    }
}

public enum ActivityConfirmationPendingCountState: Equatable {
    case verified(Int)
    case unverified

    public var value: Int? {
        guard case let .verified(value) = self else { return nil }
        return max(0, value)
    }
}

public enum ActivityConfirmationContentState: Equatable {
    case readable
    case blocked
}

public enum ActivityConfirmationBadgeTone: Equatable {
    case none
    case standard
    case warning
}

public struct ActivityConfirmationNavigationState: Equatable {
    public let pendingCountState: ActivityConfirmationPendingCountState
    public let contentState: ActivityConfirmationContentState

    public init(
        pendingCountState: ActivityConfirmationPendingCountState,
        contentState: ActivityConfirmationContentState
    ) {
        switch pendingCountState {
        case let .verified(value):
            self.pendingCountState = .verified(max(0, value))
        case .unverified:
            self.pendingCountState = .unverified
        }
        self.contentState = contentState
    }

    public static func compatible(
        pendingCount: Int,
        contentIsReadable: Bool
    ) -> Self {
        Self(
            pendingCountState: .verified(pendingCount),
            contentState: contentIsReadable ? .readable : .blocked
        )
    }

    public static func compatibleResponse(
        status: String,
        pendingCount: Int
    ) -> Self {
        guard status == "ok", pendingCount >= 0 else { return .unavailable }
        return .compatible(pendingCount: pendingCount, contentIsReadable: true)
    }

    public static let unavailable = Self(
        pendingCountState: .unverified,
        contentState: .blocked
    )

    public var verifiedPendingCount: Int? { pendingCountState.value }

    public var canDisplayContent: Bool { contentState == .readable }

    public var badgeText: String? {
        switch pendingCountState {
        case let .verified(value):
            return ActivityConfirmationBadgePresentation.visualText(pendingCount: value)
        case .unverified:
            return "!"
        }
    }

    public var badgeTone: ActivityConfirmationBadgeTone {
        guard badgeText != nil else { return .none }
        return contentState == .readable && pendingCountState.value != nil ? .standard : .warning
    }

    public var needsAttention: Bool { badgeText != nil }

    public func pageHeaderText(pendingSourceCount: Int) -> String {
        switch pendingCountState {
        case let .verified(value):
            let count = max(0, value)
            if contentState == .blocked {
                return count == 0
                    ? "待确认内容暂不可读"
                    : "有 \(count) 个问题，但内容暂不可读"
            }
            return count == 0
                ? "没有需要你处理的问题"
                : "\(count) 个问题 · \(max(0, pendingSourceCount)) 条安全线索"
        case .unverified:
            return "待确认数量暂不可验证"
        }
    }

    public var todayPromptTitle: String {
        switch pendingCountState {
        case let .verified(value):
            let count = max(0, value)
            if contentState == .blocked {
                return "有 \(count) 个问题，但内容暂不可读"
            }
            return "有 \(count) 条活动暂时无法确定"
        case .unverified:
            return "待确认数量暂不可验证"
        }
    }

    public var todayMetricDetail: String {
        switch pendingCountState {
        case let .verified(value):
            let count = max(0, value)
            guard count > 0 else { return "只统计已确认事实" }
            return contentState == .blocked
                ? "另有 \(count) 条待确认，但内容暂不可读"
                : "另有 \(count) 条待确认"
        case .unverified:
            return "待确认数量暂不可验证"
        }
    }

    public var confirmedFactsDetail: String {
        switch pendingCountState {
        case let .verified(value):
            let count = max(0, value)
            guard count > 0 else {
                return "只有直接证据确认或你亲自确认的活动，才会显示在这里。"
            }
            return contentState == .blocked
                ? "有 \(count) 条事项仍在待确认，但内容暂不可读；确认前不会显示为今天的事实。"
                : "有 \(count) 条事项仍在待确认；确认前不会显示为今天的事实。"
        case .unverified:
            return "待确认数量暂不可验证；未确认内容不会显示为今天的事实。"
        }
    }

    public func accessibilityLabel(tabTitle: String) -> String {
        switch pendingCountState {
        case let .verified(value):
            let count = max(0, value)
            guard count > 0 else { return tabTitle }
            let suffix = contentState == .blocked ? "，内容暂不可读" : ""
            return "\(tabTitle)，\(count) 条待确认\(suffix)"
        case .unverified:
            return "\(tabTitle)，待确认数量暂不可验证"
        }
    }
}

public enum ActivityConfirmationAnswer: String, CaseIterable {
    case occurred
    case notOccurred = "not_occurred"
    case ignored
    case deferred = "defer"

    public var label: String {
        switch self {
        case .occurred: return "发生了"
        case .notOccurred: return "没有发生"
        case .ignored: return "忽略该问题"
        case .deferred: return "暂时跳过"
        }
    }
}

public enum ActivityConfirmationPriority: String, CaseIterable, Equatable {
    case high
    case normal
    case low

    public var label: String {
        switch self {
        case .high: return "高优先"
        case .normal: return "普通"
        case .low: return "低优先"
        }
    }
}

public enum ActivityConfirmationQuestionKind: String, CaseIterable, Equatable {
    case occurrence
    case businessContext = "business_context"
    case outcome

    public var label: String {
        switch self {
        case .occurrence: return "是否发生"
        case .businessContext: return "业务归因"
        case .outcome: return "业务结果"
        }
    }
}

public enum ActivityConfirmationCandidateOrigin: String, CaseIterable, Equatable {
    case explicitClaim = "explicit_claim"
    case businessEpisode = "business_episode"
    case manual
}

public enum ActivityConfirmationBusinessOutcome: String, CaseIterable, Identifiable, Equatable {
    case completed
    case progressing
    case blocked
    case preparationOnly = "preparation_only"
    case unknown

    public var id: Self { self }

    public var label: String {
        switch self {
        case .completed: return "已完成"
        case .progressing: return "推进中"
        case .blocked: return "受阻"
        case .preparationOnly: return "仅准备"
        case .unknown: return "结果未知"
        }
    }
}

public enum ActivityConfirmationModePreset: String, CaseIterable, Identifiable {
    case compact
    case standard
    case maxRecall = "max_recall"

    public var id: Self { self }

    public var title: String {
        switch self {
        case .compact: return "精简 5–10"
        case .standard: return "标准 10–20"
        case .maxRecall: return "最大召回"
        }
    }

    public var detail: String {
        switch self {
        case .compact: return "只保留最需要你确认的高价值事件，目标每天约 5–10 条。"
        case .standard: return "兼顾准确率和召回，目标每天约 10–20 条。"
        case .maxRecall: return "尽量保留可能影响复盘的候选，待确认数量可能明显增加。"
        }
    }
}

public struct ActivityConfirmationPriorityCounts: Equatable {
    public let high: Int
    public let normal: Int
    public let low: Int

    public init(high: Int, normal: Int, low: Int) {
        self.high = max(0, high)
        self.normal = max(0, normal)
        self.low = max(0, low)
    }

    public init?(dictionary: [String: Any]) {
        guard let high = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["high"]),
              let normal = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["normal"]),
              let low = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["low"])
        else { return nil }
        self.init(high: high, normal: normal, low: low)
    }

    public var total: Int { high + normal + low }
}

public struct ActivityConfirmationPendingKinds: Equatable {
    public let occurrence: Int
    public let businessContext: Int
    public let outcome: Int

    public init(occurrence: Int, businessContext: Int, outcome: Int) {
        self.occurrence = max(0, occurrence)
        self.businessContext = max(0, businessContext)
        self.outcome = max(0, outcome)
    }

    public init?(dictionary: [String: Any]) {
        guard let occurrence = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["occurrence"]),
              let businessContext = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["business_context"]),
              let outcome = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["outcome"])
        else { return nil }
        self.init(occurrence: occurrence, businessContext: businessContext, outcome: outcome)
    }

    public var total: Int { occurrence + businessContext + outcome }
}

public struct ActivityConfirmationCandidateFunnel: Equatable {
    public let clearActivities: Int
    public let eventSignals: Int
    public let eligibleQuestions: Int
    public let hardFiltered: Int
    public let pending: Int
    public let confirmed: Int
    public let reportable: Int
    public let filterReasonCounts: [String: Int]

    public init(
        clearActivities: Int,
        eventSignals: Int,
        eligibleQuestions: Int,
        hardFiltered: Int,
        pending: Int,
        confirmed: Int,
        reportable: Int,
        filterReasonCounts: [String: Int]
    ) {
        self.clearActivities = max(0, clearActivities)
        self.eventSignals = max(0, eventSignals)
        self.eligibleQuestions = max(0, eligibleQuestions)
        self.hardFiltered = max(0, hardFiltered)
        self.pending = max(0, pending)
        self.confirmed = max(0, confirmed)
        self.reportable = max(0, reportable)
        self.filterReasonCounts = filterReasonCounts
    }

    public init?(dictionary: [String: Any]) {
        guard let clearActivities = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["clear_activities"]),
              let eventSignals = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["event_signals"]),
              let eligibleQuestions = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["eligible_questions"]),
              let hardFiltered = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["hard_filtered"]),
              let pending = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["pending"]),
              let confirmed = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["confirmed"]),
              let reportable = ActivityConfirmationV4Contract.nonnegativeInt(dictionary["reportable"]),
              let rawReasons = dictionary["filter_reason_counts"] as? [String: Any]
        else { return nil }
        var reasons: [String: Int] = [:]
        for (rawKey, rawValue) in rawReasons {
            guard let key = ActivityConfirmationV4Contract.safeToken(rawKey, maxLength: 80),
                  let value = ActivityConfirmationV4Contract.nonnegativeInt(rawValue)
            else { return nil }
            reasons[key] = value
        }
        self.init(
            clearActivities: clearActivities,
            eventSignals: eventSignals,
            eligibleQuestions: eligibleQuestions,
            hardFiltered: hardFiltered,
            pending: pending,
            confirmed: confirmed,
            reportable: reportable,
            filterReasonCounts: reasons
        )
    }
}

public struct ActivityConfirmationV4Summary: Equatable {
    public let priorityCounts: ActivityConfirmationPriorityCounts
    public let pendingByKind: ActivityConfirmationPendingKinds
    public let candidateFunnel: ActivityConfirmationCandidateFunnel

    public init(
        priorityCounts: ActivityConfirmationPriorityCounts,
        pendingByKind: ActivityConfirmationPendingKinds,
        candidateFunnel: ActivityConfirmationCandidateFunnel
    ) {
        self.priorityCounts = priorityCounts
        self.pendingByKind = pendingByKind
        self.candidateFunnel = candidateFunnel
    }

    public init?(dictionary: [String: Any]) {
        guard let priorityDictionary = dictionary["priority_counts"] as? [String: Any],
              let pendingDictionary = dictionary["pending_by_kind"] as? [String: Any],
              let funnelDictionary = dictionary["candidate_funnel"] as? [String: Any],
              let priorityCounts = ActivityConfirmationPriorityCounts(dictionary: priorityDictionary),
              let pendingByKind = ActivityConfirmationPendingKinds(dictionary: pendingDictionary),
              let candidateFunnel = ActivityConfirmationCandidateFunnel(dictionary: funnelDictionary)
        else { return nil }
        self.init(
            priorityCounts: priorityCounts,
            pendingByKind: pendingByKind,
            candidateFunnel: candidateFunnel
        )
    }

    public func countsMatch(pendingQuestionCount: Int) -> Bool {
        priorityCounts.total == pendingQuestionCount
            && pendingByKind.total == pendingQuestionCount
            && candidateFunnel.pending == pendingQuestionCount
    }
}

public enum ActivityConfirmationSafetyBoundary {
    public static func permits(
        rawAccess: Any?,
        destructiveActions: Any?,
        memoryWriteActions: Any?
    ) -> Bool {
        ActivityConfirmationV4Contract.bool(rawAccess) == false
            && ActivityConfirmationV4Contract.bool(destructiveActions) == false
            && ActivityConfirmationV4Contract.bool(memoryWriteActions) == false
    }

    public static func nonnegativeInteger(_ value: Any?) -> Int? {
        ActivityConfirmationV4Contract.nonnegativeInt(value)
    }
}

public struct ActivityConfirmationGroupV4Fields: Equatable {
    public let questionKind: ActivityConfirmationQuestionKind
    public let candidateOrigin: ActivityConfirmationCandidateOrigin
    public let priority: ActivityConfirmationPriority
    public let episodeID: String
    public let safeSignalTypes: [String]
    public let suggestedWorkstream: String
    public let suggestedOutcome: ActivityConfirmationBusinessOutcome?
    public let userWorkstream: String
    public let userOutcome: ActivityConfirmationBusinessOutcome?
    public let notificationEligible: Bool

    public init?(dictionary: [String: Any]) {
        guard let questionKindValue = dictionary["question_kind"] as? String,
              let questionKind = ActivityConfirmationQuestionKind(rawValue: questionKindValue),
              let originValue = dictionary["candidate_origin"] as? String,
              let candidateOrigin = ActivityConfirmationCandidateOrigin(rawValue: originValue),
              let priorityValue = dictionary["priority"] as? String,
              let priority = ActivityConfirmationPriority(rawValue: priorityValue),
              let rawSignalTypes = dictionary["safe_signal_types"] as? [String],
              rawSignalTypes.count <= 24,
              let notificationEligible = ActivityConfirmationV4Contract.bool(dictionary["notification_eligible"])
        else { return nil }
        guard dictionary["episode_id"] == nil || dictionary["episode_id"] is String else { return nil }
        let episodeValue = dictionary["episode_id"] as? String ?? ""
        let episodeID: String
        if episodeValue.isEmpty {
            episodeID = ""
        } else if let safeEpisodeID = ActivityConfirmationV4Contract.safeToken(episodeValue, maxLength: 128) {
            episodeID = safeEpisodeID
        } else {
            return nil
        }
        let signalTypes = rawSignalTypes.compactMap {
            ActivityConfirmationV4Contract.safeToken($0, maxLength: 80)
        }
        let suggestedOutcomeValue = dictionary["suggested_outcome"] as? String ?? ""
        let userOutcomeValue = dictionary["user_outcome"] as? String ?? ""
        let parsedSuggestedOutcome = suggestedOutcomeValue.isEmpty
            ? nil
            : ActivityConfirmationBusinessOutcome(rawValue: suggestedOutcomeValue)
        let parsedUserOutcome = userOutcomeValue.isEmpty
            ? nil
            : ActivityConfirmationBusinessOutcome(rawValue: userOutcomeValue)
        guard signalTypes.count == rawSignalTypes.count,
              (candidateOrigin != .businessEpisode || !episodeID.isEmpty),
              suggestedOutcomeValue.isEmpty || parsedSuggestedOutcome != nil,
              userOutcomeValue.isEmpty || parsedUserOutcome != nil
        else { return nil }
        self.questionKind = questionKind
        self.candidateOrigin = candidateOrigin
        self.priority = priority
        self.episodeID = episodeID
        self.safeSignalTypes = signalTypes
        suggestedWorkstream = ActivityConfirmationV4Contract.boundedText(
            dictionary["suggested_workstream"] as? String ?? "",
            maxLength: 160
        )
        suggestedOutcome = parsedSuggestedOutcome
        userWorkstream = ActivityConfirmationV4Contract.boundedText(
            dictionary["user_workstream"] as? String ?? "",
            maxLength: 160
        )
        userOutcome = parsedUserOutcome
        self.notificationEligible = notificationEligible
    }
}

public enum ActivityConfirmationV4ListContract {
    private static let requiredAnswerOptions: Set<String> = [
        ActivityConfirmationAnswer.occurred.rawValue,
        ActivityConfirmationAnswer.notOccurred.rawValue,
        ActivityConfirmationAnswer.ignored.rawValue,
        ActivityConfirmationAnswer.deferred.rawValue,
    ]

    public static func answerOptionsAreSafe(_ value: Any?) -> Bool {
        guard let options = value as? [String],
              options.count == requiredAnswerOptions.count,
              Set(options) == requiredAnswerOptions
        else { return false }
        return true
    }

    public static func isSafeVisiblePendingGroup(_ dictionary: [String: Any]) -> Bool {
        guard let groupKey = dictionary["group_key"] as? String,
              ActivityConfirmationV4Contract.safeToken(groupKey, maxLength: 160) != nil,
              let memberRevision = dictionary["member_revision"] as? String,
              ActivityConfirmationV4Contract.safeToken(memberRevision, maxLength: 160) != nil,
              let state = dictionary["state"] as? String,
              ["pending", "deferred"].contains(state),
              answerOptionsAreSafe(dictionary["answer_options"]),
              ActivityConfirmationGroupV4Fields(dictionary: dictionary) != nil
        else { return false }
        return true
    }

    public static func responseIsSafe(
        pendingQuestionCount: Int,
        questionGroups: [[String: Any]]
    ) -> Bool {
        guard pendingQuestionCount >= 0,
              questionGroups.allSatisfy(isSafeVisiblePendingGroup)
        else { return false }
        if pendingQuestionCount == 0 {
            return questionGroups.isEmpty
        }
        // A list can be limited, so one safe actionable card is sufficient;
        // the visible card count does not need to equal the total pending count.
        return !questionGroups.isEmpty
    }
}

public enum ActivityConfirmationV4PayloadDisposition: Equatable {
    case notV4
    case readable
    case blocked
}

public struct ActivityConfirmationV4PayloadState: Equatable {
    public let disposition: ActivityConfirmationV4PayloadDisposition
    public let navigationState: ActivityConfirmationNavigationState
    public let pendingSourceCount: Int
    public let resolvedQuestionCount: Int
    public let summary: ActivityConfirmationV4Summary?
    public let failureReason: String

    public init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let dictionary = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            self.init(
                disposition: .blocked,
                navigationState: .unavailable,
                pendingSourceCount: 0,
                resolvedQuestionCount: 0,
                summary: nil,
                failureReason: "activity-confirmation JSON 无法解析。"
            )
            return
        }
        self.init(dictionary: dictionary)
    }

    public init(dictionary: [String: Any]) {
        guard ActivityConfirmationV4Contract.nonnegativeInt(dictionary["contract_version"]) == 4 else {
            self.init(
                disposition: .notV4,
                navigationState: .unavailable,
                pendingSourceCount: 0,
                resolvedQuestionCount: 0,
                summary: nil,
                failureReason: ""
            )
            return
        }

        let envelopeIsSafe = dictionary["status"] as? String == "ok"
            && dictionary["interface"] as? String == "activity-confirmation"
            && ActivityConfirmationSafetyBoundary.permits(
                rawAccess: dictionary["raw_access"],
                destructiveActions: dictionary["destructive_actions"],
                memoryWriteActions: dictionary["memory_write_actions"]
            )
        let parsedPendingCount = ActivityConfirmationV4Contract.nonnegativeInt(
            dictionary["pending_question_count"]
        )
        let parsedSummary = ActivityConfirmationV4Summary(dictionary: dictionary)
        let countIsVerified = envelopeIsSafe
            && parsedPendingCount != nil
            && parsedSummary?.countsMatch(pendingQuestionCount: parsedPendingCount ?? -1) == true
        let pendingCountState: ActivityConfirmationPendingCountState = countIsVerified
            ? .verified(parsedPendingCount ?? 0)
            : .unverified

        let parsedSourceCount = ActivityConfirmationV4Contract.nonnegativeInt(
            dictionary["pending_source_count"]
        )
        let parsedResolvedCount = ActivityConfirmationV4Contract.nonnegativeInt(
            dictionary["resolved_question_count"]
        )
        let groupObjects = dictionary["question_groups"] as? [[String: Any]]
        let cardsAreSafe = groupObjects.map {
            ActivityConfirmationV4ListContract.responseIsSafe(
                pendingQuestionCount: parsedPendingCount ?? -1,
                questionGroups: $0
            )
        } == true
        let contentIsReadable = countIsVerified
            && parsedSourceCount != nil
            && parsedResolvedCount != nil
            && cardsAreSafe

        self.init(
            disposition: contentIsReadable ? .readable : .blocked,
            navigationState: ActivityConfirmationNavigationState(
                pendingCountState: pendingCountState,
                contentState: contentIsReadable ? .readable : .blocked
            ),
            pendingSourceCount: parsedSourceCount ?? 0,
            resolvedQuestionCount: parsedResolvedCount ?? 0,
            summary: countIsVerified ? parsedSummary : nil,
            failureReason: contentIsReadable
                ? ""
                : countIsVerified
                    ? "activity-confirmation v4 的问题内容未满足安全契约；可信计数已保留，但卡片和操作已关闭。"
                    : "activity-confirmation v4 的安全边界或计数无法验证；卡片和操作已关闭。"
        )
    }

    private init(
        disposition: ActivityConfirmationV4PayloadDisposition,
        navigationState: ActivityConfirmationNavigationState,
        pendingSourceCount: Int,
        resolvedQuestionCount: Int,
        summary: ActivityConfirmationV4Summary?,
        failureReason: String
    ) {
        self.disposition = disposition
        self.navigationState = navigationState
        self.pendingSourceCount = max(0, pendingSourceCount)
        self.resolvedQuestionCount = max(0, resolvedQuestionCount)
        self.summary = summary
        self.failureReason = failureReason
    }
}

public enum ActivityConfirmationGroupAnswerValidationResult: Equatable {
    case recorded
    case stale
    case unsafe
}

public enum ActivityConfirmationMutationResponseValidator {
    public static func configureResponseIsSafe(
        _ dictionary: [String: Any],
        expectedMode: ActivityConfirmationModePreset
    ) -> Bool {
        v4EnvelopeIsSafe(dictionary)
            && dictionary["status"] as? String == "configured"
            && dictionary["mode"] as? String == expectedMode.rawValue
    }

    public static func manualAddResponseIsSafe(
        _ dictionary: [String: Any],
        expectedWorkstream: String,
        expectedOutcome: String
    ) -> Bool {
        let workstream = ActivityConfirmationCommandBuilder.boundedWorkstream(expectedWorkstream)
        let outcome = ActivityConfirmationCommandBuilder.boundedOutcome(expectedOutcome)
        guard v4EnvelopeIsSafe(dictionary),
              dictionary["status"] as? String == "recorded",
              !workstream.isEmpty,
              !outcome.isEmpty,
              let item = dictionary["item"] as? [String: Any],
              stableIdentifier(item["item_key"]) != nil,
              stableIdentifier(item["group_key"]) != nil,
              item["candidate_origin"] as? String == ActivityConfirmationCandidateOrigin.manual.rawValue,
              item["state"] as? String == "confirmed",
              item["user_answer"] as? String == ActivityConfirmationAnswer.occurred.rawValue,
              let eventType = item["event_type"] as? String,
              eventType == (
                  outcome == ActivityConfirmationBusinessOutcome.preparationOnly.rawValue
                      ? "preparation"
                      : "manual"
              ),
              let fields = ActivityConfirmationGroupV4Fields(dictionary: item),
              fields.userWorkstream == workstream,
              fields.userOutcome?.rawValue == outcome
        else { return false }
        return true
    }

    public static func v4GroupAnswerResult(
        _ dictionary: [String: Any],
        expectedGroupKey: String,
        expectedAnswer: ActivityConfirmationAnswer,
        expectedWorkstream: String,
        expectedOutcome: String
    ) -> ActivityConfirmationGroupAnswerValidationResult {
        guard ActivityConfirmationSafetyBoundary.permits(
            rawAccess: dictionary["raw_access"],
            destructiveActions: dictionary["destructive_actions"],
            memoryWriteActions: dictionary["memory_write_actions"]
        ) else { return .unsafe }
        if dictionary["status"] as? String == "stale" {
            return .stale
        }

        let workstream = ActivityConfirmationCommandBuilder.boundedWorkstream(expectedWorkstream)
        let outcome = ActivityConfirmationCommandBuilder.boundedOutcome(expectedOutcome)
        guard dictionary["status"] as? String == "recorded",
              let expectedKey = ActivityConfirmationV4Contract.safeToken(expectedGroupKey, maxLength: 160),
              dictionary["group_key"] as? String == expectedKey,
              let topRevision = stableIdentifier(dictionary["member_revision"]),
              let group = dictionary["question_group"] as? [String: Any],
              group["group_key"] as? String == expectedKey,
              stableIdentifier(group["member_revision"]) == topRevision,
              group["state"] as? String == resolvedState(for: expectedAnswer),
              group["user_answer"] as? String == expectedAnswer.rawValue,
              ActivityConfirmationV4ListContract.answerOptionsAreSafe(group["answer_options"]),
              let fields = ActivityConfirmationGroupV4Fields(dictionary: group)
        else { return .unsafe }

        if expectedAnswer == .occurred {
            guard !workstream.isEmpty,
                  !outcome.isEmpty,
                  outcome != ActivityConfirmationBusinessOutcome.preparationOnly.rawValue,
                  fields.userWorkstream == workstream,
                  fields.userOutcome?.rawValue == outcome
            else { return .unsafe }
        } else if expectedAnswer == .notOccurred {
            guard !workstream.isEmpty,
                  [
                    ActivityConfirmationBusinessOutcome.preparationOnly.rawValue,
                    ActivityConfirmationBusinessOutcome.unknown.rawValue,
                  ].contains(outcome),
                  fields.userWorkstream == workstream,
                  fields.userOutcome?.rawValue == outcome
            else { return .unsafe }
        } else {
            guard fields.userWorkstream.isEmpty, fields.userOutcome == nil else { return .unsafe }
        }
        return .recorded
    }

    private static func v4EnvelopeIsSafe(_ dictionary: [String: Any]) -> Bool {
        dictionary["interface"] as? String == "activity-confirmation"
            && ActivityConfirmationSafetyBoundary.nonnegativeInteger(dictionary["contract_version"]) == 4
            && ActivityConfirmationSafetyBoundary.permits(
                rawAccess: dictionary["raw_access"],
                destructiveActions: dictionary["destructive_actions"],
                memoryWriteActions: dictionary["memory_write_actions"]
            )
    }

    private static func stableIdentifier(_ value: Any?) -> String? {
        guard let value = value as? String else { return nil }
        return ActivityConfirmationV4Contract.safeToken(value, maxLength: 160)
    }

    private static func resolvedState(for answer: ActivityConfirmationAnswer) -> String {
        switch answer {
        case .occurred: return "confirmed"
        case .notOccurred: return "rejected"
        case .ignored: return "ignored"
        case .deferred: return "deferred"
        }
    }
}

public enum ActivityConfirmationReason: String, CaseIterable, Identifiable {
    case oldContent = "old_content"
    case referenceOnly = "reference_only"
    case preparationOnly = "preparation_only"
    case futureNotOccurred = "future_not_occurred"
    case sidebarOrList = "sidebar_or_list"
    case ocrRecognition = "vlm_or_ocr"
    case screenRegionOrApp = "screen_region_or_app"
    case other

    public var id: Self { self }

    public var label: String {
        switch self {
        case .oldContent: return "以前发生，不是这一次"
        case .referenceOnly: return "只是参考资料"
        case .preparationOnly: return "只是准备或讨论"
        case .futureNotOccurred: return "未来安排，尚未发生"
        case .sidebarOrList: return "侧栏或列表内容误导"
        case .ocrRecognition: return "关键文字识别错"
        case .screenRegionOrApp: return "App 或截图区域归属错"
        case .other: return "其他"
        }
    }

    public static func options(
        for answer: ActivityConfirmationAnswer,
        legacyCompatible: Bool = false
    ) -> [Self] {
        let options: [Self]
        switch answer {
        case .notOccurred:
            options = [.oldContent, .preparationOnly, .futureNotOccurred, .referenceOnly, .other]
        case .occurred, .ignored, .deferred:
            options = []
        }
        return legacyCompatible ? options.filter { $0 != .futureNotOccurred } : options
    }
}

public struct ActivityConfirmationAnswerRequest: Equatable {
    public let answer: ActivityConfirmationAnswer
    public let reason: String
    public let generalize: String

    public init(answer: ActivityConfirmationAnswer, selectedReason: ActivityConfirmationReason?) {
        self.answer = answer
        switch answer {
        case .occurred:
            reason = "correct"
            generalize = "exact"
        case .notOccurred:
            reason = selectedReason?.rawValue ?? ""
            generalize = "similar"
        case .ignored:
            reason = "user_ignored"
            generalize = "exact"
        case .deferred:
            reason = "other"
            generalize = "exact"
        }
    }

    public var isValid: Bool {
        switch answer {
        case .notOccurred:
            return !reason.isEmpty
        case .occurred, .ignored, .deferred:
            return true
        }
    }
}

public enum ActivityConfirmationContractMode: Equatable {
    case grouped
    case legacyItems
    case invalidGroupedContract

    /// Presence of `question_groups` is authoritative, including an empty array.
    /// This prevents a grouped response from accidentally rendering its compatibility
    /// `items` alongside the canonical grouped questions.
    public static func resolve(contractVersion: Int, hasQuestionGroups: Bool) -> Self {
        if hasQuestionGroups { return .grouped }
        return contractVersion >= 2 ? .invalidGroupedContract : .legacyItems
    }
}

public enum ActivityConfirmationAnswerAvailability {
    public static func supports(
        _ answer: ActivityConfirmationAnswer,
        answerOptions: [String],
        isLegacy: Bool
    ) -> Bool {
        if answer == .ignored {
            return !isLegacy && answerOptions.contains(answer.rawValue)
        }
        return answerOptions.isEmpty || answerOptions.contains(answer.rawValue)
    }
}

public enum ActivityConfirmationStructuredDetailsPolicy {
    public static func requiresBusinessDetails(contractVersion: Int) -> Bool {
        contractVersion >= 4
    }
}

public enum ActivityConfirmationV5BusinessDefaults {
    public static func workstream(object: String, label: String) -> String {
        let preferred = object.trimmingCharacters(in: .whitespacesAndNewlines)
        let fallback = label.trimmingCharacters(in: .whitespacesAndNewlines)
        return ActivityConfirmationCommandBuilder.boundedWorkstream(
            preferred.isEmpty ? fallback : preferred
        )
    }

    public static func outcome(eventType: String) -> String {
        switch eventType {
        case "completion", "application", "submission", "send", "publish", "purchase":
            return ActivityConfirmationBusinessOutcome.completed.rawValue
        case "interview", "meeting", "call":
            return ActivityConfirmationBusinessOutcome.progressing.rawValue
        default:
            return ActivityConfirmationBusinessOutcome.unknown.rawValue
        }
    }
}

public enum ActivityConfirmationClarificationSubmissionPolicy {
    public static func isEnabled(
        isLoading: Bool,
        isClarifying: Bool,
        canClarify: Bool
    ) -> Bool {
        !isLoading && !isClarifying && canClarify
    }

    public static func actionLabel(isClarifying: Bool) -> String {
        isClarifying ? "正在重新梳理…" : "确认并重新梳理"
    }
}

public enum ActivityConfirmationSubmissionTarget: Equatable {
    case questionGroup(groupKey: String, memberRevision: String)
    case legacyItem(itemKey: String)
}

public struct ActivityConfirmationCoreCommand: Equatable, Sendable {
    public let arguments: [String]
    public let standardInput: String?

    public init(arguments: [String], standardInput: String? = nil) {
        self.arguments = arguments
        self.standardInput = standardInput
    }
}

public enum ActivityConfirmationCommandBuilder {
    public static func boundedNote(_ note: String) -> String {
        String(note.trimmingCharacters(in: .whitespacesAndNewlines).prefix(360))
    }

    public static func boundedAnswerNote(_ note: String) -> String {
        String(note.trimmingCharacters(in: .whitespacesAndNewlines).prefix(240))
    }

    public static func boundedWorkstream(_ value: String) -> String {
        ActivityConfirmationV4Contract.boundedText(value, maxLength: 120)
    }

    public static func boundedOutcome(_ value: String) -> String {
        let trimmed = ActivityConfirmationV4Contract.boundedText(value, maxLength: 40)
        return ActivityConfirmationBusinessOutcome(rawValue: trimmed)?.rawValue ?? ""
    }

    public static func answerCommand(
        target: ActivityConfirmationSubmissionTarget,
        request: ActivityConfirmationAnswerRequest,
        note: String = "",
        workstream: String = "",
        outcome: String = "",
        structuredDetailsSupported: Bool = false
    ) -> ActivityConfirmationCoreCommand? {
        guard request.isValid else { return nil }
        var arguments: [String]
        let isGrouped: Bool
        switch target {
        case let .questionGroup(groupKey, memberRevision):
            guard !groupKey.isEmpty, !memberRevision.isEmpty else { return nil }
            isGrouped = true
            arguments = [
                "activity-confirmation", "answer-group",
                "--group-key", groupKey,
                "--member-revision", memberRevision,
            ]
        case let .legacyItem(itemKey):
            guard !itemKey.isEmpty,
                  request.answer != .ignored,
                  request.reason != ActivityConfirmationReason.futureNotOccurred.rawValue
            else { return nil }
            isGrouped = false
            arguments = [
                "activity-confirmation", "answer",
                "--item-key", itemKey,
            ]
        }
        arguments += [
            "--answer", request.answer.rawValue,
            "--generalize", request.generalize,
        ]
        if !request.reason.isEmpty {
            arguments += ["--reason", request.reason]
        }
        let safeNote = request.answer == .ignored ? "" : boundedAnswerNote(note)
        let includesBusinessDetails = request.answer == .occurred || request.answer == .notOccurred
        let safeWorkstream = includesBusinessDetails ? boundedWorkstream(workstream) : ""
        let safeOutcome = includesBusinessDetails ? boundedOutcome(outcome) : ""
        if isGrouped && structuredDetailsSupported {
            if includesBusinessDetails {
                guard !safeWorkstream.isEmpty, !safeOutcome.isEmpty else { return nil }
            }
            if request.answer == .occurred,
               safeOutcome == ActivityConfirmationBusinessOutcome.preparationOnly.rawValue {
                return nil
            }
            if request.answer == .notOccurred {
                let preparationReason = request.reason == ActivityConfirmationReason.preparationOnly.rawValue
                guard [
                    ActivityConfirmationBusinessOutcome.preparationOnly.rawValue,
                    ActivityConfirmationBusinessOutcome.unknown.rawValue,
                ].contains(safeOutcome),
                preparationReason == (
                    safeOutcome == ActivityConfirmationBusinessOutcome.preparationOnly.rawValue
                ) else { return nil }
            }
            let payload: [String: String] = [
                "note": safeNote,
                "workstream": safeWorkstream,
                "outcome": safeOutcome,
            ]
            guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
                  data.count <= 4096,
                  let json = String(data: data, encoding: .utf8)
            else { return nil }
            arguments += ["--details-stdin"]
            return ActivityConfirmationCoreCommand(arguments: arguments, standardInput: json)
        }
        if !safeNote.isEmpty {
            arguments += ["--note-stdin"]
        }
        return ActivityConfirmationCoreCommand(
            arguments: arguments,
            standardInput: safeNote.isEmpty ? nil : safeNote
        )
    }

    public static func clarifyCommand(
        groupKey: String,
        memberRevision: String,
        note: String = ""
    ) -> ActivityConfirmationCoreCommand? {
        guard !groupKey.isEmpty, !memberRevision.isEmpty else { return nil }
        var arguments = [
            "activity-confirmation", "clarify",
            "--group-key", groupKey,
            "--member-revision", memberRevision,
        ]
        let safeNote = boundedNote(note)
        if !safeNote.isEmpty {
            arguments += ["--note-stdin"]
        }
        return ActivityConfirmationCoreCommand(
            arguments: arguments,
            standardInput: safeNote.isEmpty ? nil : safeNote
        )
    }

    public static func manualAddCommand(
        safeLabel: String,
        workstream: String,
        outcome: String,
        startedAt: String,
        endedAt: String
    ) -> ActivityConfirmationCoreCommand? {
        let label = ActivityConfirmationV4Contract.boundedText(safeLabel, maxLength: 180)
        let safeWorkstream = boundedWorkstream(workstream)
        let safeOutcome = boundedOutcome(outcome)
        let start = ActivityConfirmationV4Contract.iso8601Timestamp(startedAt)
        let end = ActivityConfirmationV4Contract.iso8601Timestamp(endedAt)
        guard !label.isEmpty,
              !safeWorkstream.isEmpty,
              !safeOutcome.isEmpty,
              let startDate = ActivityConfirmationV4Contract.iso8601Date(start),
              let endDate = ActivityConfirmationV4Contract.iso8601Date(end),
              endDate >= startDate,
              endDate.timeIntervalSince(startDate) <= 86_400
        else { return nil }
        let payload: [String: String] = [
            "safe_label": label,
            "workstream": safeWorkstream,
            "outcome": safeOutcome,
            "started_at": start,
            "ended_at": end,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
              data.count <= 4096,
              let json = String(data: data, encoding: .utf8)
        else { return nil }
        return ActivityConfirmationCoreCommand(
            arguments: [
                "activity-confirmation", "manual-add",
                "--confirm", "MANUAL_CONFIRMED_ACTIVITY",
                "--payload-stdin",
            ],
            standardInput: json
        )
    }

    public static func configureCommand(mode: ActivityConfirmationModePreset) -> ActivityConfirmationCoreCommand {
        ActivityConfirmationCoreCommand(
            arguments: ["activity-confirmation", "configure", "--mode", mode.rawValue]
        )
    }
}

public enum ActivityConfirmationNotificationPolicy {
    public static let title = "AGI"
    public static let highValueBody = "有一项高优先活动需要你在本机确认。"
    public static let batchBody = "今天还有活动需要你在本机确认。"

    public static func isHighValueClosedEpisode(
        state: String,
        priority: ActivityConfirmationPriority,
        notificationEligible: Bool,
        episodeID: String,
        observedEnd: String,
        now: Date = Date(),
        quietPeriod: TimeInterval = 5 * 60
    ) -> Bool {
        guard ActivityConfirmationVisibilityPolicy.isVisibleQuestion(state: state),
              priority == .high,
              notificationEligible,
              ActivityConfirmationV4Contract.safeToken(episodeID, maxLength: 128) != nil,
              let endedAt = ActivityConfirmationV4Contract.iso8601Date(observedEnd)
        else { return false }
        return now >= endedAt.addingTimeInterval(max(0, quietPeriod))
    }

    public static func shouldScheduleBatch(pendingQuestionCount: Int) -> Bool {
        pendingQuestionCount > 0
    }
}

public enum ActivityConfirmationFilterReasonPresentation {
    public static func label(for reason: String) -> String {
        switch reason {
        case "task_list": return "任务列表"
        case "context_only": return "仅上下文"
        case "ai_or_tool_status": return "AI / 工具状态"
        case "pam_self_ui": return "AGI 自身界面"
        case "conditional_or_negated": return "条件式或否定表述"
        case "game_ui": return "游戏界面"
        case "privacy_blocked": return "隐私保护"
        case "insufficient_subject": return "主体不明确"
        default: return reason.replacingOccurrences(of: "_", with: " ")
        }
    }
}

private enum ActivityConfirmationV4Contract {
    static func bool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID()
        else { return nil }
        return number.boolValue
    }

    static func nonnegativeInt(_ value: Any?) -> Int? {
        guard let boxed = value as? NSNumber,
              CFGetTypeID(boxed) != CFBooleanGetTypeID(),
              let number = value as? Int,
              number >= 0
        else { return nil }
        return number
    }

    static func safeToken(_ value: String, maxLength: Int) -> String? {
        let token = String(value.trimmingCharacters(in: .whitespacesAndNewlines).prefix(maxLength))
        guard !token.isEmpty else { return nil }
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
        guard token.unicodeScalars.allSatisfy(allowed.contains) else { return nil }
        return token
    }

    static func boundedText(_ value: String, maxLength: Int) -> String {
        String(value.trimmingCharacters(in: .whitespacesAndNewlines).prefix(maxLength))
    }

    static func iso8601Date(_ value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }

    static func iso8601Timestamp(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let date = iso8601Date(trimmed) else { return "" }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }
}

public enum ActivityConfirmationMessagePolicy {
    public static func isWarning(_ message: String) -> Bool {
        let warningTerms = [
            "失败", "异常", "无法解析", "不满足", "未完成", "未提交", "发生了变化", "已经变化",
            "不一致", "缺少关键信息", "没有足够信息",
            "stale", "exited with", "Traceback", "Could not run", "Error:",
        ]
        return warningTerms.contains { message.localizedCaseInsensitiveContains($0) }
    }
}

public enum ActivityConfirmationVisibilityPolicy {
    public static func isVisibleQuestion(state: String) -> Bool {
        state == "pending" || state == "deferred"
    }

    public static func sortPriority(state: String) -> Int {
        switch state {
        case "pending": return 0
        case "deferred": return 1
        default: return 2
        }
    }

    public static func statusLabel(state: String) -> String {
        state == "deferred" ? "已暂时跳过" : ""
    }
}

public enum ActivityConfirmationFieldLabel {
    public static func displayName(for field: String) -> String {
        switch field {
        case "company": return "公司"
        case "round": return "轮次"
        default: return "其他信息"
        }
    }
}

public enum ActivityConfirmationEvidenceStatus: String, Equatable {
    case available
    case partial
    case unavailable

    public static func normalized(_ value: String) -> Self {
        Self(rawValue: value) ?? .unavailable
    }
}

public struct ActivityConfirmationEvidenceSummary: Equatable {
    public let status: ActivityConfirmationEvidenceStatus
    public let evidenceCount: Int
    public let availableSourceCount: Int

    public init(status: String, evidenceCount: Int, availableSourceCount: Int) {
        let safeEvidenceCount = max(0, evidenceCount)
        let safeAvailableSourceCount = max(0, availableSourceCount)
        let normalizedStatus = ActivityConfirmationEvidenceStatus.normalized(status)
        if normalizedStatus == .available && (safeEvidenceCount == 0 || safeAvailableSourceCount == 0) {
            self.status = .unavailable
        } else if normalizedStatus == .partial && (safeEvidenceCount == 0 || safeAvailableSourceCount == 0) {
            self.status = .unavailable
        } else {
            self.status = normalizedStatus
        }
        self.evidenceCount = safeEvidenceCount
        self.availableSourceCount = safeAvailableSourceCount
    }

    public var canReinspectRetainedFrames: Bool {
        status == .available || status == .partial
    }

    public var statusLabel: String {
        switch status {
        case .available:
            return evidenceCount == 1 ? "原截图已保留（1 张）" : "原截图已保留（\(evidenceCount) 张）"
        case .partial:
            return "部分来源可用（覆盖 \(availableSourceCount) 条线索）"
        case .unavailable:
            return "原截图不可用"
        }
    }
}

public enum ActivityConfirmationClarificationOutcome: String, Equatable {
    case rewritten
    case split
    case unchanged
    case insufficientInformation = "insufficient_information"
    case systemFiltered = "system_filtered"
    case evidenceUnavailable = "evidence_unavailable"
}

public enum ActivityConfirmationClarificationSeverity: String, Equatable {
    case success
    case neutral
    case warning
}

public enum ActivityConfirmationClarificationInput: String, CaseIterable, Equatable {
    case retainedFrameOCR = "retained_frame_ocr"
    case storedCleanedText = "stored_cleaned_text"
    case nearbySafeContext = "nearby_safe_context"
    case userNote = "user_note"

    public var label: String {
        switch self {
        case .retainedFrameOCR: return "暂存截图 OCR"
        case .storedCleanedText: return "已清洗识别内容"
        case .nearbySafeContext: return "附近安全上下文"
        case .userNote: return "本次补充说明"
        }
    }
}

public enum ActivityConfirmationClarificationPolicy {
    public static func normalizedOutcome(_ outcome: String) -> ActivityConfirmationClarificationOutcome? {
        ActivityConfirmationClarificationOutcome(rawValue: outcome)
    }

    public static func normalizedSeverity(_ severity: String) -> ActivityConfirmationClarificationSeverity? {
        ActivityConfirmationClarificationSeverity(rawValue: severity)
    }

    public static func normalizedInputs(_ inputs: [String]) -> [ActivityConfirmationClarificationInput] {
        var seen: Set<String> = []
        return inputs.compactMap { rawValue in
            guard seen.insert(rawValue).inserted else { return nil }
            return ActivityConfirmationClarificationInput(rawValue: rawValue)
        }
    }

    public static func inputsAreValid(_ inputs: [String]) -> Bool {
        inputs.allSatisfy { ActivityConfirmationClarificationInput(rawValue: $0) != nil }
    }

    public static func outcomeSeverityPairIsValid(
        outcome: ActivityConfirmationClarificationOutcome?,
        severity: ActivityConfirmationClarificationSeverity?
    ) -> Bool {
        switch (outcome, severity) {
        case (.rewritten?, .success?), (.split?, .success?), (.systemFiltered?, .success?),
             (.unchanged?, .neutral?), (.insufficientInformation?, .warning?),
             (.evidenceUnavailable?, .warning?):
            return true
        default:
            return false
        }
    }

    public static func generatedAtTimeLabel(
        _ value: String,
        timeZone: TimeZone = .current
    ) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = formatter.date(from: trimmed) ?? {
            formatter.formatOptions = [.withInternetDateTime]
            return formatter.date(from: trimmed)
        }()
        guard let date else { return "" }
        let display = DateFormatter()
        display.locale = Locale(identifier: "zh_CN")
        display.timeZone = timeZone
        display.dateFormat = "HH:mm"
        return display.string(from: date)
    }

    public static func feedbackMessage(
        status: String,
        outcome: ActivityConfirmationClarificationOutcome?,
        message: String
    ) -> String {
        let safeMessage = message.trimmingCharacters(in: .whitespacesAndNewlines)
        switch status {
        case "clarified":
            if !safeMessage.isEmpty { return safeMessage }
            switch outcome {
            case .systemFiltered:
                return "已重新核对并移除不需要确认的问题。"
            case .insufficientInformation:
                return "已重新梳理，但仍没有足够信息。"
            case .evidenceUnavailable:
                return "原截图不可用；已使用现有安全信息复核，但没有可安全更新的信息。"
            case .unchanged:
                return "已重新读取可用证据，当前问题保持不变。"
            case .rewritten, .split:
                return "已完成重新梳理。"
            case nil:
                return "已重新梳理，但没有发现新增可用信息。"
            }
        case "stale":
            return "这组线索已经变化，未提交任何重新梳理结果；已重新读取最新问题。"
        case "unsafe_contract":
            return "重新梳理结果无法解析或未满足本机安全契约。"
        default:
            return safeMessage.isEmpty
                ? "重新梳理未完成：\(status.isEmpty ? "unknown" : status)。"
                : safeMessage
        }
    }

    public static func completedPayloadIsValid(
        status: String,
        groupKey: String,
        outcome: ActivityConfirmationClarificationOutcome?,
        severity: ActivityConfirmationClarificationSeverity?,
        generatedAt: String,
        questionGroupCount: Int,
        containsRootGroup: Bool,
        memberRevisionsComplete: Bool,
        inputsFieldPresent: Bool,
        inputsValid: Bool,
        evidenceStatusValid: Bool
    ) -> Bool {
        guard status == "clarified" else { return true }
        guard !groupKey.isEmpty,
              outcomeSeverityPairIsValid(outcome: outcome, severity: severity),
              !generatedAtTimeLabel(generatedAt).isEmpty,
              inputsFieldPresent,
              inputsValid,
              evidenceStatusValid,
              memberRevisionsComplete
        else { return false }
        if outcome == .systemFiltered {
            return !containsRootGroup
        }
        return questionGroupCount > 0 && containsRootGroup
    }
}

public enum ConfirmedActivityContractPolicy {
    public static func isReadable(source: String, rawAccess: Bool) -> Bool {
        source == "confirmed_activity_view" && !rawAccess
    }
}

public enum StatusRefreshRetentionPolicy {
    public static func preservesConfirmedActivity(for status: String) -> Bool {
        let normalized = status.lowercased()
        return !["unknown", "busy", "unavailable", "database_busy"].contains(normalized)
    }
}

public enum RecordingControlAction: Equatable {
    case pause
    case resume
    case start
    case none
}

public struct RecordingControlPresentation: Equatable {
    public let action: RecordingControlAction
    public let title: String
    public let systemImage: String
    public let headline: String
    public let detail: String

    public static func resolve(
        status: String,
        launchAgentLoaded: Bool,
        launchAgentRunning: Bool,
        pauseLabel: String,
        statusExplanation: String,
        recordingPaused: Bool = false
    ) -> RecordingControlPresentation {
        if status == "error" && launchAgentRunning {
            return RecordingControlPresentation(action: .none, title: "", systemImage: "exclamationmark.triangle",
                headline: "后台需要检查", detail: statusExplanation.isEmpty ? "服务仍在运行，处理进展需要确认。" : statusExplanation)
        }
        if recordingPaused {
            return RecordingControlPresentation(action: .resume, title: "继续记录", systemImage: "play.fill",
                headline: "记录已暂停", detail: "继续记录会恢复文件追踪；屏幕仍遵守原有期限。")
        }
        if pauseLabel == "屏幕采集已关闭" && launchAgentRunning {
            return RecordingControlPresentation(action: .pause, title: "暂停文件追踪", systemImage: "doc.badge.clock",
                headline: "仅文件追踪", detail: "屏幕采集已关闭。需要时可从菜单手动开启 30 分钟。")
        }

        if status == "paused" {
            return RecordingControlPresentation(
                action: .resume,
                title: "继续记录",
                systemImage: "play.fill",
                headline: "记录已暂停",
                detail: pauseLabel
            )
        }

        if status == "locked" {
            return RecordingControlPresentation(
                action: .none,
                title: "",
                systemImage: "lock.fill",
                headline: "锁屏期间不记录",
                detail: "解锁后会自动继续，不需要手动恢复。"
            )
        }

        if status == "session_state_unknown" {
            return RecordingControlPresentation(
                action: .none,
                title: "",
                systemImage: "questionmark.circle",
                headline: "暂时无法确认会话状态",
                detail: "系统已按隐私优先停止新采集，确认状态后会自动继续。"
            )
        }

        if status == "busy" || status == "unavailable" || status == "unknown" {
            return RecordingControlPresentation(
                action: .none,
                title: "",
                systemImage: "clock.arrow.circlepath",
                headline: "状态未知",
                detail: statusExplanation
            )
        }

        if !launchAgentRunning {
            return RecordingControlPresentation(
                action: .start,
                title: "开始记录",
                systemImage: "record.circle",
                headline: launchAgentLoaded ? "记录服务需要重新启动" : "当前没有记录",
                detail: "启动后会继续在本机生成清洗后的活动记录。"
            )
        }

        return RecordingControlPresentation(
            action: .pause,
            title: "暂停",
            systemImage: "pause.fill",
            headline: "正在记录",
            detail: "一切正常。需要暂时停止时再点暂停。"
        )
    }
}

public struct DecisionSurfaceItem: Equatable {
    public let key: String
    public let decisionState: String
    public let queueBucket: String
    public let canDirectWrite: Bool
    public let canRewriteWrite: Bool

    public init(
        key: String,
        decisionState: String,
        queueBucket: String,
        canDirectWrite: Bool,
        canRewriteWrite: Bool
    ) {
        self.key = key
        self.decisionState = decisionState
        self.queueBucket = queueBucket
        self.canDirectWrite = canDirectWrite
        self.canRewriteWrite = canRewriteWrite
    }
}

public enum DecisionSurfacePolicy {
    public static func actionableItems(
        memoryWriteActions: Bool,
        items: [DecisionSurfaceItem]
    ) -> [DecisionSurfaceItem] {
        guard memoryWriteActions else { return [] }
        return items.filter {
            $0.decisionState == "needs_decision"
                && $0.queueBucket != "context_only"
                && ($0.canDirectWrite || $0.canRewriteWrite)
        }
    }
}

public struct DocumentChangeCoverage: Equatable {
    public let enabled: Bool
    public let exportEnabled: Bool
    public let baselineStatus: String
    public let rootCount: Int
    public let baselineIndexedFiles: Int
    public let eventGapCount: Int
    public let benignEventCount: Int
    public let pendingEventCount: Int?
    public let resumePending: Bool
    public let coverageStartedAt: String
    public let lastEventAt: String
    public let isComplete: Bool

    fileprivate init() {
        enabled = false
        exportEnabled = false
        baselineStatus = "unavailable"
        rootCount = 0
        baselineIndexedFiles = 0
        eventGapCount = 0
        benignEventCount = 0
        pendingEventCount = nil
        resumePending = false
        coverageStartedAt = ""
        lastEventAt = ""
        isComplete = false
    }

    fileprivate init?(validated dictionary: [String: Any]) {
        guard let enabled = DocumentChangesContract.bool(dictionary["enabled"]),
              let exportEnabled = DocumentChangesContract.bool(dictionary["export_enabled"]),
              let baselineStatus = dictionary["baseline_status"] as? String,
              DocumentChangesContract.baselineStatuses.contains(baselineStatus),
              let rootCount = DocumentChangesContract.nonnegativeInt(dictionary["root_count"]),
              let baselineIndexedFiles = DocumentChangesContract.nonnegativeInt(dictionary["baseline_indexed_files"]),
              let eventGapCount = DocumentChangesContract.nonnegativeInt(dictionary["event_gap_count"]),
              let benignEventCount = DocumentChangesContract.nonnegativeInt(dictionary["benign_event_count"]),
              let coverageStartedAt = dictionary["coverage_started_at"] as? String,
              let lastEventAt = dictionary["last_event_at"] as? String,
              let coverageStatus = dictionary["coverage_status"] as? String,
              DocumentChangesContract.coverageStatuses.contains(coverageStatus),
              let isComplete = DocumentChangesContract.bool(dictionary["is_complete"])
        else {
            return nil
        }
        if dictionary["pending_event_count"] != nil && DocumentChangesContract.nonnegativeInt(dictionary["pending_event_count"]) == nil { return nil }
        if dictionary["resume_pending"] != nil && DocumentChangesContract.bool(dictionary["resume_pending"]) == nil { return nil }
        guard !isComplete || (
            (DocumentChangesContract.nonnegativeInt(dictionary["pending_event_count"]) ?? 0) == 0
                && DocumentChangesContract.bool(dictionary["resume_pending"]) != true
                && enabled
                && baselineStatus == "complete"
                && coverageStatus == "complete"
                && rootCount > 0
                && eventGapCount == 0
        ) else {
            return nil
        }
        self.enabled = enabled
        self.exportEnabled = exportEnabled
        self.baselineStatus = baselineStatus
        self.rootCount = rootCount
        self.baselineIndexedFiles = baselineIndexedFiles
        self.eventGapCount = eventGapCount
        self.benignEventCount = benignEventCount
        self.pendingEventCount = DocumentChangesContract.nonnegativeInt(dictionary["pending_event_count"])
        self.resumePending = DocumentChangesContract.bool(dictionary["resume_pending"]) ?? false
        self.coverageStartedAt = coverageStartedAt
        self.lastEventAt = lastEventAt
        self.isComplete = isComplete
    }
}

public struct DocumentChangeTotals: Equatable {
    public let distinctFiles: Int
    public let created: Int
    public let modified: Int
    public let renamed: Int
    public let deleted: Int
    public let detailed: Int
    public let metadataOnly: Int
    public let protected: Int
    public let aiOnly: Int
    public let userModified: Int
    public let unattributed: Int

    fileprivate init() {
        distinctFiles = 0
        created = 0
        modified = 0
        renamed = 0
        deleted = 0
        detailed = 0
        metadataOnly = 0
        protected = 0
        aiOnly = 0
        userModified = 0
        unattributed = 0
    }

    fileprivate init?(validated dictionary: [String: Any]) {
        guard let distinctFiles = DocumentChangesContract.nonnegativeInt(dictionary["distinct_files"]),
              let created = DocumentChangesContract.nonnegativeInt(dictionary["created"]),
              let modified = DocumentChangesContract.nonnegativeInt(dictionary["modified"]),
              let renamed = DocumentChangesContract.nonnegativeInt(dictionary["renamed"]),
              let deleted = DocumentChangesContract.nonnegativeInt(dictionary["deleted"]),
              let detailed = DocumentChangesContract.nonnegativeInt(dictionary["detailed"]),
              let metadataOnly = DocumentChangesContract.nonnegativeInt(dictionary["metadata_only"]),
              let protected = DocumentChangesContract.nonnegativeInt(dictionary["protected"]),
              let aiOnly = DocumentChangesContract.nonnegativeInt(dictionary["ai_only"]),
              let userModified = DocumentChangesContract.nonnegativeInt(dictionary["user_modified"]),
              let unattributed = DocumentChangesContract.nonnegativeInt(dictionary["unattributed"]),
              detailed + metadataOnly + protected == distinctFiles,
              aiOnly + userModified + unattributed == distinctFiles
        else {
            return nil
        }
        self.distinctFiles = distinctFiles
        self.created = created
        self.modified = modified
        self.renamed = renamed
        self.deleted = deleted
        self.detailed = detailed
        self.metadataOnly = metadataOnly
        self.protected = protected
        self.aiOnly = aiOnly
        self.userModified = userModified
        self.unattributed = unattributed
    }
}

public struct DocumentChangeItem: Equatable, Identifiable {
    public let projectLabel: String
    public let fileNameRedacted: String
    public let fileType: String
    public let changeKind: String
    public let firstChangedAt: String
    public let lastChangedAt: String
    public let versionCount: Int
    public let changeSummaryRedacted: String
    public let detailLevel: String
    public let confidence: String
    public let actorClassification: String
    public let attributionConfidence: String
    public let attributionReasonCodes: [String]
    public let attributionTool: String

    public var id: String {
        [projectLabel, fileNameRedacted, changeKind, firstChangedAt, lastChangedAt].joined(separator: "\u{1f}")
    }

    fileprivate init?(validated dictionary: [String: Any]) {
        guard DocumentChangesContract.forbiddenItemKeys.isDisjoint(with: dictionary.keys),
              let projectLabel = dictionary["project_label"] as? String,
              let fileNameRedacted = dictionary["file_name_redacted"] as? String,
              !fileNameRedacted.isEmpty,
              let fileType = dictionary["file_type"] as? String,
              !fileType.isEmpty,
              let changeKind = dictionary["change_kind"] as? String,
              DocumentChangesContract.changeKinds.contains(changeKind),
              let firstChangedAt = dictionary["first_changed_at"] as? String,
              !firstChangedAt.isEmpty,
              let lastChangedAt = dictionary["last_changed_at"] as? String,
              !lastChangedAt.isEmpty,
              let versionCount = DocumentChangesContract.positiveInt(dictionary["version_count"]),
              let changeSummaryRedacted = dictionary["change_summary_redacted"] as? String,
              let detailLevel = dictionary["detail_level"] as? String,
              DocumentChangesContract.safeDetailLevels.contains(detailLevel),
              let confidence = dictionary["confidence"] as? String,
              DocumentChangesContract.confidences.contains(confidence),
              let actorClassification = dictionary["actor_classification"] as? String,
              DocumentChangesContract.actorClassifications.contains(actorClassification),
              let attributionConfidence = dictionary["attribution_confidence"] as? String,
              DocumentChangesContract.attributionConfidences.contains(attributionConfidence),
              let attributionReasonCodes = dictionary["attribution_reason_codes"] as? [String],
              attributionReasonCodes.count <= 12,
              let attributionTool = dictionary["attribution_tool"] as? String
        else {
            return nil
        }
        self.projectLabel = projectLabel
        self.fileNameRedacted = fileNameRedacted
        self.fileType = fileType
        self.changeKind = changeKind
        self.firstChangedAt = firstChangedAt
        self.lastChangedAt = lastChangedAt
        self.versionCount = versionCount
        self.changeSummaryRedacted = changeSummaryRedacted
        self.detailLevel = detailLevel
        self.confidence = confidence
        self.actorClassification = actorClassification
        self.attributionConfidence = attributionConfidence
        self.attributionReasonCodes = attributionReasonCodes
        self.attributionTool = attributionTool
    }

    public var displayName: String {
        if detailLevel == "protected" { return "受保护文件" }
        return fileNameRedacted.isEmpty ? "未命名文件" : fileNameRedacted
    }

    public var safeSummary: String {
        if detailLevel == "protected" {
            return "检测到受保护文件变化；未显示文件名或内容。"
        }
        if detailLevel == "metadata_only" || changeSummaryRedacted.isEmpty {
            return "检测到文件变化，但没有可安全概括的前后内容。"
        }
        return changeSummaryRedacted
    }
}

public struct DocumentWatchRoot: Equatable, Identifiable {
    public let rootRef: String
    public let label: String
    public let displayPathRedacted: String
    public let enabled: Bool

    public var id: String { rootRef }

    fileprivate init?(validated dictionary: [String: Any]) {
        guard DocumentChangesContract.forbiddenRootKeys.isDisjoint(with: dictionary.keys),
              let rootRef = dictionary["root_ref"] as? String,
              DocumentChangesContract.isSafeRootRef(rootRef),
              let label = dictionary["label"] as? String,
              let displayPathRedacted = dictionary["display_path_redacted"] as? String,
              let enabled = DocumentChangesContract.bool(dictionary["enabled"])
        else {
            return nil
        }
        self.rootRef = rootRef
        self.label = label
        self.displayPathRedacted = displayPathRedacted
        self.enabled = enabled
    }

    public var displayLabel: String {
        if !label.isEmpty { return label }
        return displayPathRedacted.isEmpty ? "已授权目录" : displayPathRedacted
    }
}

public struct DocumentChangesSnapshot: Equatable {
    public enum State: Equatable {
        case unavailable
        case available
        case blocked
    }

    public let state: State
    public let source: String
    public let coverage: DocumentChangeCoverage
    public let totals: DocumentChangeTotals
    public let items: [DocumentChangeItem]
    public let roots: [DocumentWatchRoot]
    public let limitations: [String]

    public init() {
        self.init(
            state: .unavailable,
            source: "",
            coverage: DocumentChangeCoverage(),
            totals: DocumentChangeTotals(),
            items: [],
            roots: [],
            limitations: ["文档变更状态尚未返回。"]
        )
    }

    private init(blockedMessage: String) {
        self.init(
            state: .blocked,
            source: "",
            coverage: DocumentChangeCoverage(),
            totals: DocumentChangeTotals(),
            items: [],
            roots: [],
            limitations: [blockedMessage]
        )
    }

    private init(
        state: State,
        source: String,
        coverage: DocumentChangeCoverage,
        totals: DocumentChangeTotals,
        items: [DocumentChangeItem],
        roots: [DocumentWatchRoot],
        limitations: [String]
    ) {
        self.state = state
        self.source = source
        self.coverage = coverage
        self.totals = totals
        self.items = items
        self.roots = roots
        self.limitations = limitations
    }

    public init(dictionary: [String: Any], allowOwnerLocalObservation: Bool = false) {
        guard DocumentChangesContract.bool(dictionary["raw_access"]) == false else {
            self.init(blockedMessage: "文档变更接口没有明确声明 raw_access=false，已停止展示。")
            return
        }
        let source = dictionary["source"] as? String ?? ""
        let ownerLocal = allowOwnerLocalObservation
            && source == "document_owner_local_observation"
            && dictionary["contract_version"] as? String == "DocumentOwnerLocalObservationV2"
            && dictionary["projection_type"] as? String == "owner_local_observation"
            && dictionary["verification_state"] as? String == "unverified"
            && DocumentChangesContract.bool(dictionary["trusted_export_eligible"]) == false
            && dictionary["roots"] == nil
        guard source == "filesystem_confirmed" || ownerLocal else {
            self.init(blockedMessage: "文档记录的来源或核验标记不符合当前页面要求，已停止展示。")
            return
        }
        guard let coverageDictionary = dictionary["coverage"] as? [String: Any],
              let totalsDictionary = dictionary["totals"] as? [String: Any],
              let itemDictionaries = dictionary["items"] as? [[String: Any]],
              let limitations = dictionary["limitations"] as? [String]
        else {
            self.init(blockedMessage: "文档变更接口缺少必需的安全字段，已停止展示。")
            return
        }
        guard let coverage = DocumentChangeCoverage(validated: coverageDictionary),
              let totals = DocumentChangeTotals(validated: totalsDictionary)
        else {
            self.init(blockedMessage: "文档变更 coverage 或 totals 不符合安全契约，已停止展示。")
            return
        }
        guard !limitations.contains("root_control_refs_incomplete") else {
            self.init(blockedMessage: "文档追踪目录控制引用不完整，已停止展示。")
            return
        }
        guard itemDictionaries.allSatisfy({
            guard let detailLevel = $0["detail_level"] as? String else { return false }
            return DocumentChangesContract.detailLevels.contains(detailLevel)
        }) else {
            self.init(blockedMessage: "文档变更项包含未知的 detail_level，已停止展示。")
            return
        }

        // Protected rows are count-only. Drop their dictionaries before any
        // model is constructed so even a malformed Core response cannot make
        // a protected filename transiently enter the UI model.
        let safeItemDictionaries = itemDictionaries.filter { ($0["detail_level"] as? String) != "protected" }
        let safeItems = safeItemDictionaries.compactMap(DocumentChangeItem.init(validated:))
        guard safeItems.count == safeItemDictionaries.count,
              safeItems.count <= totals.distinctFiles
        else {
            self.init(blockedMessage: "文档变更项缺少脱敏字段或包含未知枚举，已停止展示。")
            return
        }

        let rootDictionaries: [[String: Any]]
        if let rawRoots = dictionary["roots"] {
            guard let typedRoots = rawRoots as? [[String: Any]] else {
                self.init(blockedMessage: "文档追踪目录不符合脱敏契约，已停止展示。")
                return
            }
            rootDictionaries = typedRoots
        } else {
            rootDictionaries = []
        }
        let roots = rootDictionaries.compactMap(DocumentWatchRoot.init(validated:))
        guard roots.count == rootDictionaries.count,
              Set(roots.map(\.rootRef)).count == roots.count,
              dictionary["roots"] == nil || roots.count == coverage.rootCount
        else {
            self.init(blockedMessage: "文档追踪目录缺少脱敏字段，已停止展示。")
            return
        }
        self.init(
            state: .available,
            source: source,
            coverage: coverage,
            totals: totals,
            items: safeItems,
            roots: roots,
            limitations: limitations
        )
    }

    public var isAvailable: Bool { state == .available }
    public var isBlocked: Bool { state == .blocked }
}

private enum DocumentChangesContract {
    static let baselineStatuses: Set<String> = ["disabled", "pending", "building", "partial", "complete"]
    static let coverageStatuses: Set<String> = ["disabled", "partial", "complete"]
    static let changeKinds: Set<String> = ["created", "modified", "renamed", "deleted"]
    static let detailLevels: Set<String> = ["detailed", "metadata_only", "protected"]
    static let safeDetailLevels: Set<String> = ["detailed", "metadata_only"]
    static let confidences: Set<String> = ["local_file_changed", "likely_user_edit"]
    static let actorClassifications: Set<String> = ["ai_only", "user_modified", "unattributed"]
    static let attributionConfidences: Set<String> = [
        "confirmed_continuous_ai_only", "confirmed_human_editor_evidence", "unknown"
    ]
    static let forbiddenItemKeys: Set<String> = [
        "absolute_path", "content", "content_hmac", "diff", "file_id", "file_name",
        "full_path", "path", "privacy_flags", "structure_hmac_json", "summary",
    ]
    static let forbiddenRootKeys: Set<String> = ["absolute_path", "full_path", "path", "real_path", "root_id", "root_label"]

    static func isSafeRootRef(_ value: String) -> Bool {
        guard value.hasPrefix("rootref_"), value.count == 32 else {
            return false
        }
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        return value.unicodeScalars.allSatisfy(allowed.contains)
    }

    static func bool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID()
        else {
            return nil
        }
        return number.boolValue
    }

    static func nonnegativeInt(_ value: Any?) -> Int? {
        guard let boxed = value as? NSNumber,
              CFGetTypeID(boxed) != CFBooleanGetTypeID(),
              let number = value as? Int,
              number >= 0
        else {
            return nil
        }
        return number
    }

    static func positiveInt(_ value: Any?) -> Int? {
        guard let number = nonnegativeInt(value), number > 0 else { return nil }
        return number
    }
}

public enum DocumentChangesPresentation {
    public static func todayItems(from snapshot: DocumentChangesSnapshot) -> [DocumentChangeItem] {
        guard snapshot.isAvailable else { return [] }
        return Array(snapshot.items.prefix(3))
    }
}

public enum DocumentChangesFreshnessPolicy {
    /// Confirmed file changes must never survive a core response that could not
    /// produce a fresh payload.  Countdown fields may be preserved separately.
    public static func mustDiscardConfirmedChanges(coreStatus: String) -> Bool {
        ["unknown", "busy", "unavailable", "database_busy"].contains(coreStatus.lowercased())
    }
}

public enum ActivityConfirmationDiagnosticsLoadPolicy {
    public static func shouldReloadAfterCardsReplacement(
        diagnosticsStatus: String,
        diagnosticsExpanded: Bool,
        calibrationsExpanded: Bool
    ) -> Bool {
        diagnosticsStatus == "unknown"
            && (diagnosticsExpanded || calibrationsExpanded)
    }
}

public enum ActivityConfirmationCardInteractionPolicy {
    public static func showsAnswerActions(isInformationInsufficient: Bool) -> Bool {
        !isInformationInsufficient
    }
}

public struct ActivityConfirmationGenerationFence: Equatable, Sendable {
    public let refreshGeneration: UInt64
    public let mutationGeneration: UInt64

    public init(refreshGeneration: UInt64, mutationGeneration: UInt64) {
        self.refreshGeneration = refreshGeneration
        self.mutationGeneration = mutationGeneration
    }

    public func allows(
        currentRefreshGeneration: UInt64,
        currentMutationGeneration: UInt64
    ) -> Bool {
        refreshGeneration == currentRefreshGeneration
            && mutationGeneration == currentMutationGeneration
    }
}

public enum ActivityConfirmationV5Contract {
    public static let forbiddenFields: Set<String> = [
        "cleaned_contents",
        "cleaned_content",
        "safe_observation",
        "source_activity_ids",
        "deletion_group_ids",
        "source_ids",
        "evidence_ids",
        "trigger_sentence",
        "content_sha256",
        "transcript_path",
        "absolute_path",
        "path",
        "prompt",
        "answer",
        "command",
        "diff",
        "ocr",
    ]

    public static func excludesForbiddenFields(_ dictionary: [String: Any]) -> Bool {
        dictionary.allSatisfy { key, value in
            !forbiddenFields.contains(key) && valueExcludesForbiddenFields(value)
        }
    }

    private static func valueExcludesForbiddenFields(_ value: Any) -> Bool {
        if let dictionary = value as? [String: Any] {
            return excludesForbiddenFields(dictionary)
        }
        if let array = value as? [Any] {
            return array.allSatisfy(valueExcludesForbiddenFields)
        }
        return true
    }
}

// MARK: - Production Core status fail-closed reducer

public enum ProductionStatusInterface: Equatable {
    case fullStatus
    case quickStatus
}

public enum ProductionStatusReadState: Equatable {
    case current
    case busy
    case unknown
    case unavailable
    case blocked
}

public enum ProductionStatusActionAvailability: Equatable {
    case captureControls
    case refreshOnly
}

public struct ProductionStatusTrustedSnapshot: Equatable {
    public let status: String
    public let captureAllowedBySession: Bool
    public let launchAgentLoaded: Bool?
    public let launchAgentRunning: Bool?
    public let launchAgentPID: Int?
    public let launchAgentLastExitCode: Int?
    public let activityCount: Int?
    public let privacyCount: Int?
    public let gapCount: Int?
    public let rawResidueCount: Int?
    public let healthStatus: String?
    public let cadenceSeconds: Int?
    public let sessionRef: String?
    public let documentRef: String?
    public let currentRef: String?
    public let trustedRef: String?
    public let confirmedRef: String?
    public let verifiedRef: String?
    public let contextRef: String?
    public let bundleRef: String?
    public let bundleHash: String?
}

public struct ProductionStatusTransition: Equatable {
    public let accepted: Bool
    public let readState: ProductionStatusReadState
    public let shouldAdvanceSuccessfulStatusTime: Bool
    public let mustClearCurrentState: Bool
    public let requiresFullRefresh: Bool
    public let actionAvailability: ProductionStatusActionAvailability
}

/// One serial read chain. Awaiters return after required full follow-up reads.
/// A failed full read never schedules another read by itself.
@MainActor
public final class ProductionStatusRefreshReads {
    public enum Kind: Sendable { case quick, full }
    private var active: Task<Void, Never>?
    private var currentKind: Kind?
    private var pendingFull = false
    public var isRunning: Bool { active != nil }

    public init() {}

    public func request(
        _ kind: Kind,
        coalescingFull: Bool = false,
        quick: @escaping @MainActor @Sendable () async -> Bool,
        full: @escaping @MainActor @Sendable () async -> Void
    ) async {
        if let active {
            if kind == .full && (currentKind == .quick || !coalescingFull) {
                pendingFull = true
            }
            await active.value
            return
        }
        currentKind = kind
        let read = Task { @MainActor in
            var next = kind
            repeat {
                currentKind = next
                if next == .quick {
                    if await quick() { pendingFull = true }
                } else {
                    await full()
                }
                guard pendingFull else { break }
                pendingFull = false
                next = .full
            } while true
            currentKind = nil
            active = nil
        }
        active = read
        await read.value
    }
}

public struct ProductionStatusClientState {
    public private(set) var readState: ProductionStatusReadState = .unknown
    public private(set) var status = "unknown"
    public private(set) var reasonCode = "status_not_loaded"
    public private(set) var observedAt = ""
    public private(set) var lastSuccessfulReadAt: String?
    public private(set) var trustedSnapshot: ProductionStatusTrustedSnapshot?
    public private(set) var actionAvailability: ProductionStatusActionAvailability = .refreshOnly
    public private(set) var requiresFullRefresh = true
    public private(set) var hasCurrentFullSnapshot = false

    public init() {}

    @discardableResult
    public mutating func apply(
        dictionary: [String: Any],
        interface: ProductionStatusInterface
    ) -> ProductionStatusTransition {
        let expectedInterface = interface == .fullStatus ? "full_status" : "quick_status"
        guard dictionary["interface"] as? String == expectedInterface,
              ProductionStatusContract.bool(dictionary["raw_access"]) == false,
              let statusValue = dictionary["status"] as? String,
              !statusValue.isEmpty
        else {
            return block(reasonCode: "invalid_status_contract")
        }

        let normalizedStatus = statusValue.lowercased()
        let reason = (dictionary["reason_code"] as? String ?? "").lowercased()
        if ProductionStatusContract.unreadableStatuses.contains(normalizedStatus)
            || reason == "database_busy" {
            return applyUnreadable(
                dictionary: dictionary,
                normalizedStatus: normalizedStatus,
                reasonCode: reason,
                interface: interface
            )
        }

        let eventTime = ProductionStatusContract.eventTime(dictionary)
        let payloadLastSuccessfulRead = ProductionStatusContract.optionalString(
            dictionary["last_successful_read_at"]
        )
        guard ProductionStatusContract.currentStatuses.contains(normalizedStatus),
              let captureAllowed = ProductionStatusContract.bool(dictionary["capture_allowed_by_session"]),
              ProductionStatusContract.isParseableUTCTimestamp(eventTime),
              ProductionStatusContract.optionalUTCTimestampIsValid(
                  dictionary["last_successful_read_at"]
              ),
              payloadLastSuccessfulRead == nil || payloadLastSuccessfulRead == eventTime
        else {
            return block(reasonCode: "unknown_status_contract")
        }

        let serviceLoaded: Bool?
        let serviceRunning: Bool?
        if interface == .fullStatus {
            guard let loaded = ProductionStatusContract.bool(dictionary["launch_agent_loaded"]),
                  let running = ProductionStatusContract.bool(dictionary["launch_agent_running"]),
                  dictionary["runtime_identity"] is [String: Any]
            else {
                return block(reasonCode: "full_status_service_contract_missing")
            }
            serviceLoaded = loaded
            serviceRunning = running
        } else {
            serviceLoaded = ProductionStatusContract.optionalBool(dictionary["launch_agent_loaded"])
            serviceRunning = ProductionStatusContract.optionalBool(dictionary["launch_agent_running"])
        }

        let activityCount = ProductionStatusContract.optionalNonnegativeInt(dictionary["today_activity_count"])
        let privacyCount = ProductionStatusContract.optionalNonnegativeInt(dictionary["today_privacy_count"])
        let gapCount = ProductionStatusContract.optionalNonnegativeInt(dictionary["today_gap_count"])
        let rawResidueCount = ProductionStatusContract.optionalNonnegativeInt(dictionary["raw_residue_count"])
        if interface == .fullStatus,
           [activityCount, privacyCount, gapCount, rawResidueCount].contains(where: { $0 == nil }) {
            return block(reasonCode: "full_status_count_contract_missing")
        }

        status = normalizedStatus
        reasonCode = reason
        observedAt = eventTime
        lastSuccessfulReadAt = eventTime

        let previousFullSnapshotWasCurrent = hasCurrentFullSnapshot
        if interface == .fullStatus {
            hasCurrentFullSnapshot = true
        }
        readState = .current
        requiresFullRefresh = interface == .quickStatus && !hasCurrentFullSnapshot
        actionAvailability = ProductionStatusContract.allowsCaptureControls(normalizedStatus)
            && (interface == .fullStatus || previousFullSnapshotWasCurrent)
            ? .captureControls
            : .refreshOnly

        trustedSnapshot = ProductionStatusTrustedSnapshot(
            status: normalizedStatus,
            captureAllowedBySession: captureAllowed,
            launchAgentLoaded: serviceLoaded,
            launchAgentRunning: serviceRunning,
            launchAgentPID: ProductionStatusContract.optionalInt(dictionary["launch_agent_pid"]),
            launchAgentLastExitCode: ProductionStatusContract.optionalInt(dictionary["launch_agent_last_exit_code"]),
            activityCount: activityCount,
            privacyCount: privacyCount,
            gapCount: gapCount,
            rawResidueCount: rawResidueCount,
            healthStatus: ProductionStatusContract.healthStatus(dictionary),
            cadenceSeconds: ProductionStatusContract.optionalNonnegativeInt(dictionary["capture_interval_seconds"]),
            sessionRef: ProductionStatusContract.marker(
                dictionary,
                directKeys: ["session_ref", "session_state"],
                nestedKey: "current_activity_session",
                nestedKeys: ["session_ref", "session_id", "id"]
            ),
            documentRef: ProductionStatusContract.marker(
                dictionary,
                directKeys: ["document_ref"],
                nestedKey: "document_changes",
                nestedKeys: ["document_ref", "source"]
            ),
            currentRef: ProductionStatusContract.optionalString(dictionary["current_ref"]),
            trustedRef: ProductionStatusContract.firstReference(dictionary, directKey: "trusted_ref", listKey: "trusted_context_refs"),
            confirmedRef: ProductionStatusContract.marker(
                dictionary,
                directKeys: ["confirmed_ref"],
                nestedKey: "confirmed_activity",
                nestedKeys: ["confirmed_ref", "source"]
            ),
            verifiedRef: ProductionStatusContract.firstReference(dictionary, directKey: "verified_ref", listKey: "verified_context_refs"),
            contextRef: ProductionStatusContract.firstReference(dictionary, directKey: "context_ref", listKey: "context_refs"),
            bundleRef: ProductionStatusContract.optionalString(dictionary["bundle_ref"]),
            bundleHash: ProductionStatusContract.optionalString(dictionary["bundle_hash"])
        )

        return ProductionStatusTransition(
            accepted: true,
            readState: .current,
            shouldAdvanceSuccessfulStatusTime: true,
            mustClearCurrentState: false,
            requiresFullRefresh: requiresFullRefresh,
            actionAvailability: actionAvailability
        )
    }

    public var hasCurrentTrustedState: Bool {
        readState == .current && trustedSnapshot != nil
    }

    public var unknownStatusTextCN: String {
        guard readState != .current else { return "" }
        let time = observedAt.isEmpty ? "观察时间未知" : "观察时间 \(observedAt)"
        return "状态未知 · \(time)"
    }

    private mutating func applyUnreadable(
        dictionary: [String: Any],
        normalizedStatus: String,
        reasonCode: String,
        interface: ProductionStatusInterface
    ) -> ProductionStatusTransition {
        guard let observed = dictionary["observed_at"] as? String,
              ProductionStatusContract.isParseableUTCTimestamp(observed),
              ProductionStatusContract.bool(dictionary["capture_allowed_by_session"]) == false,
              ProductionStatusContract.bool(dictionary["actions_enabled"]) == false,
              ProductionStatusContract.unreadableFieldsAreNullOrAbsent(dictionary),
              ProductionStatusContract.optionalUTCTimestampIsValid(dictionary["last_successful_read_at"])
        else {
            return block(reasonCode: "invalid_unreadable_status_contract")
        }

        let payloadLastSuccessfulRead = ProductionStatusContract.optionalString(dictionary["last_successful_read_at"])
        if let payloadLastSuccessfulRead,
           payloadLastSuccessfulRead != lastSuccessfulReadAt {
            return block(reasonCode: "unproven_last_successful_status_time")
        }
        status = normalizedStatus == "busy" ? "unknown" : normalizedStatus
        self.reasonCode = reasonCode.isEmpty
            ? (normalizedStatus == "unavailable" ? "database_unavailable" : "status_unknown")
            : reasonCode
        observedAt = observed
        if let payloadLastSuccessfulRead, !payloadLastSuccessfulRead.isEmpty {
            lastSuccessfulReadAt = payloadLastSuccessfulRead
        }
        trustedSnapshot = nil
        hasCurrentFullSnapshot = false
        actionAvailability = .refreshOnly
        requiresFullRefresh = true
        if self.reasonCode == "database_busy" || normalizedStatus == "busy" {
            readState = .busy
        } else if normalizedStatus == "unavailable" {
            readState = .unavailable
        } else {
            readState = .unknown
        }

        return ProductionStatusTransition(
            accepted: true,
            readState: readState,
            shouldAdvanceSuccessfulStatusTime: false,
            mustClearCurrentState: true,
            requiresFullRefresh: true,
            actionAvailability: .refreshOnly
        )
    }

    private mutating func block(reasonCode: String) -> ProductionStatusTransition {
        readState = .blocked
        status = "unknown"
        self.reasonCode = reasonCode
        observedAt = ""
        trustedSnapshot = nil
        hasCurrentFullSnapshot = false
        actionAvailability = .refreshOnly
        requiresFullRefresh = true
        return ProductionStatusTransition(
            accepted: false,
            readState: .blocked,
            shouldAdvanceSuccessfulStatusTime: false,
            mustClearCurrentState: true,
            requiresFullRefresh: true,
            actionAvailability: .refreshOnly
        )
    }
}

// MARK: - Production diagnostics fail-closed reducer

public enum ProductionDiagnosticsReadState: Equatable {
    case current
    case busy
    case unknown
    case unavailable
    case blocked
}

public struct ProductionDiagnosticsTrustedSnapshot: Equatable {
    public let status: String
    public let kind: String
    public let mode: String
    public let summary: String
    public let checkedAt: String
    public let elapsedMilliseconds: Int
    public let targetResponseMilliseconds: Int
    public let probeNames: [String]
    public let actionIDs: [String]
    public let readOnlyActionCount: Int
    public let previewableActionCount: Int
    public let confirmRequiredActionCount: Int
}

public struct ProductionDiagnosticsTransition: Equatable {
    public let accepted: Bool
    public let readState: ProductionDiagnosticsReadState
    public let shouldAdvanceSuccessfulStatusTime: Bool
    public let mustClearCurrentState: Bool
    public let refreshOnly: Bool
}

/// Pure state used by both the production menu-bar target and the deterministic
/// assertion target. An unreadable diagnostics response replaces, rather than
/// merges with, the last successful probes/actions/counts.
public struct ProductionDiagnosticsClientState {
    public private(set) var readState: ProductionDiagnosticsReadState = .unknown
    public private(set) var status = "unknown"
    public private(set) var reasonCode = "diagnostics_not_loaded"
    public private(set) var observedAt = ""
    public private(set) var lastSuccessfulReadAt: String?
    public private(set) var mode = "quick"
    public private(set) var trustedSnapshot: ProductionDiagnosticsTrustedSnapshot?
    public private(set) var trustedDictionary: [String: Any]?
    public private(set) var refreshOnly = true

    public init() {}

    @discardableResult
    public mutating func apply(
        dictionary: [String: Any],
        requestedMode: String? = nil
    ) -> ProductionDiagnosticsTransition {
        guard ProductionStatusContract.bool(dictionary["raw_access"]) == false,
              dictionary["interface"] as? String == "diagnostics",
              let statusValue = dictionary["status"] as? String,
              !statusValue.isEmpty
        else {
            return block(reasonCode: "invalid_diagnostics_contract")
        }

        let normalizedStatus = statusValue.lowercased()
        let reason = (dictionary["reason_code"] as? String ?? "").lowercased()
        if ProductionStatusContract.unreadableStatuses.contains(normalizedStatus)
            || reason == "database_busy" {
            return applyUnreadable(
                dictionary: dictionary,
                normalizedStatus: normalizedStatus,
                reasonCode: reason,
                requestedMode: requestedMode
            )
        }

        guard ["ok", "partial", "attention_required"].contains(normalizedStatus),
              let checkedAt = dictionary["checked_at"] as? String,
              ProductionStatusContract.isParseableUTCTimestamp(checkedAt),
              ProductionDiagnosticsContract.safetyBoundaryIsClosed(dictionary),
              let decodedMode = ProductionDiagnosticsContract.mode(
                  dictionary["mode"] as? String ?? requestedMode
              ),
              let kind = dictionary["kind"] as? String,
              ["ok", "warning", "danger", "neutral"].contains(kind.lowercased()),
              let summary = dictionary["summary"] as? String,
              let elapsedMilliseconds = ProductionStatusContract.optionalNonnegativeInt(
                  dictionary["elapsed_ms"]
              ),
              let targetResponseMilliseconds = ProductionStatusContract.optionalNonnegativeInt(
                  dictionary["target_response_ms"]
              ),
              let probeDictionaries = dictionary["probes"] as? [[String: Any]],
              let actionDictionaries = dictionary["actions"] as? [[String: Any]],
              let actionCounts = dictionary["action_counts"] as? [String: Any],
              let readOnlyCount = ProductionStatusContract.optionalNonnegativeInt(
                  actionCounts["read_only"]
              ),
              let previewableCount = ProductionStatusContract.optionalNonnegativeInt(
                  actionCounts["previewable"]
              ),
              let confirmRequiredCount = ProductionStatusContract.optionalNonnegativeInt(
                  actionCounts["confirm_required"]
              )
        else {
            return block(reasonCode: "invalid_current_diagnostics_contract")
        }

        let probeNames = probeDictionaries.compactMap { probe -> String? in
            guard let name = probe["name"] as? String, !name.isEmpty else { return nil }
            return name
        }
        let actionIDs = actionDictionaries.compactMap { action -> String? in
            guard let identifier = action["id"] as? String, !identifier.isEmpty else { return nil }
            return identifier
        }
        guard probeNames.count == probeDictionaries.count,
              actionIDs.count == actionDictionaries.count,
              Set(probeNames).count == probeNames.count,
              Set(actionIDs).count == actionIDs.count
        else {
            return block(reasonCode: "invalid_diagnostics_item_contract")
        }

        readState = .current
        status = normalizedStatus
        reasonCode = reason
        observedAt = checkedAt
        lastSuccessfulReadAt = checkedAt
        mode = decodedMode
        trustedSnapshot = ProductionDiagnosticsTrustedSnapshot(
            status: normalizedStatus,
            kind: kind.lowercased(),
            mode: decodedMode,
            summary: summary,
            checkedAt: checkedAt,
            elapsedMilliseconds: elapsedMilliseconds,
            targetResponseMilliseconds: targetResponseMilliseconds,
            probeNames: probeNames,
            actionIDs: actionIDs,
            readOnlyActionCount: readOnlyCount,
            previewableActionCount: previewableCount,
            confirmRequiredActionCount: confirmRequiredCount
        )
        trustedDictionary = dictionary
        refreshOnly = false
        return ProductionDiagnosticsTransition(
            accepted: true,
            readState: .current,
            shouldAdvanceSuccessfulStatusTime: true,
            mustClearCurrentState: false,
            refreshOnly: false
        )
    }

    public var hasCurrentTrustedState: Bool {
        readState == .current && trustedSnapshot != nil && trustedDictionary != nil
    }

    public var unknownStatusTextCN: String {
        guard readState != .current else { return "" }
        let time = observedAt.isEmpty ? "观察时间未知" : "观察时间 \(observedAt)"
        return "状态未知 · \(time)"
    }

    private mutating func applyUnreadable(
        dictionary: [String: Any],
        normalizedStatus: String,
        reasonCode: String,
        requestedMode: String?
    ) -> ProductionDiagnosticsTransition {
        guard let observed = dictionary["observed_at"] as? String,
              ProductionStatusContract.isParseableUTCTimestamp(observed),
              ProductionStatusContract.optionalUTCTimestampIsValid(
                  dictionary["last_successful_read_at"]
              ),
              ProductionStatusContract.bool(dictionary["capture_allowed_by_session"]) == false,
              ProductionStatusContract.bool(dictionary["actions_enabled"]) == false,
              ProductionDiagnosticsContract.safetyBoundaryIsClosed(dictionary),
              ProductionDiagnosticsContract.unreadablePayloadIsClosed(dictionary),
              let decodedMode = ProductionDiagnosticsContract.mode(
                  dictionary["mode"] as? String ?? requestedMode
              )
        else {
            return block(reasonCode: "invalid_unreadable_diagnostics_contract")
        }

        let payloadLastSuccessfulRead = ProductionStatusContract.optionalString(
            dictionary["last_successful_read_at"]
        )
        if let payloadLastSuccessfulRead,
           payloadLastSuccessfulRead != lastSuccessfulReadAt {
            return block(reasonCode: "unproven_last_successful_diagnostics_time")
        }
        status = "unknown"
        self.reasonCode = reasonCode.isEmpty
            ? (normalizedStatus == "unavailable" ? "database_unavailable" : "status_unknown")
            : reasonCode
        observedAt = observed
        if let payloadLastSuccessfulRead, !payloadLastSuccessfulRead.isEmpty {
            lastSuccessfulReadAt = payloadLastSuccessfulRead
        }
        mode = decodedMode
        trustedSnapshot = nil
        trustedDictionary = nil
        refreshOnly = true
        if self.reasonCode == "database_busy" || normalizedStatus == "busy" {
            readState = .busy
        } else if normalizedStatus == "unavailable" {
            readState = .unavailable
        } else {
            readState = .unknown
        }
        return ProductionDiagnosticsTransition(
            accepted: true,
            readState: readState,
            shouldAdvanceSuccessfulStatusTime: false,
            mustClearCurrentState: true,
            refreshOnly: true
        )
    }

    private mutating func block(reasonCode: String) -> ProductionDiagnosticsTransition {
        readState = .blocked
        status = "unknown"
        self.reasonCode = reasonCode
        observedAt = ""
        trustedSnapshot = nil
        trustedDictionary = nil
        refreshOnly = true
        return ProductionDiagnosticsTransition(
            accepted: false,
            readState: .blocked,
            shouldAdvanceSuccessfulStatusTime: false,
            mustClearCurrentState: true,
            refreshOnly: true
        )
    }
}

private enum ProductionStatusContract {
    static let currentStatuses: Set<String> = [
        "capturing", "ready", "running", "paused", "locked",
        "session_state_unknown", "stopped", "error",
    ]
    static let unreadableStatuses: Set<String> = ["unknown", "busy", "unavailable"]
    static let unreadableFieldNames: Set<String> = [
        "service", "service_status", "launch_agent_loaded", "launch_agent_running",
        "launch_agent_pid", "launch_agent_last_exit_code", "today_activity_count",
        "today_privacy_count", "today_gap_count", "raw_residue_count", "counts",
        "health", "dashboard_health", "capture_freshness", "coverage_health",
        "privacy_health", "quality_health", "structured_signal_health",
        "hygiene_cleanup", "ha_automation", "quality_advice",
        "document_changes", "current_activity_session", "today_activity_sessions",
        "capture_interval_seconds", "session_state", "pause_mode", "pause_until",
        "paused_remaining_seconds", "verified_context_refs", "trusted_context_refs",
        "context_refs", "bundle_ref", "bundle_hash", "current_ref", "trusted_ref",
        "confirmed_ref", "verified_ref", "context_ref", "document_ref", "session_ref",
    ]

    static func bool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID()
        else {
            return nil
        }
        return number.boolValue
    }

    static func optionalBool(_ value: Any?) -> Bool? {
        guard value != nil, !(value is NSNull) else { return nil }
        return bool(value)
    }

    static func optionalInt(_ value: Any?) -> Int? {
        guard value != nil, !(value is NSNull) else { return nil }
        guard let boxed = value as? NSNumber,
              CFGetTypeID(boxed) != CFBooleanGetTypeID(),
              let integer = value as? Int
        else { return nil }
        return integer
    }

    static func optionalNonnegativeInt(_ value: Any?) -> Int? {
        guard let value = optionalInt(value), value >= 0 else { return nil }
        return value
    }

    static func optionalString(_ value: Any?) -> String? {
        guard value != nil, !(value is NSNull) else { return nil }
        return value as? String
    }

    static func optionalUTCTimestampIsValid(_ value: Any?) -> Bool {
        guard value != nil, !(value is NSNull) else { return true }
        guard let value = value as? String else { return false }
        return isParseableUTCTimestamp(value)
    }

    static func eventTime(_ dictionary: [String: Any]) -> String {
        for key in ["observed_at", "checked_at"] {
            if let value = dictionary[key] as? String, !value.isEmpty {
                return value
            }
        }
        return ""
    }

    static func isParseableUTCTimestamp(_ value: String) -> Bool {
        guard value.hasSuffix("Z") || value.hasSuffix("+00:00") else { return false }
        let plain = ISO8601DateFormatter()
        if plain.date(from: value) != nil {
            return true
        }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: value) != nil
    }

    static func unreadableFieldsAreNullOrAbsent(_ dictionary: [String: Any]) -> Bool {
        unreadableFieldNames.allSatisfy { key in
            guard let value = dictionary[key] else { return true }
            return value is NSNull
        }
    }

    static func allowsCaptureControls(_ status: String) -> Bool {
        !["locked", "session_state_unknown"].contains(status)
    }

    static func healthStatus(_ dictionary: [String: Any]) -> String? {
        if let value = dictionary["health"] as? String { return value }
        if let health = dictionary["dashboard_health"] as? [String: Any] {
            return health["status"] as? String
        }
        return nil
    }

    static func marker(
        _ dictionary: [String: Any],
        directKeys: [String],
        nestedKey: String,
        nestedKeys: [String]
    ) -> String? {
        for key in directKeys {
            if let value = dictionary[key] as? String, !value.isEmpty { return value }
        }
        guard let nested = dictionary[nestedKey] as? [String: Any] else { return nil }
        for key in nestedKeys {
            if let value = nested[key] as? String, !value.isEmpty { return value }
        }
        return nil
    }

    static func firstReference(
        _ dictionary: [String: Any],
        directKey: String,
        listKey: String
    ) -> String? {
        if let direct = dictionary[directKey] as? String, !direct.isEmpty { return direct }
        return (dictionary[listKey] as? [String])?.first
    }
}

private enum ProductionDiagnosticsContract {
    static func mode(_ value: String?) -> String? {
        guard let value, ["quick", "full"].contains(value) else { return nil }
        return value
    }

    static func safetyBoundaryIsClosed(_ dictionary: [String: Any]) -> Bool {
        guard ProductionStatusContract.bool(dictionary["raw_access"]) == false,
              dictionary["cloud_analysis"] as? String == "off",
              let boundary = dictionary["safety_boundary"] as? [String: Any]
        else {
            return false
        }
        for key in ["destructive_actions", "memory_write_actions", "rule_write_actions"]
        where ProductionStatusContract.bool(dictionary[key]) != false {
            return false
        }
        for key in [
            "raw_access", "destructive_actions", "memory_write_actions",
            "rule_write_actions", "reads_raw_sources",
        ] where ProductionStatusContract.bool(boundary[key]) != false {
            return false
        }
        if boundary["cloud_analysis"] as? String != "off" {
            return false
        }
        return true
    }

    static func unreadablePayloadIsClosed(_ dictionary: [String: Any]) -> Bool {
        guard (dictionary["kind"] as? String)?.lowercased() == "warning" else {
            return false
        }
        if dictionary["actions"] != nil || dictionary["action_counts"] != nil {
            return false
        }
        let probes = dictionary["probes"] as? [[String: Any]] ?? []
        guard probes.count == 1,
              probes[0]["name"] as? String == "database_busy",
              (probes[0]["kind"] as? String)?.lowercased() == "warning",
              (probes[0]["status"] as? String)?.lowercased() == "unknown"
        else {
            return false
        }
        for key in [
            "runtime", "service", "launch_agent_loaded", "launch_agent_running",
            "launch_agent_pid", "launch_agent_last_exit_code", "checked_probes",
            "skipped_probes", "check_scope",
        ] where dictionary[key] != nil && !(dictionary[key] is NSNull) {
            return false
        }
        return true
    }
}

// MARK: - Context Console fail-closed client state

public enum ContextConsoleFeatureGate {
    /// Phase 0/1 is source-only. Production actions remain hard disabled until
    /// the governed Memory Gateway and delivery verification loop are complete.
    public static let productionEnabled = false
}

public enum ContextConsoleReadState: String, Equatable {
    case available
    case disabled
    case unknown
    case busy
    case unavailable
    case blocked
}

public struct ContextConsoleCounts: Equatable {
    public let activeVerifiedContexts: Int
    public let activeUnverifiedClues: Int
    public let memoryCandidates: Int

    fileprivate init?(dictionary: [String: Any]) {
        guard
            let verified = ContextConsoleContract.nonnegativeInt(dictionary["active_verified_contexts"]),
            let clues = ContextConsoleContract.nonnegativeInt(dictionary["active_unverified_clues"]),
            let memory = ContextConsoleContract.nonnegativeInt(dictionary["memory_candidates"])
        else {
            return nil
        }
        activeVerifiedContexts = verified
        activeUnverifiedClues = clues
        memoryCandidates = memory
    }
}

public struct ContextConsoleReadEnvelope {
    public let state: ContextConsoleReadState
    public let reasonCode: String
    public let observedAt: String
    public let lastSuccessfulReadAt: String?
    public let counts: ContextConsoleCounts?
    public let actionsEnabled: Bool
    public let rawAccess: Bool
    public let verifiedContextRefs: [String]
    public let trustedContextRefs: [String]
    public let bundleHash: String?

    public init(dictionary: [String: Any]) {
        guard ContextConsoleContract.bool(dictionary["raw_access"]) == false,
              let statusText = dictionary["status"] as? String,
              let observedAt = dictionary["observed_at"] as? String,
              !observedAt.isEmpty
        else {
            self = .blocked(reasonCode: "invalid_context_status_contract")
            return
        }

        let reasonCode = dictionary["reason_code"] as? String ?? ""
        let lastRead = dictionary["last_successful_read_at"] as? String
        switch statusText.lowercased() {
        case "available":
            guard ContextConsoleContract.bool(dictionary["actions_enabled"]) == true,
                  let rawCounts = dictionary["counts"] as? [String: Any],
                  let counts = ContextConsoleCounts(dictionary: rawCounts)
            else {
                self = .blocked(reasonCode: "available_context_status_incomplete")
                return
            }
            let verified = ContextConsoleContract.safeOpaqueRefs(dictionary["verified_context_refs"])
            let trusted = ContextConsoleContract.safeOpaqueRefs(dictionary["trusted_context_refs"])
            let bundleHash = ContextConsoleContract.safeBundleHash(dictionary["bundle_hash"])
            self.init(
                state: .available,
                reasonCode: reasonCode,
                observedAt: observedAt,
                lastSuccessfulReadAt: lastRead ?? observedAt,
                counts: counts,
                actionsEnabled: ContextConsoleFeatureGate.productionEnabled,
                rawAccess: false,
                verifiedContextRefs: verified,
                trustedContextRefs: trusted,
                bundleHash: bundleHash
            )
        case "disabled":
            self.init(
                state: .disabled,
                reasonCode: reasonCode.isEmpty ? "feature_flag_off" : reasonCode,
                observedAt: observedAt,
                lastSuccessfulReadAt: lastRead,
                counts: nil,
                actionsEnabled: false,
                rawAccess: false,
                verifiedContextRefs: [],
                trustedContextRefs: [],
                bundleHash: nil
            )
        case "unknown":
            // A failed read must not preserve counts, service health, trusted
            // references, or a bundle from an older successful snapshot.
            self.init(
                state: reasonCode == "database_busy" ? .busy : .unknown,
                reasonCode: reasonCode.isEmpty ? "database_unavailable" : reasonCode,
                observedAt: observedAt,
                lastSuccessfulReadAt: lastRead,
                counts: nil,
                actionsEnabled: false,
                rawAccess: false,
                verifiedContextRefs: [],
                trustedContextRefs: [],
                bundleHash: nil
            )
        case "busy":
            self.init(
                state: .busy,
                reasonCode: reasonCode.isEmpty ? "database_busy" : reasonCode,
                observedAt: observedAt,
                lastSuccessfulReadAt: lastRead,
                counts: nil,
                actionsEnabled: false,
                rawAccess: false,
                verifiedContextRefs: [],
                trustedContextRefs: [],
                bundleHash: nil
            )
        case "unavailable":
            self.init(
                state: .unavailable,
                reasonCode: reasonCode.isEmpty ? "database_unavailable" : reasonCode,
                observedAt: observedAt,
                lastSuccessfulReadAt: lastRead,
                counts: nil,
                actionsEnabled: false,
                rawAccess: false,
                verifiedContextRefs: [],
                trustedContextRefs: [],
                bundleHash: nil
            )
        default:
            self = .blocked(reasonCode: "unknown_context_status")
        }
    }

    private init(
        state: ContextConsoleReadState,
        reasonCode: String,
        observedAt: String,
        lastSuccessfulReadAt: String?,
        counts: ContextConsoleCounts?,
        actionsEnabled: Bool,
        rawAccess: Bool,
        verifiedContextRefs: [String],
        trustedContextRefs: [String],
        bundleHash: String?
    ) {
        self.state = state
        self.reasonCode = reasonCode
        self.observedAt = observedAt
        self.lastSuccessfulReadAt = lastSuccessfulReadAt
        self.counts = counts
        self.actionsEnabled = actionsEnabled
        self.rawAccess = rawAccess
        self.verifiedContextRefs = verifiedContextRefs
        self.trustedContextRefs = trustedContextRefs
        self.bundleHash = bundleHash
    }

    private static func blocked(reasonCode: String) -> ContextConsoleReadEnvelope {
        ContextConsoleReadEnvelope(
            state: .blocked,
            reasonCode: reasonCode,
            observedAt: "",
            lastSuccessfulReadAt: nil,
            counts: nil,
            actionsEnabled: false,
            rawAccess: false,
            verifiedContextRefs: [],
            trustedContextRefs: [],
            bundleHash: nil
        )
    }
}

public struct ContextConsoleClientState {
    public private(set) var readState: ContextConsoleReadState = .unknown
    public private(set) var observedAt: String = ""
    public private(set) var lastSuccessfulReadAt: String?
    public private(set) var counts: ContextConsoleCounts?
    public private(set) var actionsEnabled = false
    public private(set) var verifiedContextRefs: [String] = []
    public private(set) var trustedContextRefs: [String] = []
    public private(set) var bundleHash: String?

    public init() {}

    public mutating func apply(dictionary: [String: Any]) {
        let envelope = ContextConsoleReadEnvelope(dictionary: dictionary)
        readState = envelope.state
        observedAt = envelope.observedAt
        lastSuccessfulReadAt = envelope.lastSuccessfulReadAt
        actionsEnabled = envelope.actionsEnabled

        guard envelope.state == .available else {
            // Revocation may have happened while Core was busy. Never retain an
            // older trusted snapshot across an unreadable state.
            counts = nil
            verifiedContextRefs = []
            trustedContextRefs = []
            bundleHash = nil
            return
        }
        counts = envelope.counts
        verifiedContextRefs = envelope.verifiedContextRefs
        trustedContextRefs = envelope.trustedContextRefs
        bundleHash = envelope.bundleHash
    }

    public var hasFreshTrustedSnapshot: Bool {
        readState == .available && counts != nil
    }
}

private enum ContextConsoleContract {
    static func bool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID()
        else {
            return nil
        }
        return number.boolValue
    }

    static func nonnegativeInt(_ value: Any?) -> Int? {
        guard let boxed = value as? NSNumber,
              CFGetTypeID(boxed) != CFBooleanGetTypeID(),
              let number = value as? Int,
              number >= 0
        else {
            return nil
        }
        return number
    }

    static func safeOpaqueRefs(_ value: Any?) -> [String] {
        guard let refs = value as? [String] else { return [] }
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        guard refs.count <= 100,
              refs.allSatisfy({ ref in
                  (ref.hasPrefix("verified_") || ref.hasPrefix("context_"))
                      && ref.count <= 128
                      && ref.unicodeScalars.allSatisfy(allowed.contains)
              })
        else {
            return []
        }
        return refs
    }

    static func safeBundleHash(_ value: Any?) -> String? {
        guard let value = value as? String, value.hasPrefix("sha256:") else { return nil }
        let digest = value.dropFirst("sha256:".count)
        let hex = CharacterSet(charactersIn: "0123456789abcdef")
        guard digest.count == 64, digest.unicodeScalars.allSatisfy(hex.contains) else { return nil }
        return value
    }
}

/// Classified failure of one core CLI invocation.  `runCoreBlocking` wraps
/// non-zero exits and timeouts with a text prefix before the CLI's own JSON
/// error payload; this classifier recovers the structured error so callers can
/// tell a transient `database_busy` apart from a real failure instead of
/// treating every wrapped payload as unparseable.
public enum CoreOutputError: Equatable {
    case databaseBusy
    case databaseMissing
    case timedOut
    case commandFailed
}

public enum CoreOutputErrorClassifier {
    public static func classify(_ raw: String) -> CoreOutputError? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let timedOut = trimmed.hasPrefix("personal-ai-monitor-core timed out after")
        let wrappedExit = trimmed.hasPrefix("personal-ai-monitor-core exited with")
        let couldNotRun = trimmed.hasPrefix("Could not run personal-ai-monitor-core")
        if couldNotRun { return .commandFailed }
        if let payload = embeddedErrorPayload(in: trimmed) {
            switch payload {
            case "database_busy": return .databaseBusy
            case "database_missing": return .databaseMissing
            default: return timedOut ? .timedOut : .commandFailed
            }
        }
        if timedOut { return .timedOut }
        if wrappedExit { return .commandFailed }
        return nil
    }

    private static func embeddedErrorPayload(in text: String) -> String? {
        guard let start = text.firstIndex(of: "{") else { return nil }
        let candidate = String(text[start...])
        guard
            let data = candidate.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            (object["status"] as? String) == "error",
            let error = object["error"] as? String
        else { return nil }
        return error
    }
}
