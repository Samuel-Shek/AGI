import Foundation
import PAMInteractionModel

private var completed = 0

private func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else {
        FileHandle.standardError.write(Data("FAILED: \(message)\n".utf8))
        exit(1)
    }
    completed += 1
}

private func jsonText(_ dictionary: [String: Any]) -> String {
    let data = try! JSONSerialization.data(withJSONObject: dictionary, options: [.sortedKeys])
    return String(data: data, encoding: .utf8)!
}

private func runNamedTest(_ name: String, _ body: () -> Void) {
    print("TEST \(name)")
    body()
    print("PASS \(name)")
}

private func productionStatusSuccessPayload(
    label: String,
    observedAt: String,
    seed: Int,
    status: String = "ready",
    captureAllowed: Bool = true
) -> [String: Any] {
    [
        "status": status,
        "reason_code": "",
        "interface": "full_status",
        "observed_at": observedAt,
        "last_successful_read_at": observedAt,
        "raw_access": false,
        "capture_allowed_by_session": captureAllowed,
        "runtime_identity": ["source": "fixture_\(label)"],
        "launch_agent_loaded": true,
        "launch_agent_running": true,
        "launch_agent_pid": seed + 1000,
        "launch_agent_last_exit_code": seed,
        "today_activity_count": seed + 10,
        "today_privacy_count": seed + 20,
        "today_gap_count": seed + 30,
        "raw_residue_count": seed + 40,
        "dashboard_health": ["status": "health_\(label)"],
        "capture_interval_seconds": seed + 50,
        "session_ref": "session_\(label)",
        "document_ref": "document_\(label)",
        "current_ref": "current_\(label)",
        "trusted_ref": "trusted_\(label)",
        "confirmed_ref": "confirmed_\(label)",
        "verified_ref": "verified_\(label)",
        "context_ref": "context_\(label)",
        "bundle_ref": "bundle_\(label)",
        "bundle_hash": "sha256:\(label)",
    ]
}

private func productionStatusBusyPayload(
    observedAt: String,
    lastSuccessfulReadAt: Any
) -> [String: Any] {
    [
        "status": "unknown",
        "reason_code": "database_busy",
        "kind": "warning",
        "interface": "full_status",
        "observed_at": observedAt,
        "last_successful_read_at": lastSuccessfulReadAt,
        "capture_allowed_by_session": false,
        "actions_enabled": false,
        "raw_access": false,
        "service": NSNull(),
        "service_status": NSNull(),
        "launch_agent_loaded": NSNull(),
        "launch_agent_running": NSNull(),
        "launch_agent_pid": NSNull(),
        "launch_agent_last_exit_code": NSNull(),
        "today_activity_count": NSNull(),
        "today_privacy_count": NSNull(),
        "today_gap_count": NSNull(),
        "raw_residue_count": NSNull(),
        "counts": NSNull(),
        "health": NSNull(),
        "dashboard_health": NSNull(),
        "capture_freshness": NSNull(),
        "coverage_health": NSNull(),
        "privacy_health": NSNull(),
        "quality_health": NSNull(),
        "structured_signal_health": NSNull(),
        "hygiene_cleanup": NSNull(),
        "ha_automation": NSNull(),
        "quality_advice": NSNull(),
        "document_changes": NSNull(),
        "current_activity_session": NSNull(),
        "today_activity_sessions": NSNull(),
        "capture_interval_seconds": NSNull(),
        "session_state": NSNull(),
        "pause_mode": NSNull(),
        "pause_until": NSNull(),
        "paused_remaining_seconds": NSNull(),
        "verified_context_refs": NSNull(),
        "trusted_context_refs": NSNull(),
        "context_refs": NSNull(),
        "bundle_ref": NSNull(),
        "bundle_hash": NSNull(),
        "current_ref": NSNull(),
        "trusted_ref": NSNull(),
        "confirmed_ref": NSNull(),
        "verified_ref": NSNull(),
        "context_ref": NSNull(),
        "document_ref": NSNull(),
        "session_ref": NSNull(),
    ]
}

private func productionDiagnosticsSuccessPayload(
    label: String,
    checkedAt: String,
    mode: String,
    kind: String = "ok",
    status: String = "ok"
) -> [String: Any] {
    [
        "status": status,
        "reason_code": "",
        "kind": kind,
        "interface": "diagnostics",
        "mode": mode,
        "checked_at": checkedAt,
        "elapsed_ms": label == "A" ? 101 : 202,
        "target_response_ms": mode == "quick" ? 2_000 : 90_000,
        "summary": "diagnostics_\(label)",
        "probes": [[
            "name": "probe_\(label)",
            "label": "Probe \(label)",
            "status": kind == "ok" ? "ok" : "warning",
            "kind": kind,
            "summary": "probe_summary_\(label)",
        ]],
        "recommendations": ["recommendation_\(label)"],
        "actions": [[
            "id": "action_\(label)",
            "probe": "probe_\(label)",
            "kind": "read_only",
            "label": "Action \(label)",
            "summary": "action_summary_\(label)",
        ]],
        "action_counts": [
            "read_only": label == "A" ? 7 : 3,
            "previewable": label == "A" ? 5 : 2,
            "confirm_required": label == "A" ? 4 : 1,
        ],
        "raw_access": false,
        "destructive_actions": false,
        "memory_write_actions": false,
        "rule_write_actions": false,
        "cloud_analysis": "off",
        "safety_boundary": [
            "raw_access": false,
            "destructive_actions": false,
            "memory_write_actions": false,
            "rule_write_actions": false,
            "cloud_analysis": "off",
            "reads_raw_sources": false,
        ],
    ]
}

private func productionDiagnosticsBusyPayload(
    observedAt: String,
    lastSuccessfulReadAt: Any
) -> [String: Any] {
    [
        "status": "unknown",
        "reason_code": "database_busy",
        "kind": "warning",
        "interface": "diagnostics",
        "observed_at": observedAt,
        "last_successful_read_at": lastSuccessfulReadAt,
        "capture_allowed_by_session": false,
        "actions_enabled": false,
        "summary": "database busy",
        "safety_boundary": [
            "raw_access": false,
            "destructive_actions": false,
            "memory_write_actions": false,
            "rule_write_actions": false,
            "cloud_analysis": "off",
            "reads_raw_sources": false,
        ],
        "probes": [[
            "name": "database_busy",
            "label": "Database",
            "status": "unknown",
            "kind": "warning",
            "summary": "database busy",
            "raw_access": false,
            "destructive_actions": false,
        ]],
        "recommendations": ["refresh"],
        "raw_access": false,
        "destructive_actions": false,
        "memory_write_actions": false,
        "rule_write_actions": false,
        "cloud_analysis": "off",
    ]
}

check(
    AppTab.allCases == [.today, .reflection, .evolution, .confirmations, .fileHistory, .review, .memoryReview, .systemStatus, .privacy, .settings],
    "navigation must retain Confirmations while adding File History"
)
check(
    AppTab.allCases.map(\.title) == ["今天", "思考与反思", "自主进化", "需要你决定", "记录", "回顾", "记忆库", "系统状态", "隐私与数据", "设置"],
    "legacy destinations remain routable with clear decision and records labels"
)
check(AppTab.dailyTabs == [.today, .reflection, .confirmations, .fileHistory, .memoryReview, .evolution], "daily navigation groups decisions, records and memory")
check(
    AppTab.managementTabs == [.systemStatus, .settings],
    "management navigation nests privacy in settings"
)
check(
    AppTab.memoryReview.navigationGroup == .daily,
    "memory browsing is a primary destination"
)

let confirmationBadgeCases: [(count: Int, visualText: String?, accessibilityLabel: String)] = [
    (0, nil, "待确认"),
    (99, "99", "待确认，99 条待确认"),
    (100, "99+", "待确认，100 条待确认"),
    (366, "99+", "待确认，366 条待确认"),
]
for badgeCase in confirmationBadgeCases {
    check(
        ActivityConfirmationBadgePresentation.visualText(pendingCount: badgeCase.count) == badgeCase.visualText,
        "the confirmation badge must show the exact count through 99 and use 99+ above 99"
    )
    check(
        ActivityConfirmationBadgePresentation.accessibilityLabel(
            tabTitle: "待确认",
            pendingCount: badgeCase.count
        ) == badgeCase.accessibilityLabel,
        "VoiceOver must announce the exact pending confirmation count even when the visual badge is capped"
    )
}

let confirmedActivity = ActivityConfirmationAnswerRequest(answer: .occurred, selectedReason: nil)
check(confirmedActivity.reason == "correct", "a confirmed activity must record a positive correction")
check(confirmedActivity.generalize == "exact", "positive activity confirmation must never generalize to similar items")
check(confirmedActivity.isValid, "positive confirmation must not require a negative reason")
check(
    ActivityConfirmationAnswer.allCases.map(\.rawValue) == ["occurred", "not_occurred", "ignored", "defer"],
    "the new confirmation UI must expose ignored instead of the legacy misrecognized action"
)
check(
    ActivityConfirmationAnswer.occurred.label == "发生了"
        && ActivityConfirmationAnswer.notOccurred.label == "没有发生"
        && ActivityConfirmationAnswer.ignored.label == "忽略该问题",
    "confirmation action labels must match the requested user-facing choices"
)

let missingReason = ActivityConfirmationAnswerRequest(answer: .notOccurred, selectedReason: nil)
check(!missingReason.isValid, "a rejected activity must require a user-selected reason")

let oldActivity = ActivityConfirmationAnswerRequest(answer: .notOccurred, selectedReason: .oldContent)
let preparationActivity = ActivityConfirmationAnswerRequest(answer: .notOccurred, selectedReason: .preparationOnly)
check(oldActivity.reason == "old_content", "a rejected activity must preserve the user's selected old-content reason")
check(oldActivity.generalize == "similar", "old/reference corrections may improve similar future evidence")

let ignoredActivity = ActivityConfirmationAnswerRequest(answer: .ignored, selectedReason: nil)
check(ignoredActivity.reason == "user_ignored", "ignored must use a distinct neutral reason")
check(ignoredActivity.generalize == "exact", "ignored must never generalize into calibration")
check(ignoredActivity.isValid, "ignored must not require a correction reason")

check(
    ActivityConfirmationReason.allCases.map(\.rawValue) == [
        "old_content", "reference_only", "preparation_only", "future_not_occurred", "sidebar_or_list",
        "vlm_or_ocr", "screen_region_or_app", "other",
    ],
    "the wire reason enum must retain historical correction values without exposing them as new UI actions"
)
check(
    ActivityConfirmationReason.options(for: .notOccurred) == [
        .oldContent, .preparationOnly, .futureNotOccurred, .referenceOnly, .other,
    ],
    "not-occurred reasons must describe time, preparation, future plans, and references in user language"
)
check(
    ActivityConfirmationReason.options(for: .ignored).isEmpty,
    "ignored must not open the old recognition-correction reason selector"
)
check(ActivityConfirmationReason.options(for: .occurred).isEmpty, "positive confirmation must not show correction reasons")
check(
    !ActivityConfirmationReason.options(for: .notOccurred, legacyCompatible: true).contains(.futureNotOccurred),
    "v1 compatibility cards must not offer a reason the old Core cannot parse"
)

let deferredActivity = ActivityConfirmationAnswerRequest(answer: .deferred, selectedReason: nil)
check(deferredActivity.reason == "other", "deferred answers use the neutral default without requiring a negative-reason choice")
check(deferredActivity.generalize == "exact", "deferred answers must never generalize")

check(
    ActivityConfirmationAnswerAvailability.supports(
        .ignored,
        answerOptions: ["occurred", "not_occurred", "ignored", "defer"],
        isLegacy: false
    ),
    "a v3 grouped card may show ignored only when Core advertises it"
)
check(
    !ActivityConfirmationAnswerAvailability.supports(
        .ignored,
        answerOptions: ["occurred", "not_occurred", "misrecognized", "defer"],
        isLegacy: false
    ),
    "legacy misrecognized capability must never be relabeled as ignored"
)
check(
    !ActivityConfirmationAnswerAvailability.supports(
        .ignored,
        answerOptions: ["ignored"],
        isLegacy: true
    ),
    "legacy cards must fail closed even if an unexpected ignored option appears"
)
check(
    ActivityConfirmationClarificationSubmissionPolicy.isEnabled(
        isLoading: false,
        isClarifying: false,
        canClarify: true
    ),
    "the clarification confirmation must be enabled only when the card can accept an idle submission"
)
check(
    !ActivityConfirmationClarificationSubmissionPolicy.isEnabled(
        isLoading: true,
        isClarifying: false,
        canClarify: true
    )
        && !ActivityConfirmationClarificationSubmissionPolicy.isEnabled(
            isLoading: false,
            isClarifying: true,
            canClarify: true
        )
        && !ActivityConfirmationClarificationSubmissionPolicy.isEnabled(
            isLoading: false,
            isClarifying: false,
            canClarify: false
        ),
    "loading, an active clarification, or an unsupported card must block duplicate clarification submissions"
)
check(
    ActivityConfirmationClarificationSubmissionPolicy.actionLabel(isClarifying: false) == "确认并重新梳理"
        && ActivityConfirmationClarificationSubmissionPolicy.actionLabel(isClarifying: true) == "正在重新梳理…",
    "the supplement confirmation must replace the old duplicate uncertainty action with one explicit label"
)

check(
    ActivityConfirmationContractMode.resolve(contractVersion: 2, hasQuestionGroups: true) == .grouped,
    "grouped contract question_groups must be authoritative even when the array is empty"
)
check(
    ActivityConfirmationContractMode.resolve(contractVersion: 3, hasQuestionGroups: true) == .grouped,
    "contract v3 question_groups must remain authoritative"
)
check(
    ActivityConfirmationContractMode.resolve(contractVersion: 1, hasQuestionGroups: false) == .legacyItems,
    "an old Core without question_groups must safely fall back to individual items"
)
check(
    !ActivityConfirmationStructuredDetailsPolicy.requiresBusinessDetails(contractVersion: 3),
    "contract v3 compatibility cards must remain answerable without hidden v4 business fields"
)
check(
    ActivityConfirmationStructuredDetailsPolicy.requiresBusinessDetails(contractVersion: 4),
    "contract v4 cards must require the three-layer business details"
)
check(
    ActivityConfirmationContractMode.resolve(contractVersion: 3, hasQuestionGroups: false) == .invalidGroupedContract,
    "a grouped contract without question_groups must fail closed instead of reading compatibility items"
)

let v4SummaryDictionary: [String: Any] = [
    "priority_counts": ["high": 1, "normal": 1, "low": 0],
    "pending_by_kind": ["occurrence": 1, "business_context": 1, "outcome": 0],
    "candidate_funnel": [
        "clear_activities": 40,
        "event_signals": 8,
        "eligible_questions": 3,
        "hard_filtered": 1,
        "pending": 2,
        "confirmed": 4,
        "reportable": 4,
        "filter_reason_counts": ["task_list": 1],
    ],
]
let v4Summary = ActivityConfirmationV4Summary(dictionary: v4SummaryDictionary)
check(
    v4Summary?.countsMatch(pendingQuestionCount: 2) == true
        && v4Summary?.priorityCounts.high == 1
        && v4Summary?.pendingByKind.businessContext == 1
        && v4Summary?.candidateFunnel.filterReasonCounts["task_list"] == 1,
    "contract v4 must parse priority, question-kind, and content-free funnel diagnostics"
)
check(
    v4Summary?.countsMatch(pendingQuestionCount: 3) == false,
    "contract v4 count disagreements must be detectable for fail-closed parsing"
)
var malformedV4Summary = v4SummaryDictionary
malformedV4Summary["priority_counts"] = ["high": true, "normal": 1, "low": 0]
check(
    ActivityConfirmationV4Summary(dictionary: malformedV4Summary) == nil,
    "a Boolean masquerading as a v4 count must fail closed"
)

let explicitClaimWithoutEpisode: [String: Any] = [
    "question_kind": "occurrence",
    "candidate_origin": "explicit_claim",
    "priority": "normal",
    "episode_id": "",
    "safe_signal_types": ["cleaned_text"],
    "suggested_workstream": "",
    "suggested_outcome": "",
    "user_workstream": "",
    "user_outcome": "",
    "notification_eligible": false,
]
check(
    ActivityConfirmationGroupV4Fields(dictionary: explicitClaimWithoutEpisode) != nil,
    "v4 explicit claims may safely omit episode_id when they are not notification eligible"
)
var businessEpisodeWithoutID = explicitClaimWithoutEpisode
businessEpisodeWithoutID["candidate_origin"] = "business_episode"
check(
    ActivityConfirmationGroupV4Fields(dictionary: businessEpisodeWithoutID) == nil,
    "a business episode must have an episode_id"
)
var notificationEligibleExplicitClaim = explicitClaimWithoutEpisode
notificationEligibleExplicitClaim["notification_eligible"] = true
check(
    ActivityConfirmationGroupV4Fields(dictionary: notificationEligibleExplicitClaim) != nil,
    "notification_eligible must not make episode_id mandatory for an explicit claim"
)
var notificationEligibleExplicitClaimWithoutField = notificationEligibleExplicitClaim
notificationEligibleExplicitClaimWithoutField.removeValue(forKey: "episode_id")
check(
    ActivityConfirmationGroupV4Fields(dictionary: notificationEligibleExplicitClaimWithoutField) != nil,
    "only business_episode may require the episode_id field"
)

var realV4ExplicitGroup = notificationEligibleExplicitClaim
realV4ExplicitGroup.merge([
    "group_key": "grp_explicit_without_episode",
    "member_revision": "rev_explicit_1",
    "priority": "high",
    "event_type": "activity_claim",
    "state": "pending",
    "answer_options": ["occurred", "not_occurred", "ignored", "defer"],
    "subject": ["label": "一项明确活动"],
    "subject_label": "一项明确活动",
    "trigger_sentence": "已完成一项明确活动",
    "decision_question": "这项活动真的发生了吗？",
    "why_uncertain": "需要本人确认",
    "app": "ChatGPT",
    "time_label": "今天 10:00",
    "observed_start": "2026-07-21T10:00:00+08:00",
    "observed_end": "2026-07-21T10:01:00+08:00",
    "confidence": 0.8,
    "reason_codes": ["explicit_claim"],
    "source_count": 1,
    "evidence_status": "unavailable",
    "evidence_count": 999,
    "available_source_count": 0,
    "cleaned_contents": [["text": "已完成一项明确活动", "count": 1]],
], uniquingKeysWith: { _, new in new })

func realV4Envelope(
    pendingCount: Int = 1,
    pendingSourceCount: Int = 1,
    groups: [[String: Any]] = [realV4ExplicitGroup]
) -> [String: Any] {
    [
        "status": "ok",
        "interface": "activity-confirmation",
        "contract_version": 4,
        "raw_access": false,
        "destructive_actions": false,
        "memory_write_actions": false,
        "range": "7d",
        "time_range": [
            "from": "2026-07-15T00:00:00+08:00",
            "to": "2026-07-22T00:00:00+08:00",
        ],
        "pending_count": pendingSourceCount,
        "resolved_count": 0,
        "pending_question_count": pendingCount,
        "pending_source_count": pendingSourceCount,
        "mode": "standard",
        "priority_counts": ["high": pendingCount, "normal": 0, "low": 0],
        "pending_by_kind": ["occurrence": pendingCount, "business_context": 0, "outcome": 0],
        "candidate_funnel": [
            "clear_activities": max(1, pendingCount),
            "event_signals": max(1, pendingCount),
            "eligible_questions": pendingCount,
            "hard_filtered": 0,
            "pending": pendingCount,
            "confirmed": 0,
            "reportable": 0,
            "filter_reason_counts": [:] as [String: Int],
        ],
        "resolved_question_count": 0,
        "question_groups": groups,
        "confirmation_evidence_health": [
            "status": "ok",
            "available_count": pendingCount,
            "unavailable_count": 0,
            "delete_pending_count": 0,
            "error_count": 0,
            "raw_access": false,
        ],
        "items": [],
        "calibrations": [],
        "summary": pendingCount == 0
            ? "当前没有待确认的活动事实。"
            : "有 \(pendingCount) 个问题等待确认（合并 \(pendingSourceCount) 条线索）。",
    ]
}

let readableExplicitPayload = ActivityConfirmationV4PayloadState(
    jsonText: jsonText(realV4Envelope())
)
check(
    readableExplicitPayload.disposition == .readable
        && readableExplicitPayload.navigationState.verifiedPendingCount == 1
        && readableExplicitPayload.navigationState.badgeText == "1"
        && readableExplicitPayload.navigationState.badgeTone == .standard,
    "a full producer-shaped v4 payload must keep a notification-eligible explicit claim without an episode readable"
)
check(
    readableExplicitPayload.navigationState.pageHeaderText(pendingSourceCount: 1) == "1 个问题 · 1 条安全线索",
    "the readable page header must use the same verified navigation count as Today and the sidebar"
)
check(
    !ActivityConfirmationNotificationPolicy.isHighValueClosedEpisode(
        state: "pending",
        priority: .high,
        notificationEligible: true,
        episodeID: "",
        observedEnd: "2026-07-21T10:01:00+08:00",
        now: ISO8601DateFormatter().date(from: "2026-07-21T10:10:00+08:00")!
    ),
    "making an explicit claim readable must not widen immediate notifications without a stable episode"
)

var realBusinessEpisodeWithoutID = realV4ExplicitGroup
realBusinessEpisodeWithoutID["candidate_origin"] = "business_episode"
realBusinessEpisodeWithoutID.removeValue(forKey: "episode_id")
let blockedKnownBusinessPayload = ActivityConfirmationV4PayloadState(
    jsonText: jsonText(realV4Envelope(groups: [realBusinessEpisodeWithoutID]))
)
check(
    blockedKnownBusinessPayload.disposition == .blocked
        && blockedKnownBusinessPayload.navigationState.verifiedPendingCount == 1
        && blockedKnownBusinessPayload.navigationState.badgeText == "1"
        && blockedKnownBusinessPayload.navigationState.badgeTone == .warning
        && !blockedKnownBusinessPayload.navigationState.canDisplayContent,
    "a business episode without an id must block cards and actions while preserving its independently verified count"
)
check(
    blockedKnownBusinessPayload.navigationState.pageHeaderText(pendingSourceCount: 1)
        == "有 1 个问题，但内容暂不可读"
        && blockedKnownBusinessPayload.navigationState.todayPromptTitle
        == "有 1 个问题，但内容暂不可读"
        && blockedKnownBusinessPayload.navigationState.accessibilityLabel(tabTitle: "待确认")
        == "待确认，1 条待确认，内容暂不可读",
    "blocked-known count copy must stay identical across the page, Today, sidebar, and VoiceOver state"
)

var invalidChoicesEnvelope = realV4Envelope()
var invalidChoicesGroup = realV4ExplicitGroup
invalidChoicesGroup["answer_options"] = ["occurred", "not_occurred", "defer"]
invalidChoicesEnvelope["question_groups"] = [invalidChoicesGroup]
let invalidChoicesPayload = ActivityConfirmationV4PayloadState(
    jsonText: jsonText(invalidChoicesEnvelope)
)
check(
    invalidChoicesPayload.disposition == .blocked
        && invalidChoicesPayload.navigationState.verifiedPendingCount == 1
        && invalidChoicesPayload.navigationState.badgeTone == .warning,
    "invalid answer choices must fail closed without discarding an independently verified count"
)

var rawAccessEnvelope = realV4Envelope()
rawAccessEnvelope["raw_access"] = true
let rawAccessPayload = ActivityConfirmationV4PayloadState(jsonText: jsonText(rawAccessEnvelope))
check(
    rawAccessPayload.disposition == .blocked
        && rawAccessPayload.navigationState.pendingCountState == .unverified
        && rawAccessPayload.navigationState.badgeText == "!"
        && rawAccessPayload.navigationState.badgeTone == .warning
        && rawAccessPayload.navigationState.accessibilityLabel(tabTitle: "待确认")
        == "待确认，待确认数量暂不可验证",
    "raw_access=true must block content and make the pending count unverifiable"
)

var funnelMismatchEnvelope = realV4Envelope()
var mismatchedFunnel = funnelMismatchEnvelope["candidate_funnel"] as! [String: Any]
mismatchedFunnel["pending"] = 2
funnelMismatchEnvelope["candidate_funnel"] = mismatchedFunnel
let funnelMismatchPayload = ActivityConfirmationV4PayloadState(
    jsonText: jsonText(funnelMismatchEnvelope)
)
check(
    funnelMismatchPayload.disposition == .blocked
        && funnelMismatchPayload.navigationState.pendingCountState == .unverified
        && funnelMismatchPayload.navigationState.badgeText == "!"
        && funnelMismatchPayload.navigationState.pageHeaderText(pendingSourceCount: 1)
        == "待确认数量暂不可验证",
    "a funnel mismatch must block content and replace the untrusted count with an exclamation mark"
)

let zeroV4Payload = ActivityConfirmationV4PayloadState(
    jsonText: jsonText(realV4Envelope(pendingCount: 0, pendingSourceCount: 0, groups: []))
)
check(
    zeroV4Payload.disposition == .readable
        && zeroV4Payload.navigationState.verifiedPendingCount == 0
        && zeroV4Payload.navigationState.badgeText == nil
        && zeroV4Payload.navigationState.badgeTone == .none
        && zeroV4Payload.navigationState.pageHeaderText(pendingSourceCount: 0)
        == "没有需要你处理的问题",
    "a healthy zero-count v4 payload must not show a badge"
)

let cappedV4Payload = ActivityConfirmationV4PayloadState(
    jsonText: jsonText(realV4Envelope(pendingCount: 100, pendingSourceCount: 120))
)
check(
    cappedV4Payload.disposition == .readable
        && cappedV4Payload.navigationState.verifiedPendingCount == 100
        && cappedV4Payload.navigationState.badgeText == "99+",
    "a limited v4 card list must keep a verified 100-question count and cap only its visual badge"
)

var evidenceChangedEnvelope = realV4Envelope()
evidenceChangedEnvelope["confirmation_evidence_health"] = [
    "status": "attention",
    "available_count": 0,
    "unavailable_count": 2,
    "delete_pending_count": 0,
    "error_count": 2,
    "raw_access": false,
]
var evidenceChangedGroup = realV4ExplicitGroup
evidenceChangedGroup["evidence_status"] = "partial"
evidenceChangedGroup["evidence_count"] = 2
evidenceChangedGroup["available_source_count"] = 1
evidenceChangedEnvelope["question_groups"] = [evidenceChangedGroup]
let evidenceChangedPayload = ActivityConfirmationV4PayloadState(
    jsonText: jsonText(evidenceChangedEnvelope)
)
check(
    evidenceChangedPayload.navigationState == readableExplicitPayload.navigationState,
    "evidence availability metadata must not drive the navigation badge"
)

let v3Navigation = ActivityConfirmationNavigationState.compatibleResponse(
    status: "ok",
    pendingCount: 3
)
let blockedV3Navigation = ActivityConfirmationNavigationState.compatibleResponse(
    status: "blocked",
    pendingCount: 3
)
let malformedLegacyNavigation = ActivityConfirmationNavigationState.compatibleResponse(
    status: "ok",
    pendingCount: -1
)
check(
    ActivityConfirmationV4PayloadState(jsonText: jsonText([
        "status": "ok",
        "interface": "activity-confirmation",
        "contract_version": 3,
    ])).disposition == .notV4
        && v3Navigation.badgeText == "3"
        && v3Navigation.badgeTone == .standard
        && v3Navigation.canDisplayContent,
    "v3 remains on the compatible readable navigation path"
)
check(
    blockedV3Navigation == .unavailable
        && blockedV3Navigation.badgeText == "!"
        && !blockedV3Navigation.canDisplayContent
        && malformedLegacyNavigation == .unavailable,
    "blocked or invalid-count v3 and legacy responses must fail closed instead of showing a normal badge"
)
var unknownOrigin = explicitClaimWithoutEpisode
unknownOrigin["candidate_origin"] = "free_text_origin"
check(
    ActivityConfirmationGroupV4Fields(dictionary: unknownOrigin) == nil,
    "candidate_origin must use the v4 allowlist"
)
var unknownOutcome = explicitClaimWithoutEpisode
unknownOutcome["suggested_outcome"] = "made_up_result"
var supersededOutcome = explicitClaimWithoutEpisode
supersededOutcome["suggested_outcome"] = "progressed"
check(
    ActivityConfirmationGroupV4Fields(dictionary: unknownOutcome) == nil
        && ActivityConfirmationGroupV4Fields(dictionary: supersededOutcome) == nil,
    "suggested and user outcomes must use the closed business outcome taxonomy"
)
check(
    ActivityConfirmationBusinessOutcome.allCases.map(\.rawValue) == [
        "completed", "progressing", "blocked", "preparation_only", "unknown",
    ]
        && ActivityConfirmationBusinessOutcome.allCases.map(\.label) == [
            "已完成", "推进中", "受阻", "仅准备", "结果未知",
        ]
        && ActivityConfirmationCommandBuilder.boundedOutcome("progressed").isEmpty
        && ActivityConfirmationCommandBuilder.boundedOutcome("preparation").isEmpty,
    "v4 public outcomes must use only the approved taxonomy and reject the two superseded values"
)

var validPendingV4Group = explicitClaimWithoutEpisode
validPendingV4Group["group_key"] = "grp_safe_1"
validPendingV4Group["member_revision"] = "rev_safe_1"
validPendingV4Group["state"] = "pending"
validPendingV4Group["answer_options"] = ["occurred", "not_occurred", "ignored", "defer"]
check(
    ActivityConfirmationV4ListContract.responseIsSafe(
        pendingQuestionCount: 9,
        questionGroups: [validPendingV4Group]
    ),
    "a limited v4 list may expose one safe actionable card even when the total pending count is larger"
)
check(
    !ActivityConfirmationV4ListContract.responseIsSafe(
        pendingQuestionCount: 1,
        questionGroups: []
    ),
    "a positive v4 pending count must never render without at least one safe actionable card"
)
var invalidPendingGroupKey = validPendingV4Group
invalidPendingGroupKey["group_key"] = "unsafe group key"
var invalidPendingRevision = validPendingV4Group
invalidPendingRevision["member_revision"] = 42
var invalidPendingState = validPendingV4Group
invalidPendingState["state"] = "confirmed"
var invalidPendingAnswerOptions = validPendingV4Group
invalidPendingAnswerOptions["answer_options"] = ["occurred", "not_occurred", "defer"]
check(
    !ActivityConfirmationV4ListContract.isSafeVisiblePendingGroup(invalidPendingGroupKey)
        && !ActivityConfirmationV4ListContract.isSafeVisiblePendingGroup(invalidPendingRevision)
        && !ActivityConfirmationV4ListContract.isSafeVisiblePendingGroup(invalidPendingState)
        && !ActivityConfirmationV4ListContract.isSafeVisiblePendingGroup(invalidPendingAnswerOptions),
    "v4 cards must reject unstable keys, non-string revisions, resolved states, and incomplete answer capabilities"
)

var safeV4ConfigureResponse: [String: Any] = [
    "status": "configured",
    "interface": "activity-confirmation",
    "contract_version": 4,
    "mode": "standard",
    "raw_access": false,
    "destructive_actions": false,
    "memory_write_actions": false,
]
check(
    ActivityConfirmationMutationResponseValidator.configureResponseIsSafe(
        safeV4ConfigureResponse,
        expectedMode: .standard
    ),
    "configure must accept an explicit matching v4 activity-confirmation envelope"
)
var wrongConfigureInterface = safeV4ConfigureResponse
wrongConfigureInterface["interface"] = "other-interface"
var wrongConfigureContract = safeV4ConfigureResponse
wrongConfigureContract["contract_version"] = 3
var wrongConfigureMode = safeV4ConfigureResponse
wrongConfigureMode["mode"] = "compact"
var missingConfigureInterface = safeV4ConfigureResponse
missingConfigureInterface.removeValue(forKey: "interface")
check(
    !ActivityConfirmationMutationResponseValidator.configureResponseIsSafe(
        wrongConfigureInterface,
        expectedMode: .standard
    )
        && !ActivityConfirmationMutationResponseValidator.configureResponseIsSafe(
            wrongConfigureContract,
            expectedMode: .standard
        )
        && !ActivityConfirmationMutationResponseValidator.configureResponseIsSafe(
            wrongConfigureMode,
            expectedMode: .standard
        )
        && !ActivityConfirmationMutationResponseValidator.configureResponseIsSafe(
            missingConfigureInterface,
            expectedMode: .standard
        ),
    "configure must fail closed on an omitted or mismatched interface, contract, or mode"
)

var recordedManualItem = validPendingV4Group
recordedManualItem["item_key"] = "manual_item_1"
recordedManualItem["group_key"] = "grp_manual_1"
recordedManualItem["candidate_origin"] = "manual"
recordedManualItem["event_type"] = "manual"
recordedManualItem["state"] = "confirmed"
recordedManualItem["user_answer"] = "occurred"
recordedManualItem["user_workstream"] = "海外渠道"
recordedManualItem["user_outcome"] = "unknown"
recordedManualItem["suggested_workstream"] = "海外渠道"
recordedManualItem["suggested_outcome"] = "unknown"
var safeManualResponse = safeV4ConfigureResponse
safeManualResponse["status"] = "recorded"
safeManualResponse["item"] = recordedManualItem
check(
    ActivityConfirmationMutationResponseValidator.manualAddResponseIsSafe(
        safeManualResponse,
        expectedWorkstream: "海外渠道",
        expectedOutcome: "unknown"
    ),
    "manual-add must confirm its v4 envelope, recorded item, and stable item/group identifiers"
)
var manualMissingGroupIdentifier = safeManualResponse
var itemWithoutGroupIdentifier = recordedManualItem
itemWithoutGroupIdentifier["group_key"] = ""
manualMissingGroupIdentifier["item"] = itemWithoutGroupIdentifier
var manualWrongOutcome = safeManualResponse
var itemWithWrongOutcome = recordedManualItem
itemWithWrongOutcome["user_outcome"] = "blocked"
manualWrongOutcome["item"] = itemWithWrongOutcome
var manualWrongEnvelope = safeManualResponse
manualWrongEnvelope["contract_version"] = 3
var manualWrongStatus = safeManualResponse
manualWrongStatus["status"] = "ok"
var safeManualPreparationResponse = safeManualResponse
var manualPreparationItem = recordedManualItem
manualPreparationItem["event_type"] = "preparation"
manualPreparationItem["user_outcome"] = "preparation_only"
safeManualPreparationResponse["item"] = manualPreparationItem
var manualPreparationWrongEvent = safeManualPreparationResponse
var manualPreparationInterviewItem = manualPreparationItem
manualPreparationInterviewItem["event_type"] = "interview"
manualPreparationWrongEvent["item"] = manualPreparationInterviewItem
check(
    !ActivityConfirmationMutationResponseValidator.manualAddResponseIsSafe(
        manualMissingGroupIdentifier,
        expectedWorkstream: "海外渠道",
        expectedOutcome: "unknown"
    )
        && !ActivityConfirmationMutationResponseValidator.manualAddResponseIsSafe(
            manualWrongOutcome,
            expectedWorkstream: "海外渠道",
            expectedOutcome: "unknown"
        )
        && !ActivityConfirmationMutationResponseValidator.manualAddResponseIsSafe(
            manualWrongEnvelope,
            expectedWorkstream: "海外渠道",
            expectedOutcome: "unknown"
        )
        && !ActivityConfirmationMutationResponseValidator.manualAddResponseIsSafe(
            manualWrongStatus,
            expectedWorkstream: "海外渠道",
            expectedOutcome: "unknown"
        ),
    "manual-add must reject missing stable identifiers and a Core response that records different business fields"
)
check(
    ActivityConfirmationMutationResponseValidator.manualAddResponseIsSafe(
        safeManualPreparationResponse,
        expectedWorkstream: "海外渠道",
        expectedOutcome: "preparation_only"
    )
        && !ActivityConfirmationMutationResponseValidator.manualAddResponseIsSafe(
            manualPreparationWrongEvent,
            expectedWorkstream: "海外渠道",
            expectedOutcome: "preparation_only"
        ),
    "manual preparation must be accepted only as an explicit preparation event"
)

var recordedAnswerGroup = validPendingV4Group
recordedAnswerGroup["member_revision"] = "rev_after_1"
recordedAnswerGroup["state"] = "confirmed"
recordedAnswerGroup["user_answer"] = "occurred"
recordedAnswerGroup["user_workstream"] = "海外渠道"
recordedAnswerGroup["user_outcome"] = "progressing"
recordedAnswerGroup["suggested_outcome"] = "unknown"
recordedAnswerGroup["notification_eligible"] = false
let safeRecordedAnswerResponse: [String: Any] = [
    "status": "recorded",
    "group_key": "grp_safe_1",
    "member_revision": "rev_after_1",
    "question_group": recordedAnswerGroup,
    "raw_access": false,
    "destructive_actions": false,
    "memory_write_actions": false,
]
check(
    ActivityConfirmationMutationResponseValidator.v4GroupAnswerResult(
        safeRecordedAnswerResponse,
        expectedGroupKey: "grp_safe_1",
        expectedAnswer: .occurred,
        expectedWorkstream: "海外渠道",
        expectedOutcome: "progressing"
    ) == .recorded,
    "v4 occurred must verify the exact group, answer, workstream, and allowlisted outcome returned by Core"
)
var recordedNotOccurredGroup = validPendingV4Group
recordedNotOccurredGroup["member_revision"] = "rev_after_denial_1"
recordedNotOccurredGroup["state"] = "rejected"
recordedNotOccurredGroup["user_answer"] = "not_occurred"
recordedNotOccurredGroup["user_workstream"] = "海外求职"
recordedNotOccurredGroup["user_outcome"] = "unknown"
recordedNotOccurredGroup["notification_eligible"] = false
let safeRecordedNotOccurredResponse: [String: Any] = [
    "status": "recorded",
    "group_key": "grp_safe_1",
    "member_revision": "rev_after_denial_1",
    "question_group": recordedNotOccurredGroup,
    "raw_access": false,
    "destructive_actions": false,
    "memory_write_actions": false,
]
check(
    ActivityConfirmationMutationResponseValidator.v4GroupAnswerResult(
        safeRecordedNotOccurredResponse,
        expectedGroupKey: "grp_safe_1",
        expectedAnswer: .notOccurred,
        expectedWorkstream: "海外求职",
        expectedOutcome: "unknown"
    ) == .recorded,
    "v4 not-occurred must verify business attribution and the explicit unknown outcome recorded by Core"
)
var recordedPreparationGroup = recordedNotOccurredGroup
recordedPreparationGroup["member_revision"] = "rev_after_preparation_1"
recordedPreparationGroup["user_workstream"] = "富友求职"
recordedPreparationGroup["user_outcome"] = "preparation_only"
let safeRecordedPreparationResponse: [String: Any] = [
    "status": "recorded",
    "group_key": "grp_safe_1",
    "member_revision": "rev_after_preparation_1",
    "question_group": recordedPreparationGroup,
    "raw_access": false,
    "destructive_actions": false,
    "memory_write_actions": false,
]
check(
    ActivityConfirmationMutationResponseValidator.v4GroupAnswerResult(
        safeRecordedPreparationResponse,
        expectedGroupKey: "grp_safe_1",
        expectedAnswer: .notOccurred,
        expectedWorkstream: "富友求职",
        expectedOutcome: "preparation_only"
    ) == .recorded,
    "v4 not-occurred may record a source-bound preparation classification without releasing the occurrence"
)
var wrongRecordedAnswerResponse = safeRecordedAnswerResponse
var wrongRecordedAnswerGroup = recordedAnswerGroup
wrongRecordedAnswerGroup["user_outcome"] = "completed"
wrongRecordedAnswerResponse["question_group"] = wrongRecordedAnswerGroup
var wrongRecordedGroupKey = safeRecordedAnswerResponse
wrongRecordedGroupKey["group_key"] = "grp_different"
var wrongRecordedState = safeRecordedAnswerResponse
var wrongRecordedStateGroup = recordedAnswerGroup
wrongRecordedStateGroup["state"] = "deferred"
wrongRecordedState["question_group"] = wrongRecordedStateGroup
check(
    ActivityConfirmationMutationResponseValidator.v4GroupAnswerResult(
        wrongRecordedAnswerResponse,
        expectedGroupKey: "grp_safe_1",
        expectedAnswer: .occurred,
        expectedWorkstream: "海外渠道",
        expectedOutcome: "progressing"
    ) == .unsafe
        && ActivityConfirmationMutationResponseValidator.v4GroupAnswerResult(
            wrongRecordedGroupKey,
            expectedGroupKey: "grp_safe_1",
            expectedAnswer: .occurred,
            expectedWorkstream: "海外渠道",
            expectedOutcome: "progressing"
        ) == .unsafe
        && ActivityConfirmationMutationResponseValidator.v4GroupAnswerResult(
            wrongRecordedState,
            expectedGroupKey: "grp_safe_1",
            expectedAnswer: .occurred,
            expectedWorkstream: "海外渠道",
            expectedOutcome: "progressing"
        ) == .unsafe,
    "a recorded response with different business fields must fail closed"
)
check(
    ActivityConfirmationMutationResponseValidator.v4GroupAnswerResult(
        [
            "status": "stale",
            "raw_access": false,
            "destructive_actions": false,
            "memory_write_actions": false,
        ],
        expectedGroupKey: "grp_safe_1",
        expectedAnswer: .occurred,
        expectedWorkstream: "海外渠道",
        expectedOutcome: "progressing"
    ) == .stale,
    "a safe stale response must remain a non-success refresh path"
)

let notificationFormatter = ISO8601DateFormatter()
let episodeEnd = "2026-07-20T10:00:00Z"
let beforeQuietPeriod = notificationFormatter.date(from: "2026-07-20T10:04:59Z")!
let afterQuietPeriod = notificationFormatter.date(from: "2026-07-20T10:05:00Z")!
check(
    !ActivityConfirmationNotificationPolicy.isHighValueClosedEpisode(
        state: "pending",
        priority: .high,
        notificationEligible: true,
        episodeID: "episode-1",
        observedEnd: episodeEnd,
        now: beforeQuietPeriod
    ),
    "a high-value episode must stay silent while it is active or inside the five-minute quiet period"
)
check(
    ActivityConfirmationNotificationPolicy.isHighValueClosedEpisode(
        state: "pending",
        priority: .high,
        notificationEligible: true,
        episodeID: "episode-1",
        observedEnd: episodeEnd,
        now: afterQuietPeriod
    ),
    "a high-value episode may notify only after its end plus five quiet minutes"
)
check(
    !ActivityConfirmationNotificationPolicy.isHighValueClosedEpisode(
        state: "pending",
        priority: .high,
        notificationEligible: true,
        episodeID: "episode-1",
        observedEnd: "invalid",
        now: afterQuietPeriod
    ),
    "an invalid episode end time must fail closed without notification"
)
check(
    !ActivityConfirmationNotificationPolicy.isHighValueClosedEpisode(
        state: "pending",
        priority: .high,
        notificationEligible: true,
        episodeID: "unstable episode id",
        observedEnd: episodeEnd,
        now: afterQuietPeriod
    ),
    "an immediate notification must require a stable allowlisted episode identifier"
)
check(
    ActivityConfirmationNotificationPolicy.shouldScheduleBatch(pendingQuestionCount: 1)
        && !ActivityConfirmationNotificationPolicy.shouldScheduleBatch(pendingQuestionCount: 0),
    "the 22:30 batch reminder must exist only while pending questions remain"
)
check(
    ActivityConfirmationNotificationPolicy.highValueBody == "有一项高优先活动需要你在本机确认。"
        && ActivityConfirmationNotificationPolicy.batchBody == "今天还有活动需要你在本机确认。"
        && !ActivityConfirmationNotificationPolicy.highValueBody.contains("浩方")
        && !ActivityConfirmationNotificationPolicy.batchBody.contains("客户"),
    "local notification copy must remain content-free and never interpolate company, question, or outcome text"
)
check(
    ActivityConfirmationSafetyBoundary.permits(
        rawAccess: false,
        destructiveActions: false,
        memoryWriteActions: false
    ),
    "activity-confirmation responses must pass only when every safety flag is the JSON boolean false"
)
check(
    !ActivityConfirmationSafetyBoundary.permits(
        rawAccess: 0,
        destructiveActions: false,
        memoryWriteActions: false
    ),
    "numeric zero must not be accepted as raw_access=false"
)
check(
    !ActivityConfirmationSafetyBoundary.permits(
        rawAccess: false,
        destructiveActions: nil,
        memoryWriteActions: false
    ),
    "missing safety flags must fail closed"
)
check(
    !ActivityConfirmationSafetyBoundary.permits(
        rawAccess: false,
        destructiveActions: false,
        memoryWriteActions: true
    ),
    "any enabled safety-sensitive action must fail closed"
)
check(
    ActivityConfirmationSafetyBoundary.nonnegativeInteger(0) == 0
        && ActivityConfirmationSafetyBoundary.nonnegativeInteger(12) == 12
        && ActivityConfirmationSafetyBoundary.nonnegativeInteger(true) == nil
        && ActivityConfirmationSafetyBoundary.nonnegativeInteger(-1) == nil,
    "v4 top-level counts must accept only nonnegative JSON integers, never booleans"
)

let groupedCommand = ActivityConfirmationCommandBuilder.answerCommand(
    target: .questionGroup(groupKey: "group-1", memberRevision: "revision-8"),
    request: ActivityConfirmationAnswerRequest(answer: .notOccurred, selectedReason: .futureNotOccurred),
    note: "未来安排"
)
check(
    groupedCommand == ActivityConfirmationCoreCommand(arguments: [
        "activity-confirmation", "answer-group",
        "--group-key", "group-1",
        "--member-revision", "revision-8",
        "--answer", "not_occurred",
        "--generalize", "similar",
        "--reason", "future_not_occurred",
        "--note-stdin",
    ], standardInput: "未来安排"),
    "group answers must bind the revision and carry private notes over stdin"
)
let ignoredCommand = ActivityConfirmationCommandBuilder.answerCommand(
    target: .questionGroup(groupKey: "group-1", memberRevision: "revision-8"),
    request: ignoredActivity,
    note: "不应被保存的说明"
)
check(
    ignoredCommand == ActivityConfirmationCoreCommand(arguments: [
        "activity-confirmation", "answer-group",
        "--group-key", "group-1",
        "--member-revision", "revision-8",
        "--answer", "ignored",
        "--generalize", "exact",
        "--reason", "user_ignored",
    ]),
    "ignored must submit a neutral v3 answer without correction calibration or note input"
)
let legacyCommand = ActivityConfirmationCommandBuilder.answerCommand(
    target: .legacyItem(itemKey: "item-1"),
    request: confirmedActivity
)
check(
    legacyCommand == ActivityConfirmationCoreCommand(arguments: [
        "activity-confirmation", "answer",
        "--item-key", "item-1",
        "--answer", "occurred",
        "--generalize", "exact",
        "--reason", "correct",
    ]),
    "legacy fallback must keep using the v1 single-item answer command"
)
let structuredCommand = ActivityConfirmationCommandBuilder.answerCommand(
    target: .questionGroup(groupKey: "group-v4", memberRevision: "revision-v4"),
    request: confirmedActivity,
    note: "本人刚完成",
    workstream: "海外渠道",
    outcome: "progressing",
    structuredDetailsSupported: true
)
let structuredPayload = structuredCommand?.standardInput
    .flatMap { $0.data(using: .utf8) }
    .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
check(
    structuredCommand?.arguments == [
        "activity-confirmation", "answer-group",
        "--group-key", "group-v4",
        "--member-revision", "revision-v4",
        "--answer", "occurred",
        "--generalize", "exact",
        "--reason", "correct",
        "--details-stdin",
    ]
        && structuredPayload?["note"] == "本人刚完成"
        && structuredPayload?["workstream"] == "海外渠道"
        && structuredPayload?["outcome"] == "progressing"
        && !(structuredCommand?.arguments.contains("海外渠道") ?? true),
    "v4 occurrence details must use bounded JSON stdin and never enter argv"
)
let v5DefaultWorkstream = ActivityConfirmationV5BusinessDefaults.workstream(
    object: "Solo Staging 表单",
    label: "Solo Staging 表单"
)
let v5DefaultOutcome = ActivityConfirmationV5BusinessDefaults.outcome(eventType: "publish")
let v5DefaultAnswerCommand = ActivityConfirmationCommandBuilder.answerCommand(
    target: .questionGroup(groupKey: "group-v5", memberRevision: "revision-v5"),
    request: confirmedActivity,
    workstream: v5DefaultWorkstream,
    outcome: v5DefaultOutcome,
    structuredDetailsSupported: true
)
let v5DefaultAnswerPayload = v5DefaultAnswerCommand?.standardInput
    .flatMap { $0.data(using: .utf8) }
    .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
check(
    v5DefaultWorkstream == "Solo Staging 表单"
        && v5DefaultOutcome == "completed"
        && ActivityConfirmationV5BusinessDefaults.outcome(eventType: "interview") == "progressing"
        && v5DefaultAnswerPayload?["workstream"] == "Solo Staging 表单"
        && v5DefaultAnswerPayload?["outcome"] == "completed",
    "V5 identified cards must derive safe business defaults and submit without forcing manual expansion"
)
let oversizedAnswerCommand = ActivityConfirmationCommandBuilder.answerCommand(
    target: .questionGroup(groupKey: "group-v4", memberRevision: "revision-v4"),
    request: confirmedActivity,
    note: String(repeating: "答", count: 241),
    workstream: "海外渠道",
    outcome: "unknown",
    structuredDetailsSupported: true
)
let oversizedAnswerPayload = oversizedAnswerCommand?.standardInput
    .flatMap { $0.data(using: .utf8) }
    .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
check(
    oversizedAnswerPayload?["note"]?.count == 240,
    "answer notes must match the Core 240-character storage boundary"
)
check(
    ActivityConfirmationCommandBuilder.answerCommand(
        target: .questionGroup(groupKey: "group-v4", memberRevision: "revision-v4"),
        request: confirmedActivity,
        workstream: "",
        outcome: "unknown",
        structuredDetailsSupported: true
    ) == nil
        && ActivityConfirmationCommandBuilder.answerCommand(
            target: .questionGroup(groupKey: "group-v4", memberRevision: "revision-v4"),
            request: confirmedActivity,
            workstream: "海外渠道",
            outcome: "",
            structuredDetailsSupported: true
        ) == nil
        && ActivityConfirmationCommandBuilder.answerCommand(
            target: .questionGroup(groupKey: "group-v4", memberRevision: "revision-v4"),
            request: confirmedActivity,
            workstream: "海外渠道",
            outcome: "progressed",
            structuredDetailsSupported: true
        ) == nil
        && ActivityConfirmationCommandBuilder.answerCommand(
            target: .questionGroup(groupKey: "group-v4", memberRevision: "revision-v4"),
            request: confirmedActivity,
            workstream: "海外渠道",
            outcome: "preparation_only",
            structuredDetailsSupported: true
        ) == nil
        && ActivityConfirmationCommandBuilder.answerCommand(
            target: .questionGroup(groupKey: "group-v4", memberRevision: "revision-v4"),
            request: confirmedActivity,
            workstream: "海外渠道",
            outcome: "unknown",
            structuredDetailsSupported: true
        ) != nil,
    "v4 occurred must atomically require a nonempty workstream and a non-preparation outcome"
)
let structuredRejection = ActivityConfirmationCommandBuilder.answerCommand(
    target: .questionGroup(groupKey: "group-v4", memberRevision: "revision-v4"),
    request: oldActivity,
    note: "只是旧资料",
    workstream: "海外求职",
    outcome: "unknown",
    structuredDetailsSupported: true
)
let structuredRejectionPayload = structuredRejection?.standardInput
    .flatMap { $0.data(using: .utf8) }
    .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
check(
    structuredRejectionPayload?["note"] == "只是旧资料"
        && structuredRejectionPayload?["workstream"] == "海外求职"
        && structuredRejectionPayload?["outcome"] == "unknown",
    "v4 non-occurrence answers must preserve the three-level business classification"
)
let structuredPreparation = ActivityConfirmationCommandBuilder.answerCommand(
    target: .questionGroup(groupKey: "group-v4", memberRevision: "revision-v4"),
    request: preparationActivity,
    workstream: "富友求职",
    outcome: "preparation_only",
    structuredDetailsSupported: true
)
let structuredPreparationPayload = structuredPreparation?.standardInput
    .flatMap { $0.data(using: .utf8) }
    .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
check(
    structuredPreparationPayload?["workstream"] == "富友求职"
        && structuredPreparationPayload?["outcome"] == "preparation_only"
        && ActivityConfirmationCommandBuilder.answerCommand(
            target: .questionGroup(groupKey: "group-v4", memberRevision: "revision-v4"),
            request: preparationActivity,
            workstream: "富友求职",
            outcome: "unknown",
            structuredDetailsSupported: true
        ) == nil,
    "preparation-only must be submitted as not-occurred with matching reason, workstream, and outcome"
)
let manualAddCommand = ActivityConfirmationCommandBuilder.manualAddCommand(
    safeLabel: "完成客户方案复盘",
    workstream: "海外渠道",
    outcome: "unknown",
    startedAt: "2026-07-20T10:00:00Z",
    endedAt: "2026-07-20T10:30:00Z"
)
let manualPayload = manualAddCommand?.standardInput
    .flatMap { $0.data(using: .utf8) }
    .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
check(
    manualAddCommand?.arguments == [
        "activity-confirmation", "manual-add",
        "--confirm", "MANUAL_CONFIRMED_ACTIVITY",
        "--payload-stdin",
    ]
        && manualPayload?["safe_label"] == "完成客户方案复盘"
        && manualPayload?["workstream"] == "海外渠道"
        && manualPayload?["outcome"] == "unknown"
        && !(manualAddCommand?.arguments.contains("完成客户方案复盘") ?? true),
    "manual-add must require explicit confirmation and keep all user content in JSON stdin"
)
let oversizedManualCommand = ActivityConfirmationCommandBuilder.manualAddCommand(
    safeLabel: String(repeating: "补", count: 181),
    workstream: "海外渠道",
    outcome: "unknown",
    startedAt: "2026-07-20T10:00:00Z",
    endedAt: "2026-07-20T10:30:00Z"
)
let oversizedManualPayload = oversizedManualCommand?.standardInput
    .flatMap { $0.data(using: .utf8) }
    .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
check(
    oversizedManualPayload?["safe_label"]?.count == 180,
    "manual labels must match the Core 180-character storage boundary"
)
check(
    ActivityConfirmationCommandBuilder.manualAddCommand(
        safeLabel: "活动",
        workstream: "业务",
        outcome: "completed",
        startedAt: "2026-07-20T11:00:00Z",
        endedAt: "2026-07-20T10:00:00Z"
    ) == nil,
    "manual-add must reject an inverted time range"
)
check(
    ActivityConfirmationCommandBuilder.configureCommand(mode: .compact).arguments == [
        "activity-confirmation", "configure", "--mode", "compact",
    ]
        && ActivityConfirmationModePreset.allCases.map(\.title) == ["精简 5–10", "标准 10–20", "最大召回"],
    "the three recall presets must map to the locked Core mode values"
)
check(
    ActivityConfirmationCommandBuilder.answerCommand(
        target: .legacyItem(itemKey: "item-1"),
        request: ignoredActivity
    ) == nil,
    "ignored must fail closed against a legacy Core"
)
check(
    ActivityConfirmationCommandBuilder.answerCommand(
        target: .legacyItem(itemKey: "item-1"),
        request: ActivityConfirmationAnswerRequest(answer: .notOccurred, selectedReason: .futureNotOccurred)
    ) == nil,
    "legacy command generation must defensively reject the v2-only future-plan reason"
)
check(
    ActivityConfirmationCommandBuilder.clarifyCommand(groupKey: "group-1", memberRevision: "revision-8") == ActivityConfirmationCoreCommand(arguments: [
        "activity-confirmation", "clarify",
        "--group-key", "group-1",
        "--member-revision", "revision-8",
    ]),
    "clarify must be bound to the exact visible group revision"
)
check(
    ActivityConfirmationCommandBuilder.clarifyCommand(
        groupKey: "group-1",
        memberRevision: "revision-8",
        note: "  实际是浩方二面  \n"
    ) == ActivityConfirmationCoreCommand(arguments: [
        "activity-confirmation", "clarify",
        "--group-key", "group-1",
        "--member-revision", "revision-8",
        "--note-stdin",
    ], standardInput: "实际是浩方二面"),
    "clarify must trim and carry the optional local note without exposing it in argv"
)
check(
    ActivityConfirmationCommandBuilder.clarifyCommand(
        groupKey: "group-1",
        memberRevision: "revision-8",
        note: " \n\t "
    ) == ActivityConfirmationCoreCommand(arguments: [
        "activity-confirmation", "clarify",
        "--group-key", "group-1",
        "--member-revision", "revision-8",
    ]),
    "clarify must omit a blank note"
)
let oversizedClarificationNote = String(repeating: "补", count: 361)
let boundedClarificationCommand = ActivityConfirmationCommandBuilder.clarifyCommand(
    groupKey: "group-1",
    memberRevision: "revision-8",
    note: oversizedClarificationNote
)
check(
    boundedClarificationCommand?.standardInput?.count == 360
        && boundedClarificationCommand?.arguments.last == "--note-stdin"
        && !(boundedClarificationCommand?.arguments.contains(String(repeating: "补", count: 360)) ?? true),
    "clarify must cap its private stdin note at 360 characters without placing it in argv"
)
check(
    ActivityConfirmationCommandBuilder.clarifyCommand(groupKey: "group-1", memberRevision: "") == nil,
    "a v2 card without a member revision must fail closed"
)
check(
    ActivityConfirmationMessagePolicy.isWarning("这组线索发生了变化，未提交任何部分结果；已刷新。"),
    "a stale answer must render as a warning instead of a green success banner"
)
check(
    ActivityConfirmationMessagePolicy.isWarning("已使用你的补充说明重新梳理，但仍没有足够信息。"),
    "an insufficient clarification must stay visibly styled as a warning"
)
check(
    !ActivityConfirmationMessagePolicy.isWarning("已记录你的回答，并重新整理待确认内容。"),
    "a completed group answer may render as a success banner"
)
check(
    ActivityConfirmationVisibilityPolicy.isVisibleQuestion(state: "pending"),
    "an unanswered pending question must remain visible"
)
check(
    ActivityConfirmationVisibilityPolicy.isVisibleQuestion(state: "deferred"),
    "a deferred question must remain visible even when user_answer is defer"
)
check(
    !ActivityConfirmationVisibilityPolicy.isVisibleQuestion(state: "confirmed"),
    "a confirmed question must leave the pending card surface"
)
check(
    !ActivityConfirmationVisibilityPolicy.isVisibleQuestion(state: "ignored"),
    "an ignored question must leave the pending surface without becoming an occurrence claim"
)
check(
    ["deferred", "pending"].sorted {
        ActivityConfirmationVisibilityPolicy.sortPriority(state: $0)
            < ActivityConfirmationVisibilityPolicy.sortPriority(state: $1)
    } == ["pending", "deferred"],
    "pending questions must sort ahead of temporarily deferred questions"
)
check(
    ActivityConfirmationVisibilityPolicy.statusLabel(state: "deferred") == "已暂时跳过",
    "a deferred card must explain why it remains visible"
)
check(
    ActivityConfirmationFieldLabel.displayName(for: "company") == "公司"
        && ActivityConfirmationFieldLabel.displayName(for: "round") == "轮次",
    "clarification missing fields must use user-facing Chinese labels"
)
check(
    ActivityConfirmationFieldLabel.displayName(for: "unexpected_internal_field") == "其他信息",
    "unknown clarification fields must not leak unexplained internal identifiers"
)

let availableEvidence = ActivityConfirmationEvidenceSummary(
    status: "available",
    evidenceCount: 2,
    availableSourceCount: 2
)
check(
    availableEvidence.status == .available && availableEvidence.canReinspectRetainedFrames,
    "available evidence must explicitly allow retained-frame reinspection"
)
check(
    availableEvidence.statusLabel == "原截图已保留（2 张）",
    "available evidence must explain that screenshots were retained for reinspection"
)
let partialEvidence = ActivityConfirmationEvidenceSummary(
    status: "partial",
    evidenceCount: 1,
    availableSourceCount: 1
)
check(
    partialEvidence.status == .partial
        && partialEvidence.statusLabel.contains("覆盖 1 条线索"),
    "partial evidence must disclose its limited source coverage"
)
let unavailableEvidence = ActivityConfirmationEvidenceSummary(
    status: "unexpected",
    evidenceCount: -1,
    availableSourceCount: -1
)
check(
    unavailableEvidence.status == .unavailable
        && unavailableEvidence.evidenceCount == 0
        && !unavailableEvidence.canReinspectRetainedFrames,
    "unknown or malformed evidence metadata must fail closed as unavailable"
)
check(
    ActivityConfirmationClarificationPolicy.normalizedOutcome("not_requested") == nil,
    "a clarification that was not requested must not render an empty result panel"
)
check(
    ActivityConfirmationClarificationPolicy.normalizedOutcome("rewritten") == .rewritten,
    "a completed clarification outcome must remain visible"
)
check(
    ActivityConfirmationClarificationPolicy.normalizedOutcome("system_filtered") == .systemFiltered
        && ActivityConfirmationClarificationPolicy.normalizedOutcome("unchanged") == .unchanged
        && ActivityConfirmationClarificationPolicy.normalizedOutcome("evidence_unavailable") == .evidenceUnavailable,
    "v3 must distinguish filtered, unchanged, and unavailable-evidence clarification outcomes"
)
check(
    ActivityConfirmationClarificationPolicy.normalizedSeverity("warning") == .warning
        && ActivityConfirmationClarificationPolicy.normalizedSeverity("green") == nil,
    "clarification severity must use the structured v3 enum"
)
let normalizedInputs = ActivityConfirmationClarificationPolicy.normalizedInputs([
    "retained_frame_ocr", "stored_cleaned_text", "user_note", "retained_frame_ocr",
])
check(
    normalizedInputs == [.retainedFrameOCR, .storedCleanedText, .userNote],
    "clarification inputs must be typed, ordered, and deduplicated"
)
check(
    normalizedInputs.map(\.label) == ["暂存截图 OCR", "已清洗识别内容", "本次补充说明"],
    "the UI must disclose which safe inputs were actually reread"
)
check(
    !ActivityConfirmationClarificationPolicy.inputsAreValid(["retained_frame_ocr", "raw_path"]),
    "unknown clarification input sources must fail the v3 contract"
)
check(
    ActivityConfirmationClarificationPolicy.outcomeSeverityPairIsValid(
        outcome: .systemFiltered,
        severity: .success
    )
        && ActivityConfirmationClarificationPolicy.outcomeSeverityPairIsValid(
            outcome: .rewritten,
            severity: .success
        )
        && ActivityConfirmationClarificationPolicy.outcomeSeverityPairIsValid(
            outcome: .insufficientInformation,
            severity: .warning
        )
        && ActivityConfirmationClarificationPolicy.outcomeSeverityPairIsValid(
            outcome: .unchanged,
            severity: .neutral
        )
        && ActivityConfirmationClarificationPolicy.outcomeSeverityPairIsValid(
            outcome: .evidenceUnavailable,
            severity: .warning
        ),
    "each clarification outcome must carry its fixed semantic severity"
)
check(
    !ActivityConfirmationClarificationPolicy.outcomeSeverityPairIsValid(
        outcome: .insufficientInformation,
        severity: .success
    )
        && !ActivityConfirmationClarificationPolicy.outcomeSeverityPairIsValid(
            outcome: .unchanged,
            severity: .success
        ),
    "insufficient or unchanged evidence must never render as a green success"
)
let shanghai = TimeZone(secondsFromGMT: 8 * 60 * 60)!
check(
    ActivityConfirmationClarificationPolicy.generatedAtTimeLabel(
        "2026-07-18T12:34:56.123Z",
        timeZone: shanghai
    ) == "20:34",
    "a clarification timestamp must render as a stable local HH:mm label"
)
check(
    ActivityConfirmationClarificationPolicy.generatedAtTimeLabel("not-a-date", timeZone: shanghai).isEmpty,
    "an invalid clarification timestamp must fail closed without showing misleading time"
)
check(
    ActivityConfirmationClarificationPolicy.feedbackMessage(
        status: "clarified",
        outcome: .insufficientInformation,
        message: ""
    ) == "已重新梳理，但仍没有足够信息。",
    "a clarified but insufficient result must always have persistent feedback"
)
check(
    ActivityConfirmationClarificationPolicy.feedbackMessage(
        status: "clarified",
        outcome: .unchanged,
        message: ""
    ).contains("问题保持不变")
        && ActivityConfirmationClarificationPolicy.feedbackMessage(
            status: "clarified",
            outcome: .evidenceUnavailable,
            message: ""
        ).contains("原截图不可用"),
    "unchanged and unavailable-evidence outcomes must explain what the recheck actually found"
)
check(
    ActivityConfirmationClarificationPolicy.feedbackMessage(
        status: "clarified",
        outcome: nil,
        message: ""
    ) == "已重新梳理，但没有发现新增可用信息。",
    "a repeated clarification with no new information must never look like a silent no-op"
)
check(
    ActivityConfirmationClarificationPolicy.feedbackMessage(
        status: "stale",
        outcome: nil,
        message: "ignored"
    ).contains("线索已经变化"),
    "a stale revision must keep an explicit visible result"
)
check(
    ActivityConfirmationClarificationPolicy.feedbackMessage(
        status: "unsafe_contract",
        outcome: nil,
        message: ""
    ).contains("无法解析"),
    "a parse or contract error must keep an explicit visible result"
)
check(
    ActivityConfirmationClarificationPolicy.completedPayloadIsValid(
        status: "clarified",
        groupKey: "group-1",
        outcome: .rewritten,
        severity: .success,
        generatedAt: "2026-07-18T12:34:56Z",
        questionGroupCount: 1,
        containsRootGroup: true,
        memberRevisionsComplete: true,
        inputsFieldPresent: true,
        inputsValid: true,
        evidenceStatusValid: true
    ),
    "a complete clarified payload must satisfy the visible-result contract"
)
check(
    ActivityConfirmationClarificationPolicy.completedPayloadIsValid(
        status: "clarified",
        groupKey: "group-1",
        outcome: .systemFiltered,
        severity: .success,
        generatedAt: "2026-07-18T12:34:56Z",
        questionGroupCount: 0,
        containsRootGroup: false,
        memberRevisionsComplete: true,
        inputsFieldPresent: true,
        inputsValid: true,
        evidenceStatusValid: true
    ),
    "system-filtered may safely remove the invalid root question and return no replacement group"
)
check(
    !ActivityConfirmationClarificationPolicy.completedPayloadIsValid(
        status: "clarified",
        groupKey: "group-1",
        outcome: .insufficientInformation,
        severity: .success,
        generatedAt: "",
        questionGroupCount: 0,
        containsRootGroup: false,
        memberRevisionsComplete: false,
        inputsFieldPresent: false,
        inputsValid: false,
        evidenceStatusValid: false
    ),
    "a malformed or misleading clarified payload must fail closed as an unsafe contract"
)
check(
    !ActivityConfirmationClarificationPolicy.completedPayloadIsValid(
        status: "clarified",
        groupKey: "group-1",
        outcome: .rewritten,
        severity: .success,
        generatedAt: "2026-07-18T12:34:56Z",
        questionGroupCount: 1,
        containsRootGroup: true,
        memberRevisionsComplete: true,
        inputsFieldPresent: true,
        inputsValid: true,
        evidenceStatusValid: false
    ),
    "a v3 clarification without a valid evidence status must fail closed"
)

check(
    ConfirmedActivityContractPolicy.isReadable(source: "confirmed_activity_view", rawAccess: false),
    "Today may read only the explicit safe confirmed-activity contract"
)
check(
    !ConfirmedActivityContractPolicy.isReadable(source: "timeline", rawAccess: false),
    "Today must reject legacy or unexpected activity sources"
)
check(
    !ConfirmedActivityContractPolicy.isReadable(source: "confirmed_activity_view", rawAccess: true),
    "Today must reject a confirmed-activity contract that exposes raw access"
)
check(
    !StatusRefreshRetentionPolicy.preservesConfirmedActivity(for: "unavailable"),
    "a failed status refresh must clear previously confirmed Today facts"
)
check(
    StatusRefreshRetentionPolicy.preservesConfirmedActivity(for: "capturing"),
    "a successful live status may preserve its confirmed-activity payload"
)

let ready = RecordingControlPresentation.resolve(
    status: "ready",
    launchAgentLoaded: true,
    launchAgentRunning: true,
    pauseLabel: "未暂停",
    statusExplanation: "记录正常"
)
check(ready.action == .pause && ready.title == "暂停", "running must only offer Pause")
check(ready.title != "恢复", "running must never show Restore")

let paused = RecordingControlPresentation.resolve(
    status: "paused",
    launchAgentLoaded: true,
    launchAgentRunning: true,
    pauseLabel: "还剩 12 分钟",
    statusExplanation: "记录已暂停"
)
check(paused.action == .resume && paused.title == "继续记录", "paused must only offer Continue Recording")
check(paused.detail == "还剩 12 分钟", "paused must explain the remaining duration")

let stopped = RecordingControlPresentation.resolve(
    status: "stopped",
    launchAgentLoaded: false,
    launchAgentRunning: false,
    pauseLabel: "未暂停",
    statusExplanation: "记录服务未运行"
)
check(stopped.action == .start && stopped.title == "开始记录", "stopped must offer Start Recording")

let loadedButStopped = RecordingControlPresentation.resolve(
    status: "ready",
    launchAgentLoaded: true,
    launchAgentRunning: false,
    pauseLabel: "未暂停",
    statusExplanation: "记录服务异常"
)
check(loadedButStopped.action == .start && loadedButStopped.title == "开始记录", "a stopped loaded service must still say Start Recording")

for status in ["locked", "session_state_unknown", "unknown"] {
    let presentation = RecordingControlPresentation.resolve(
        status: status,
        launchAgentLoaded: true,
        launchAgentRunning: true,
        pauseLabel: "未暂停",
        statusExplanation: "状态待确认"
    )
    check(presentation.action == .none, "\(status) must not offer an invalid action")
}

let contextOnlyItems = (0..<8).map {
    DecisionSurfaceItem(
        key: "context-\($0)",
        decisionState: "needs_decision",
        queueBucket: "context_only",
        canDirectWrite: false,
        canRewriteWrite: false
    )
}
check(
    DecisionSurfacePolicy.actionableItems(memoryWriteActions: false, items: contextOnlyItems).isEmpty,
    "writer disabled plus eight context-only items must surface no decision"
)
check(
    DecisionSurfacePolicy.actionableItems(memoryWriteActions: true, items: contextOnlyItems).isEmpty,
    "context-only items must remain hidden even if writing is enabled in the future"
)

private func completeCoverage(rootCount: Int = 1) -> [String: Any] {
    [
        "enabled": true,
        "export_enabled": false,
        "baseline_status": "complete",
        "root_count": rootCount,
        "baseline_indexed_files": 12,
        "event_gap_count": 0,
        "benign_event_count": 0,
        "coverage_started_at": "2026-07-17T07:30:00Z",
        "last_event_at": "2026-07-17T08:30:00Z",
        "coverage_status": "complete",
        "is_complete": true,
    ]
}

private func completeTotals(
    distinct: Int,
    created: Int = 0,
    modified: Int = 0,
    renamed: Int = 0,
    deleted: Int = 0,
    detailed: Int = 0,
    metadataOnly: Int = 0,
    protected: Int = 0,
    aiOnly: Int = 0,
    userModified: Int = 0,
    unattributed: Int? = nil
) -> [String: Any] {
    [
        "distinct_files": distinct,
        "created": created,
        "modified": modified,
        "renamed": renamed,
        "deleted": deleted,
        "detailed": detailed,
        "metadata_only": metadataOnly,
        "protected": protected,
        "ai_only": aiOnly,
        "user_modified": userModified,
        "unattributed": unattributed ?? max(0, distinct - aiOnly - userModified),
    ]
}

private func detailedItem(index: Int = 0) -> [String: Any] {
    [
        "project_label": "PAM",
        "file_name_redacted": "safe-\(index).md",
        "file_type": "md",
        "change_kind": "modified",
        "first_changed_at": "2026-07-17T08:00:00Z",
        "last_changed_at": "2026-07-17T08:30:00Z",
        "version_count": 1,
        "change_summary_redacted": "安全摘要 \(index)",
        "detail_level": "detailed",
        "confidence": "local_file_changed",
        "actor_classification": "unattributed",
        "attribution_confidence": "unknown",
        "attribution_reason_codes": ["no_direct_actor_evidence"],
        "attribution_tool": "",
    ]
}

let safeDocumentChanges = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(rootCount: 1),
    "totals": completeTotals(distinct: 2, modified: 2, detailed: 1, metadataOnly: 1, userModified: 1),
    "items": [
        [
            "project_label": "PAM",
            "file_name_redacted": "README.md",
            "file_type": "md",
            "change_kind": "modified",
            "first_changed_at": "2026-07-17T08:00:00Z",
            "last_changed_at": "2026-07-17T08:30:00Z",
            "version_count": 2,
            "change_summary_redacted": "更新了安装说明。",
            "detail_level": "detailed",
            "confidence": "likely_user_edit",
            "actor_classification": "user_modified",
            "attribution_confidence": "confirmed_human_editor_evidence",
            "attribution_reason_codes": ["foreground_editor_exact_mtime"],
            "attribution_tool": "Visual Studio Code",
        ],
        [
            "project_label": "",
            "file_name_redacted": "notes.pdf",
            "file_type": "pdf",
            "change_kind": "modified",
            "first_changed_at": "2026-07-17T08:10:00Z",
            "last_changed_at": "2026-07-17T08:10:00Z",
            "version_count": 1,
            "change_summary_redacted": "",
            "detail_level": "metadata_only",
            "confidence": "local_file_changed",
            "actor_classification": "unattributed",
            "attribution_confidence": "unknown",
            "attribution_reason_codes": ["no_direct_actor_evidence"],
            "attribution_tool": "",
        ],
    ],
    "roots": [
        ["root_ref": "rootref_0123456789abcdefghijklmn", "label": "Obsidian 默认仓库", "display_path_redacted": "~/…/默认仓库", "enabled": true],
    ],
    "limitations": [],
])
check(safeDocumentChanges.isAvailable, "filesystem-confirmed document changes with raw_access=false must be accepted")
check(safeDocumentChanges.totals.distinctFiles == 2, "document change totals must preserve the confirmed file count")
check(safeDocumentChanges.items.count == 2, "safe document change items must be available to Today and Review")
check(safeDocumentChanges.items[1].safeSummary == "检测到文件变化，但没有可安全概括的前后内容。", "metadata-only changes must use deterministic wording")
check(safeDocumentChanges.roots.first?.displayLabel == "Obsidian 默认仓库", "Settings must use the Core-provided redacted root label")

let encodedSafeDocumentChanges = try! JSONSerialization.data(withJSONObject: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 1, modified: 1, detailed: 1),
    "items": [detailedItem()],
    "limitations": [],
])
let decodedSafeDocumentChanges = try! JSONSerialization.jsonObject(with: encodedSafeDocumentChanges) as! [String: Any]
check(DocumentChangesSnapshot(dictionary: decodedSafeDocumentChanges).isAvailable, "JSON NSNumber bridging must preserve strict Bool and Int contract types")

var ownerLocalDocumentChanges = decodedSafeDocumentChanges
ownerLocalDocumentChanges["source"] = "document_owner_local_observation"
ownerLocalDocumentChanges["contract_version"] = "DocumentOwnerLocalObservationV2"
ownerLocalDocumentChanges["projection_type"] = "owner_local_observation"
ownerLocalDocumentChanges["verification_state"] = "unverified"
ownerLocalDocumentChanges["trusted_export_eligible"] = false
check(DocumentChangesSnapshot(dictionary: ownerLocalDocumentChanges).isBlocked, "owner-local observations must stay blocked outside the explicit Review parser")
let ownerLocalSnapshot = DocumentChangesSnapshot(dictionary: ownerLocalDocumentChanges, allowOwnerLocalObservation: true)
check(ownerLocalSnapshot.isAvailable && ownerLocalSnapshot.source == "document_owner_local_observation", "Review must preserve the unverified source rather than relabel it as confirmed")
for key in ["contract_version", "projection_type", "verification_state", "trusted_export_eligible", "raw_access"] {
    var missing = ownerLocalDocumentChanges
    missing.removeValue(forKey: key)
    check(DocumentChangesSnapshot(dictionary: missing, allowOwnerLocalObservation: true).isBlocked, "Review must reject a missing owner-local safety marker: \(key)")
}
for (key, value) in [
    ("contract_version", "unsupported" as Any),
    ("projection_type", "trusted_verified_context" as Any),
    ("verification_state", "verified_only" as Any),
    ("trusted_export_eligible", true as Any),
    ("trusted_export_eligible", 0 as Any),
    ("raw_access", 0 as Any),
    ("roots", [] as [[String: Any]])
] {
    var changed = ownerLocalDocumentChanges
    changed[key] = value
    check(DocumentChangesSnapshot(dictionary: changed, allowOwnerLocalObservation: true).isBlocked, "Review must reject changed owner-local markers and root controls: \(key)")
}

var numericRawAccess = decodedSafeDocumentChanges
numericRawAccess["raw_access"] = 0
check(DocumentChangesSnapshot(dictionary: numericRawAccess).isBlocked, "numeric raw_access=0 must not masquerade as false")

let manyDocumentChanges = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 5, modified: 5, detailed: 5),
    "items": (0..<5).map(detailedItem(index:)),
    "limitations": [],
])
check(DocumentChangesPresentation.todayItems(from: manyDocumentChanges).count == 3, "Today must show at most three document summaries")

let protectedDocumentChanges = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 1, modified: 1, protected: 1),
    "items": [[
        "file_name": "private-client-name.md",
        "content": "must never enter a UI model",
        "detail_level": "protected",
    ]],
    "limitations": [],
])
check(protectedDocumentChanges.items.isEmpty, "protected items must be discarded even if a malformed Core response supplies a filename")
check(protectedDocumentChanges.totals.protected == 1, "protected document changes remain visible only as a count")

var legacyRawItem = detailedItem()
legacyRawItem.removeValue(forKey: "file_name_redacted")
legacyRawItem["file_name"] = "unsafe-fallback.md"
let legacyRawFallback = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 1, modified: 1, detailed: 1),
    "items": [legacyRawItem],
    "limitations": [],
])
check(legacyRawFallback.isBlocked, "legacy file_name must never substitute for file_name_redacted")

var unsafeSummaryItem = detailedItem()
unsafeSummaryItem["summary"] = "raw summary must be rejected"
let unsafeSummaryPayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 1, modified: 1, detailed: 1),
    "items": [unsafeSummaryItem],
    "limitations": [],
])
check(unsafeSummaryPayload.isBlocked, "raw summary fields must fail closed even when a redacted summary also exists")

var unknownKindItem = detailedItem()
unknownKindItem["change_kind"] = "opened"
let unknownKindPayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 1, modified: 1, detailed: 1),
    "items": [unknownKindItem],
    "limitations": [],
])
check(unknownKindPayload.isBlocked, "unknown change_kind values must fail closed")

var unknownCoverage = completeCoverage()
unknownCoverage["baseline_status"] = "probably_complete"
let unknownCoveragePayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": unknownCoverage,
    "totals": completeTotals(distinct: 0),
    "items": [],
    "limitations": [],
])
check(unknownCoveragePayload.isBlocked, "unknown baseline_status values must fail closed")

var missingCoverageField = completeCoverage()
missingCoverageField.removeValue(forKey: "last_event_at")
let incompleteCoveragePayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": missingCoverageField,
    "totals": completeTotals(distinct: 0),
    "items": [],
    "limitations": [],
])
check(incompleteCoveragePayload.isBlocked, "missing required coverage fields must fail closed")

let rawRootPayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 0),
    "items": [],
    "roots": [["root_ref": "rootref_0123456789abcdefghijklmn", "label": "Documents", "display_path_redacted": "Documents", "enabled": true, "path": "/private/raw"]],
    "limitations": [],
])
check(rawRootPayload.isBlocked, "raw root paths must fail closed")

let internalRootIDPayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 0),
    "items": [],
    "roots": [["root_ref": "rootref_0123456789abcdefghijklmn", "root_id": "root_internal_hmac", "label": "Documents", "display_path_redacted": "Documents", "enabled": true]],
    "limitations": [],
])
check(internalRootIDPayload.isBlocked, "internal root HMAC identifiers must fail closed")

let malformedRootRefPayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 0),
    "items": [],
    "roots": [["root_ref": "rootref_too-short", "label": "Documents", "display_path_redacted": "Documents", "enabled": true]],
    "limitations": [],
])
check(malformedRootRefPayload.isBlocked, "malformed public root references must fail closed")

let smuggledInternalHashRefPayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 0),
    "items": [],
    "roots": [["root_ref": "rootref_root_0123456789abcdef0123456789abcdef", "label": "Documents", "display_path_redacted": "Documents", "enabled": true]],
    "limitations": [],
])
check(smuggledInternalHashRefPayload.isBlocked, "root references cannot smuggle internal HMAC identifiers")

let duplicateRootRefPayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(rootCount: 2),
    "totals": completeTotals(distinct: 0),
    "items": [],
    "roots": [
        ["root_ref": "rootref_0123456789abcdefghijklmn", "label": "Documents", "display_path_redacted": "Documents", "enabled": true],
        ["root_ref": "rootref_0123456789abcdefghijklmn", "label": "Desktop", "display_path_redacted": "Desktop", "enabled": true],
    ],
    "limitations": [],
])
check(duplicateRootRefPayload.isBlocked, "duplicate public root references must fail closed")

let incompleteRootControlsPayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(rootCount: 2),
    "totals": completeTotals(distinct: 0),
    "items": [],
    "roots": [["root_ref": "rootref_0123456789abcdefghijklmn", "label": "Documents", "display_path_redacted": "Documents", "enabled": true]],
    "limitations": ["root_control_refs_incomplete"],
])
check(incompleteRootControlsPayload.isBlocked, "incomplete root control sets must fail closed")

let emptyIncompleteRootControlsPayload = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(rootCount: 0),
    "totals": completeTotals(distinct: 0),
    "items": [],
    "roots": [],
    "limitations": ["root_control_refs_incomplete"],
])
check(emptyIncompleteRootControlsPayload.isBlocked, "empty but incomplete root control sets must fail closed")

let missingRawBoundary = DocumentChangesSnapshot(dictionary: [
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 0),
    "items": [],
    "limitations": [],
])
check(missingRawBoundary.isBlocked, "missing raw_access must fail closed")
check(missingRawBoundary.items.isEmpty, "a blocked document response must not expose items")

let unsafeRawBoundary = DocumentChangesSnapshot(dictionary: [
    "raw_access": true,
    "source": "filesystem_confirmed",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 0),
    "items": [],
    "limitations": [],
])
check(unsafeRawBoundary.isBlocked, "raw_access=true must fail closed")
check(unsafeRawBoundary.items.isEmpty, "raw_access=true must discard all document items")

let screenshotOnlySource = DocumentChangesSnapshot(dictionary: [
    "raw_access": false,
    "source": "screenshot_inferred",
    "coverage": completeCoverage(),
    "totals": completeTotals(distinct: 0),
    "items": [],
    "limitations": [],
])
check(screenshotOnlySource.isBlocked, "screenshot-inferred document changes must not be shown as confirmed edits")

let unavailableDocumentChanges = DocumentChangesSnapshot()
check(!unavailableDocumentChanges.isAvailable, "a missing status response must not be presented as available document tracking")
check(!unavailableDocumentChanges.isBlocked, "unavailable and contract-blocked document states must remain distinguishable")
check(unavailableDocumentChanges.totals.distinctFiles == 0, "an unavailable document snapshot must not retain stale totals")
check(
    DocumentChangesFreshnessPolicy.mustDiscardConfirmedChanges(coreStatus: "busy"),
    "a busy full-status response must discard stale confirmed document changes"
)
check(
    DocumentChangesFreshnessPolicy.mustDiscardConfirmedChanges(coreStatus: "unavailable"),
    "an unavailable full-status response must discard stale confirmed document changes"
)
check(
    !DocumentChangesFreshnessPolicy.mustDiscardConfirmedChanges(coreStatus: "ready"),
    "a fresh ready response may present its own confirmed document changes"
)
check(
    ActivityConfirmationDiagnosticsLoadPolicy.shouldReloadAfterCardsReplacement(
        diagnosticsStatus: "unknown",
        diagnosticsExpanded: true,
        calibrationsExpanded: false
    ),
    "expanded confirmation diagnostics must reload after cards replacement resets the snapshot"
)
check(
    !ActivityConfirmationDiagnosticsLoadPolicy.shouldReloadAfterCardsReplacement(
        diagnosticsStatus: "unknown",
        diagnosticsExpanded: false,
        calibrationsExpanded: false
    ),
    "collapsed confirmation diagnostics must stay lazy after cards replacement"
)

check(
    ActivityConfirmationV5Contract.excludesForbiddenFields([
        "display_title": "发布 Solo Staging 表单",
        "display_question": "你是否发布了 Solo Staging 表单？",
        "display_evidence": "尚未检测到独立发布回执。",
    ]),
    "a V5 presentation-only card may pass the forbidden-field boundary"
)
check(
    ActivityConfirmationV5Contract.forbiddenFields.allSatisfy { field in
        !ActivityConfirmationV5Contract.excludesForbiddenFields([field: "private"])
    },
    "every V5 raw, provenance, transcript, command, and legacy content field must fail closed"
)
check(
    !ActivityConfirmationV5Contract.excludesForbiddenFields([
        "cards": [[
            "canonical_subject": ["object": "Solo Staging 表单", "ocr": "private"],
        ]],
    ]),
    "V5 forbidden fields must fail closed recursively across the page and canonical subject"
)
check(
    !ActivityConfirmationV5Contract.excludesForbiddenFields([
        "cards": [[
            "display_title": "发布 Solo Staging 表单",
            "cleaned_contents": [["text": "private OCR"]],
        ]],
    ]),
    "V5 cards must reject legacy cleaned_contents recursively"
)
check(
    ActivityConfirmationCardInteractionPolicy.showsAnswerActions(
        isInformationInsufficient: false
    ),
    "identified confirmation cards must retain answer actions"
)
check(
    !ActivityConfirmationCardInteractionPolicy.showsAnswerActions(
        isInformationInsufficient: true
    ),
    "information-insufficient cards must never expose answer actions"
)


runNamedTest("status_unknown_payload_accepts_null_service_fields") {
    var state = ProductionStatusClientState()
    let unknown_busy_B = productionStatusBusyPayload(
        observedAt: "2026-07-29T02:00:03Z",
        lastSuccessfulReadAt: NSNull()
    )
    let transition = state.apply(dictionary: unknown_busy_B, interface: .fullStatus)
    check(transition.accepted, "unknown/database_busy must accept intentional null service fields")
    check(transition.readState == .busy, "database_busy must reduce to busy rather than a healthy state")
    check(transition.mustClearCurrentState, "database_busy must clear every current production value")
    check(transition.actionAvailability == .refreshOnly, "database_busy must leave refresh as the only status action")
    check(state.trustedSnapshot == nil, "database_busy with null service fields must expose no trusted snapshot")
    check(state.lastSuccessfulReadAt == nil, "a busy payload may not invent a successful-read timestamp")
    check(state.unknownStatusTextCN == "状态未知 · 观察时间 2026-07-29T02:00:03Z", "busy wording must show 状态未知 and the observation time")

    var invalidLastSuccess = unknown_busy_B
    invalidLastSuccess["last_successful_read_at"] = "not-a-utc-timestamp"
    var invalidState = ProductionStatusClientState()
    let invalidTransition = invalidState.apply(dictionary: invalidLastSuccess, interface: .fullStatus)
    check(!invalidTransition.accepted && invalidState.readState == .blocked, "busy may retain only null or a parseable real UTC last-success timestamp")
}

runNamedTest("success_busy_recovery_clears_then_restores_trusted_state") {
    var state = ProductionStatusClientState()
    let full_success_A = productionStatusSuccessPayload(
        label: "A",
        observedAt: "2026-07-29T03:00:00Z",
        seed: 1
    )
    let transitionA = state.apply(dictionary: full_success_A, interface: .fullStatus)
    check(transitionA.accepted && transitionA.readState == .current, "full success A must be accepted as current")
    check(transitionA.shouldAdvanceSuccessfulStatusTime, "full success A may advance the successful status time")
    check(transitionA.actionAvailability == .captureControls, "full success A must enable the normal capture controls")
    check(state.hasCurrentFullSnapshot, "full success A must establish current full evidence")
    let snapshotA = state.trustedSnapshot
    check(snapshotA?.launchAgentPID == 1001, "A must install its service PID")
    check(snapshotA?.activityCount == 11, "A must install its activity count")
    check(snapshotA?.healthStatus == "health_A", "A must install its health status")
    check(snapshotA?.cadenceSeconds == 51, "A must install its cadence")
    check(snapshotA?.sessionRef == "session_A", "A must install its session reference")
    check(snapshotA?.documentRef == "document_A", "A must install its document reference")
    check(snapshotA?.currentRef == "current_A", "A must install its current reference")
    check(snapshotA?.trustedRef == "trusted_A", "A must install its trusted reference")
    check(snapshotA?.confirmedRef == "confirmed_A", "A must install its confirmed reference")
    check(snapshotA?.verifiedRef == "verified_A", "A must install its verified reference")
    check(snapshotA?.contextRef == "context_A", "A must install its context reference")
    check(snapshotA?.bundleRef == "bundle_A", "A must install its bundle reference")
    check(snapshotA?.bundleHash == "sha256:A", "A must install its bundle hash")

    let successfulTimeA = state.lastSuccessfulReadAt
    let unknown_busy_B = productionStatusBusyPayload(
        observedAt: "2026-07-29T03:00:03Z",
        lastSuccessfulReadAt: successfulTimeA ?? NSNull()
    )
    let transitionB = state.apply(dictionary: unknown_busy_B, interface: .fullStatus)
    check(transitionB.accepted && transitionB.readState == .busy, "unknown busy B must be accepted as unreadable")
    check(!transitionB.shouldAdvanceSuccessfulStatusTime, "unknown busy B must not advance successful status time")
    check(transitionB.mustClearCurrentState, "unknown busy B must require complete current-state clearing")
    check(transitionB.requiresFullRefresh, "unknown busy B must force the next full refresh")
    check(transitionB.actionAvailability == .refreshOnly, "unknown busy B must expose refresh-only actions")
    check(state.trustedSnapshot == nil, "B must clear current/trusted/confirmed/verified, service, PID, counts, health, cadence, session, document, context and bundle values")
    check(!state.hasCurrentFullSnapshot, "B must invalidate A as reusable full evidence")
    check(state.lastSuccessfulReadAt == successfulTimeA, "B must retain only the real last-success metadata from A")
    check(state.observedAt == "2026-07-29T03:00:03Z", "B must retain its own observation time")
    check(state.unknownStatusTextCN.contains("状态未知"), "B must present the Chinese 状态未知 wording")
    check(state.unknownStatusTextCN.contains("2026-07-29T03:00:03Z"), "B wording must include the observation time")
    let busyControl = RecordingControlPresentation.resolve(
        status: "unknown",
        launchAgentLoaded: false,
        launchAgentRunning: false,
        pauseLabel: "状态未知",
        statusExplanation: state.unknownStatusTextCN
    )
    check(busyControl.action == .none, "B must expose no Start/Pause/Resume/record control")
    check(busyControl.headline == "状态未知", "B recording presentation must not say stopped or healthy")
    check(!StatusRefreshRetentionPolicy.preservesConfirmedActivity(for: "unknown"), "unknown must clear stale confirmed activity")
    check(!StatusRefreshRetentionPolicy.preservesConfirmedActivity(for: "database_busy"), "database_busy must clear stale confirmed activity")
    check(DocumentChangesFreshnessPolicy.mustDiscardConfirmedChanges(coreStatus: "unknown"), "unknown must clear stale document changes")
    check(DocumentChangesFreshnessPolicy.mustDiscardConfirmedChanges(coreStatus: "database_busy"), "database_busy must clear stale document changes")

    let success_C = productionStatusSuccessPayload(
        label: "C",
        observedAt: "2026-07-29T03:00:06Z",
        seed: 7
    )
    let transitionC = state.apply(dictionary: success_C, interface: .fullStatus)
    check(transitionC.accepted && transitionC.readState == .current, "success C must recover the full production state")
    check(transitionC.shouldAdvanceSuccessfulStatusTime, "success C may advance the successful status time")
    check(!transitionC.requiresFullRefresh, "full success C must satisfy the forced full refresh")
    check(transitionC.actionAvailability == .captureControls, "full success C must restore capture controls")
    let snapshotC = state.trustedSnapshot
    check(snapshotC?.launchAgentPID == 1007 && snapshotC?.launchAgentPID != snapshotA?.launchAgentPID, "recovery must use only C service/PID values")
    check(snapshotC?.activityCount == 17 && snapshotC?.activityCount != snapshotA?.activityCount, "recovery must use only C count values")
    check(snapshotC?.healthStatus == "health_C" && snapshotC?.healthStatus != snapshotA?.healthStatus, "recovery must use only C health")
    check(snapshotC?.cadenceSeconds == 57 && snapshotC?.cadenceSeconds != snapshotA?.cadenceSeconds, "recovery must use only C cadence")
    check(snapshotC?.sessionRef == "session_C" && snapshotC?.sessionRef != snapshotA?.sessionRef, "recovery must use only C session")
    check(snapshotC?.documentRef == "document_C" && snapshotC?.documentRef != snapshotA?.documentRef, "recovery must use only C document")
    check(snapshotC?.currentRef == "current_C" && snapshotC?.currentRef != snapshotA?.currentRef, "recovery must use only C current reference")
    check(snapshotC?.trustedRef == "trusted_C" && snapshotC?.trustedRef != snapshotA?.trustedRef, "recovery must use only C trusted reference")
    check(snapshotC?.confirmedRef == "confirmed_C" && snapshotC?.confirmedRef != snapshotA?.confirmedRef, "recovery must use only C confirmed reference")
    check(snapshotC?.verifiedRef == "verified_C" && snapshotC?.verifiedRef != snapshotA?.verifiedRef, "recovery must use only C verified reference")
    check(snapshotC?.contextRef == "context_C" && snapshotC?.contextRef != snapshotA?.contextRef, "recovery must use only C context reference")
    check(snapshotC?.bundleRef == "bundle_C" && snapshotC?.bundleRef != snapshotA?.bundleRef, "recovery must use only C bundle reference")
    check(snapshotC?.bundleHash == "sha256:C" && snapshotC?.bundleHash != snapshotA?.bundleHash, "recovery must use only C bundle hash")
    check(state.lastSuccessfulReadAt == "2026-07-29T03:00:06Z", "success C must replace A's successful-read metadata")

    var paused = productionStatusSuccessPayload(label: "paused", observedAt: "2026-07-29T03:00:09Z", seed: 9, status: "paused")
    paused["capture_allowed_by_session"] = true
    let pausedTransition = state.apply(dictionary: paused, interface: .fullStatus)
    check(pausedTransition.readState == .current, "paused must remain a current readable state")
    check(pausedTransition.actionAvailability == .captureControls, "paused must retain resume controls")

    let locked = productionStatusSuccessPayload(label: "locked", observedAt: "2026-07-29T03:00:12Z", seed: 12, status: "locked", captureAllowed: false)
    let lockedTransition = state.apply(dictionary: locked, interface: .fullStatus)
    check(lockedTransition.readState == .current, "locked must remain distinguishable from database unknown")
    check(lockedTransition.actionAvailability == .refreshOnly, "locked must not expose capture mutation controls")
    check(state.trustedSnapshot != nil, "locked must not be confused with a database-busy clear")

    let sessionUnknown = productionStatusSuccessPayload(label: "session_unknown", observedAt: "2026-07-29T03:00:15Z", seed: 15, status: "session_state_unknown", captureAllowed: false)
    let sessionUnknownTransition = state.apply(dictionary: sessionUnknown, interface: .fullStatus)
    check(sessionUnknownTransition.readState == .current, "session unknown must remain a readable privacy state")
    check(sessionUnknownTransition.actionAvailability == .refreshOnly, "session unknown must remain fail-closed for capture actions")
    check(state.trustedSnapshot?.sessionRef == "session_session_unknown", "session unknown must install only its own current payload")
}

runNamedTest("quick_success_busy_recovery_uses_only_new_payload") {
    var state = ProductionStatusClientState()
    var quick_success_A = productionStatusSuccessPayload(
        label: "quick_A",
        observedAt: "2026-07-29T04:00:00Z",
        seed: 21
    )
    quick_success_A["interface"] = "quick_status"
    let quickA = state.apply(dictionary: quick_success_A, interface: .quickStatus)
    check(quickA.accepted && quickA.readState == .current, "quick success A must decode")
    check(quickA.requiresFullRefresh, "an initial quick success cannot establish full-production evidence")
    check(quickA.actionAvailability == .refreshOnly, "initial quick success must remain refresh-only")
    let snapshotA = state.trustedSnapshot
    check(snapshotA?.trustedRef == "trusted_quick_A", "quick A must install its own trusted marker")

    var unknown_busy_B = productionStatusBusyPayload(
        observedAt: "2026-07-29T04:00:03Z",
        lastSuccessfulReadAt: state.lastSuccessfulReadAt ?? NSNull()
    )
    unknown_busy_B["interface"] = "quick_status"
    let quickB = state.apply(dictionary: unknown_busy_B, interface: .quickStatus)
    check(quickB.readState == .busy, "quick unknown B must enter busy")
    check(quickB.mustClearCurrentState && state.trustedSnapshot == nil, "quick B must clear every A value")
    check(quickB.actionAvailability == .refreshOnly, "quick B must expose refresh only")
    check(!quickB.shouldAdvanceSuccessfulStatusTime, "quick B must not advance the successful read time")

    var success_C = productionStatusSuccessPayload(
        label: "quick_C",
        observedAt: "2026-07-29T04:00:06Z",
        seed: 27
    )
    success_C["interface"] = "quick_status"
    let quickC = state.apply(dictionary: success_C, interface: .quickStatus)
    check(quickC.accepted && quickC.readState == .current, "quick success C must recover from busy")
    check(quickC.requiresFullRefresh, "quick C must still force a full refresh before production mutations")
    check(quickC.actionAvailability == .refreshOnly, "quick C may not restore capture controls after B")
    let snapshotC = state.trustedSnapshot
    check(snapshotC?.launchAgentPID == 1027 && snapshotC?.launchAgentPID != snapshotA?.launchAgentPID, "quick recovery must use C service/PID only")
    check(snapshotC?.activityCount == 37 && snapshotC?.activityCount != snapshotA?.activityCount, "quick recovery must use C counts only")
    check(snapshotC?.healthStatus == "health_quick_C" && snapshotC?.healthStatus != snapshotA?.healthStatus, "quick recovery must use C health only")
    check(snapshotC?.sessionRef == "session_quick_C" && snapshotC?.sessionRef != snapshotA?.sessionRef, "quick recovery must use C session only")
    check(snapshotC?.documentRef == "document_quick_C" && snapshotC?.documentRef != snapshotA?.documentRef, "quick recovery must use C document only")
    check(snapshotC?.trustedRef == "trusted_quick_C" && snapshotC?.trustedRef != snapshotA?.trustedRef, "quick recovery must use C trusted marker only")
    check(snapshotC?.verifiedRef == "verified_quick_C" && snapshotC?.verifiedRef != snapshotA?.verifiedRef, "quick recovery must use C verified marker only")
    check(snapshotC?.bundleHash == "sha256:quick_C" && snapshotC?.bundleHash != snapshotA?.bundleHash, "quick recovery must use C bundle hash only")
    check(state.lastSuccessfulReadAt == "2026-07-29T04:00:06Z", "quick success C must install C's successful-read time")
}

runNamedTest("full_success_unknown_success_uses_only_c") {
    var state = ProductionStatusClientState()
    let fullA = productionStatusSuccessPayload(
        label: "full_A",
        observedAt: "2026-07-29T01:00:00Z",
        seed: 111
    )
    _ = state.apply(dictionary: fullA, interface: .fullStatus)
    let aSnapshot = state.trustedSnapshot
    let busy = productionStatusBusyPayload(
        observedAt: "2026-07-29T01:00:03Z",
        lastSuccessfulReadAt: "2026-07-29T01:00:00Z"
    )
    let transitionB = state.apply(dictionary: busy, interface: .fullStatus)
    check(transitionB.mustClearCurrentState && state.trustedSnapshot == nil, "full B must remove every A value")

    var fullC = productionStatusSuccessPayload(
        label: "full_C",
        observedAt: "2026-07-29T01:00:06Z",
        seed: 222
    )
    fullC["launch_agent_running"] = false
    fullC["launch_agent_last_exit_code"] = 17
    let transitionC = state.apply(dictionary: fullC, interface: .fullStatus)
    let cSnapshot = state.trustedSnapshot
    check(transitionC.accepted && !transitionC.requiresFullRefresh, "full C must satisfy the forced full refresh")
    check(cSnapshot?.launchAgentPID == 1222 && cSnapshot?.launchAgentPID != aSnapshot?.launchAgentPID, "full recovery must install only C service identity")
    check(cSnapshot?.launchAgentRunning == false, "full recovery must install C stopped service evidence")
    check(cSnapshot?.launchAgentLastExitCode == 17, "full recovery must install C exit evidence")
    check(cSnapshot?.activityCount == 232 && cSnapshot?.activityCount != aSnapshot?.activityCount, "full recovery must install only C counts")
    check(cSnapshot?.sessionRef == "session_full_C" && cSnapshot?.sessionRef != aSnapshot?.sessionRef, "full recovery must install only C session")
    check(cSnapshot?.documentRef == "document_full_C" && cSnapshot?.documentRef != aSnapshot?.documentRef, "full recovery must install only C document")
    check(cSnapshot?.trustedRef == "trusted_full_C" && cSnapshot?.trustedRef != aSnapshot?.trustedRef, "full recovery must install only C trusted reference")
    check(cSnapshot?.bundleHash == "sha256:full_C" && cSnapshot?.bundleHash != aSnapshot?.bundleHash, "full recovery must install only C bundle")
}

runNamedTest("quick_success_unknown_success_uses_only_c") {
    var state = ProductionStatusClientState()
    var quickA = productionStatusSuccessPayload(
        label: "named_quick_A",
        observedAt: "2026-07-29T01:00:00Z",
        seed: 311
    )
    quickA["interface"] = "quick_status"
    _ = state.apply(dictionary: quickA, interface: .quickStatus)
    let aSnapshot = state.trustedSnapshot

    var busy = productionStatusBusyPayload(
        observedAt: "2026-07-29T01:00:03Z",
        lastSuccessfulReadAt: "2026-07-29T01:00:00Z"
    )
    busy["interface"] = "quick_status"
    busy["attention_codes"] = ["database_busy"]
    _ = state.apply(dictionary: busy, interface: .quickStatus)
    check(state.trustedSnapshot == nil, "quick B must clear A")

    var quickC = productionStatusSuccessPayload(
        label: "named_quick_C",
        observedAt: "2026-07-29T01:00:06Z",
        seed: 422
    )
    quickC["interface"] = "quick_status"
    quickC["launch_agent_running"] = false
    let transitionC = state.apply(dictionary: quickC, interface: .quickStatus)
    let cSnapshot = state.trustedSnapshot
    check(transitionC.requiresFullRefresh && transitionC.actionAvailability == .refreshOnly, "quick C must not regain mutations before a new full read")
    check(cSnapshot?.launchAgentPID == 1422 && cSnapshot?.launchAgentPID != aSnapshot?.launchAgentPID, "quick recovery must install only C service identity")
    check(cSnapshot?.activityCount == 432 && cSnapshot?.activityCount != aSnapshot?.activityCount, "quick recovery must install only C count values")
    check(cSnapshot?.documentRef == "document_named_quick_C" && cSnapshot?.documentRef != aSnapshot?.documentRef, "quick recovery must install only C document")
    check(cSnapshot?.bundleHash == "sha256:named_quick_C" && cSnapshot?.bundleHash != aSnapshot?.bundleHash, "quick recovery must install only C bundle")
}

runNamedTest("diagnostics_success_unknown_success_uses_only_c") {
    var state = ProductionDiagnosticsClientState()
    let diagnosticsA = productionDiagnosticsSuccessPayload(
        label: "A",
        checkedAt: "2026-07-29T01:00:00Z",
        mode: "quick"
    )
    let transitionA = state.apply(dictionary: diagnosticsA, requestedMode: "quick")
    check(transitionA.accepted && transitionA.shouldAdvanceSuccessfulStatusTime, "diagnostics A must establish a current snapshot")
    check(state.trustedSnapshot?.probeNames == ["probe_A"], "diagnostics A must install A probes")
    check(state.trustedSnapshot?.actionIDs == ["action_A"], "diagnostics A must install A actions")
    check(state.trustedSnapshot?.readOnlyActionCount == 7, "diagnostics A must install A action counts")

    let diagnosticsB = productionDiagnosticsBusyPayload(
        observedAt: "2026-07-29T01:00:03Z",
        lastSuccessfulReadAt: "2026-07-29T01:00:00Z"
    )
    let transitionB = state.apply(dictionary: diagnosticsB, requestedMode: "quick")
    check(transitionB.accepted && transitionB.readState == .busy, "diagnostics B must become unreadable")
    check(transitionB.mustClearCurrentState && transitionB.refreshOnly, "diagnostics B must replace A with refresh-only")
    check(state.trustedSnapshot == nil && state.trustedDictionary == nil, "diagnostics B must clear A probes, actions, counts, and OK state")
    check(state.observedAt == "2026-07-29T01:00:03Z", "diagnostics B must show B observation time")
    check(state.lastSuccessfulReadAt == "2026-07-29T01:00:00Z", "diagnostics B may retain only A success metadata")

    let diagnosticsC = productionDiagnosticsSuccessPayload(
        label: "C",
        checkedAt: "2026-07-29T01:00:06Z",
        mode: "full",
        kind: "warning",
        status: "attention_required"
    )
    let transitionC = state.apply(dictionary: diagnosticsC, requestedMode: "full")
    check(transitionC.accepted && transitionC.readState == .current, "diagnostics C must recover")
    check(state.trustedSnapshot?.probeNames == ["probe_C"], "diagnostics recovery must contain only C probes")
    check(state.trustedSnapshot?.actionIDs == ["action_C"], "diagnostics recovery must contain only C actions")
    check(state.trustedSnapshot?.readOnlyActionCount == 3, "diagnostics recovery must contain only C counts")
    check(state.lastSuccessfulReadAt == "2026-07-29T01:00:06Z", "diagnostics recovery must install C successful time")
}

runNamedTest("unknown_does_not_advance_last_successful_status_time") {
    var state = ProductionStatusClientState()
    _ = state.apply(
        dictionary: productionStatusSuccessPayload(
            label: "timestamp_A",
            observedAt: "2026-07-29T05:00:00Z",
            seed: 1
        ),
        interface: .fullStatus
    )
    let beforeBusy = state.lastSuccessfulReadAt
    let transition = state.apply(
        dictionary: productionStatusBusyPayload(
            observedAt: "2026-07-29T05:00:03Z",
            lastSuccessfulReadAt: beforeBusy ?? NSNull()
        ),
        interface: .fullStatus
    )
    check(!transition.shouldAdvanceSuccessfulStatusTime, "unknown must not advance the success clock")
    check(state.lastSuccessfulReadAt == beforeBusy, "unknown must preserve the prior proven success timestamp")

    var missingCoreTime = productionStatusSuccessPayload(
        label: "missing_time",
        observedAt: "2026-07-29T05:00:06Z",
        seed: 6
    )
    missingCoreTime.removeValue(forKey: "observed_at")
    missingCoreTime.removeValue(forKey: "last_successful_read_at")
    let missingTimeTransition = state.apply(
        dictionary: missingCoreTime,
        interface: .fullStatus
    )
    check(!missingTimeTransition.accepted, "a successful status without a Core-issued UTC time must fail closed")
    check(state.lastSuccessfulReadAt == beforeBusy, "a missing success time must not replace the prior proven success")

    var forgedSuccessTime = productionStatusSuccessPayload(
        label: "forged_time",
        observedAt: "2026-07-29T05:00:09Z",
        seed: 9
    )
    forgedSuccessTime["last_successful_read_at"] = "2026-07-29T04:59:59Z"
    let forgedTimeTransition = state.apply(
        dictionary: forgedSuccessTime,
        interface: .fullStatus
    )
    check(!forgedTimeTransition.accepted, "a successful status may not supply a mismatched last-success time")

    let fractionalSuccess = productionStatusSuccessPayload(
        label: "fractional_time",
        observedAt: "2026-07-29T05:00:12.123456+00:00",
        seed: 12
    )
    let fractionalTransition = state.apply(
        dictionary: fractionalSuccess,
        interface: .fullStatus
    )
    check(fractionalTransition.accepted, "Core UTC timestamps with fractional seconds must remain valid")
}

runNamedTest("unknown_requires_next_full_refresh") {
    var state = ProductionStatusClientState()
    _ = state.apply(
        dictionary: productionStatusSuccessPayload(
            label: "refresh_A",
            observedAt: "2026-07-29T05:10:00Z",
            seed: 2
        ),
        interface: .fullStatus
    )
    let transition = state.apply(
        dictionary: productionStatusBusyPayload(
            observedAt: "2026-07-29T05:10:03Z",
            lastSuccessfulReadAt: "2026-07-29T05:10:00Z"
        ),
        interface: .quickStatus
    )
    check(transition.requiresFullRefresh && state.requiresFullRefresh, "unknown must force the next status request through full")
    check(!state.hasCurrentFullSnapshot, "unknown must invalidate reusable full evidence")
}

runNamedTest("unknown_exposes_refresh_only") {
    var state = ProductionStatusClientState()
    let transition = state.apply(
        dictionary: productionStatusBusyPayload(
            observedAt: "2026-07-29T05:20:03Z",
            lastSuccessfulReadAt: NSNull()
        ),
        interface: .fullStatus
    )
    check(transition.actionAvailability == .refreshOnly, "unknown must expose refresh only")
    check(!state.hasCurrentTrustedState, "unknown must expose no trusted state to a mutating control")
}

runNamedTest("unknown_label_contains_observed_time") {
    var state = ProductionStatusClientState()
    _ = state.apply(
        dictionary: productionStatusBusyPayload(
            observedAt: "2026-07-29T05:30:03Z",
            lastSuccessfulReadAt: NSNull()
        ),
        interface: .fullStatus
    )
    check(state.unknownStatusTextCN.contains("状态未知"), "unknown label must state 状态未知")
    check(state.unknownStatusTextCN.contains("2026-07-29T05:30:03Z"), "unknown label must contain B observation time")
    check(!state.unknownStatusTextCN.contains("正常"), "unknown label must not claim health")
}

runNamedTest("paused_locked_session_unknown_do_not_regress") {
    var state = ProductionStatusClientState()
    let paused = productionStatusSuccessPayload(
        label: "paused_exact",
        observedAt: "2026-07-29T05:40:00Z",
        seed: 3,
        status: "paused"
    )
    let pausedTransition = state.apply(dictionary: paused, interface: .fullStatus)
    check(pausedTransition.readState == .current && pausedTransition.actionAvailability == .captureControls, "paused must stay current with Resume controls")

    let locked = productionStatusSuccessPayload(
        label: "locked_exact",
        observedAt: "2026-07-29T05:40:03Z",
        seed: 4,
        status: "locked",
        captureAllowed: false
    )
    let lockedTransition = state.apply(dictionary: locked, interface: .fullStatus)
    check(lockedTransition.readState == .current && lockedTransition.actionAvailability == .refreshOnly, "locked must stay readable and privacy closed")

    let sessionUnknown = productionStatusSuccessPayload(
        label: "session_unknown_exact",
        observedAt: "2026-07-29T05:40:06Z",
        seed: 5,
        status: "session_state_unknown",
        captureAllowed: false
    )
    let sessionTransition = state.apply(dictionary: sessionUnknown, interface: .fullStatus)
    check(sessionTransition.readState == .current && sessionTransition.actionAvailability == .refreshOnly, "session unknown must stay readable and privacy closed")
}

runNamedTest("diagnostics_busy_does_not_claim_healthy") {
    var state = ProductionDiagnosticsClientState()
    let busyPayload = productionDiagnosticsBusyPayload(
        observedAt: "2026-07-29T05:50:03Z",
        lastSuccessfulReadAt: NSNull()
    )
    _ = state.apply(
        dictionary: busyPayload,
        requestedMode: "full"
    )
    check(state.readState == .busy && state.status == "unknown", "diagnostics busy must not claim OK")
    check(state.trustedSnapshot == nil && state.refreshOnly, "diagnostics busy must expose no old healthy snapshot")

    var missingBoundary = busyPayload
    missingBoundary.removeValue(forKey: "memory_write_actions")
    var missingBoundaryState = ProductionDiagnosticsClientState()
    let missingBoundaryTransition = missingBoundaryState.apply(
        dictionary: missingBoundary,
        requestedMode: "full"
    )
    check(!missingBoundaryTransition.accepted, "diagnostics busy must require every top-level safety gate to be exact false")

    var fabricatedCounts = busyPayload
    fabricatedCounts["actions"] = []
    fabricatedCounts["action_counts"] = [:]
    var fabricatedCountsState = ProductionDiagnosticsClientState()
    let fabricatedCountsTransition = fabricatedCountsState.apply(
        dictionary: fabricatedCounts,
        requestedMode: "full"
    )
    check(!fabricatedCountsTransition.accepted, "diagnostics busy must reject even empty fabricated action metrics")
}

runNamedTest("database_busy_status_preserves_last_success") {
    var state = ProductionDiagnosticsClientState()
    _ = state.apply(
        dictionary: productionDiagnosticsSuccessPayload(
            label: "A",
            checkedAt: "2026-07-29T06:00:00Z",
            mode: "full"
        ),
        requestedMode: "full"
    )
    _ = state.apply(
        dictionary: productionDiagnosticsBusyPayload(
            observedAt: "2026-07-29T06:00:03Z",
            lastSuccessfulReadAt: "2026-07-29T06:00:00Z"
        ),
        requestedMode: "full"
    )
    check(state.observedAt == "2026-07-29T06:00:03Z", "busy must retain its own observation time")
    check(state.lastSuccessfulReadAt == "2026-07-29T06:00:00Z", "busy must preserve only the prior successful time")

    let forgedBusy = productionDiagnosticsBusyPayload(
        observedAt: "2026-07-29T06:00:06Z",
        lastSuccessfulReadAt: "2026-07-29T05:59:59Z"
    )
    let forgedTransition = state.apply(dictionary: forgedBusy, requestedMode: "full")
    check(!forgedTransition.accepted, "busy diagnostics may not install an unproven last-success time")
    check(state.lastSuccessfulReadAt == "2026-07-29T06:00:00Z", "a forged busy timestamp must not replace proven success metadata")

    var fractionalDiagnostics = ProductionDiagnosticsClientState()
    let fractionalSuccess = productionDiagnosticsSuccessPayload(
        label: "fractional",
        checkedAt: "2026-07-29T06:00:09.123456+00:00",
        mode: "quick"
    )
    let fractionalTransition = fractionalDiagnostics.apply(
        dictionary: fractionalSuccess,
        requestedMode: "quick"
    )
    check(fractionalTransition.accepted, "diagnostics must accept Core UTC timestamps with fractional seconds")
}

check(!ContextConsoleFeatureGate.productionEnabled, "Context Console production actions must remain hard disabled")

var contextClient = ContextConsoleClientState()
contextClient.apply(dictionary: [
    "status": "available",
    "reason_code": "",
    "observed_at": "2026-07-29T01:00:00Z",
    "last_successful_read_at": "2026-07-29T01:00:00Z",
    "raw_access": false,
    "actions_enabled": true,
    "counts": [
        "active_verified_contexts": 2,
        "active_unverified_clues": 1,
        "memory_candidates": 0,
    ],
    "verified_context_refs": ["verified_0123456789abcdef"],
    "trusted_context_refs": ["context_0123456789abcdef"],
    "bundle_hash": "sha256:" + String(repeating: "a", count: 64),
])
check(contextClient.hasFreshTrustedSnapshot, "a valid success response must create a fresh Context Console snapshot")
check(contextClient.counts?.activeVerifiedContexts == 2, "a valid success response must decode verified counts")
check(contextClient.verifiedContextRefs == ["verified_0123456789abcdef"], "a valid success response may retain safe opaque verified references")
check(contextClient.bundleHash != nil, "a valid success response may retain its current bundle hash")
check(!contextClient.actionsEnabled, "source-only Context Console actions must stay disabled even on a healthy read")

contextClient.apply(dictionary: [
    "status": "unknown",
    "reason_code": "database_busy",
    "observed_at": "2026-07-29T01:00:03Z",
    "last_successful_read_at": "2026-07-29T01:00:00Z",
    "raw_access": false,
    "actions_enabled": false,
    "counts": NSNull(),
    "service_status": "unknown",
])
check(contextClient.readState == .busy, "database_busy must be represented as a non-healthy busy state")
check(!contextClient.hasFreshTrustedSnapshot, "busy must invalidate the previous fresh snapshot")
check(contextClient.counts == nil, "busy must clear stale counts rather than present zeros")
check(contextClient.verifiedContextRefs.isEmpty, "busy must clear trusted references that may have been revoked")
check(contextClient.trustedContextRefs.isEmpty, "busy must clear trusted projection data")
check(contextClient.bundleHash == nil, "busy must clear the old bundle hash")
check(!contextClient.actionsEnabled, "busy must disable Context Console actions")

contextClient.apply(dictionary: [
    "status": "available",
    "reason_code": "",
    "observed_at": "2026-07-29T01:00:06Z",
    "last_successful_read_at": "2026-07-29T01:00:06Z",
    "raw_access": false,
    "actions_enabled": true,
    "counts": [
        "active_verified_contexts": 1,
        "active_unverified_clues": 0,
        "memory_candidates": 0,
    ],
    "verified_context_refs": ["verified_fedcba9876543210"],
    "trusted_context_refs": [],
    "bundle_hash": "sha256:" + String(repeating: "b", count: 64),
])
check(contextClient.readState == .available, "a fresh successful read must recover from busy")
check(contextClient.hasFreshTrustedSnapshot, "recovery must install the new fresh snapshot")
check(contextClient.counts?.activeVerifiedContexts == 1, "recovery must not reuse the old count")
check(contextClient.verifiedContextRefs == ["verified_fedcba9876543210"], "recovery must use the new opaque references")

var malformedContextClient = ContextConsoleClientState()
malformedContextClient.apply(dictionary: [
    "status": "available",
    "observed_at": "2026-07-29T01:00:00Z",
    "raw_access": true,
    "actions_enabled": true,
    "counts": [
        "active_verified_contexts": 9,
        "active_unverified_clues": 9,
        "memory_candidates": 9,
    ],
])
check(malformedContextClient.readState == .blocked, "raw_access=true must block the Context Console response")
check(!malformedContextClient.hasFreshTrustedSnapshot, "a blocked response must expose no current snapshot")

runNamedTest("phase2_context_contracts_are_fail_closed") {
    let suite = "PAMInteractionModelChecks.phase2.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defer { defaults.removePersistentDomain(forName: suite) }
    check(
        !ContextConsolePhase2FeatureFlag.isEnabled(in: defaults),
        "the Phase 2 UI feature flag must default off"
    )
    var route = Phase2RouteState(destination: .today)
    route.select(.legacy)
    check(route.destination == .legacy, "the complete legacy shell must remain routable")
    route.select(.today)
    check(route.destination == .today, "the Phase 2 shell must remain reachable after legacy rollback")
    let enterFind = Phase2DestinationTransition(current: .today, requested: .find)
    check(
        enterFind.shouldBeginFindFlow && !enterFind.shouldCancelFindBeforeSelection,
        "entering Find must start exactly one flow without running the leave revocation"
    )
    let stayInFind = Phase2DestinationTransition(current: .find, requested: .find)
    let restartFind = Phase2DestinationTransition(
        current: .find,
        requested: .find,
        restartFind: true
    )
    check(
        !stayInFind.shouldBeginFindFlow
            && !stayInFind.shouldCancelFindBeforeSelection
            && restartFind.shouldBeginFindFlow
            && !restartFind.shouldCancelFindBeforeSelection,
        "a repeated route must be inert unless the caller explicitly restarts Find"
    )
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
        check(
            leaveFind.shouldCancelFindBeforeSelection
                && !leaveFind.shouldBeginFindFlow
                && leaveFind.destination == destination,
            "every route away from Find must revoke the old action capability before selection"
        )
    }

    let query = try! ContextSnapshotQuery(
        query: #"PAM "%_ 找回"#,
        includeClues: true,
        limit: 100
    )
    let command = try! ContextConsoleCoreCommands.snapshot(query)
    check(
        command.arguments == ["context-console", "snapshot", "--payload-stdin"],
        "Phase 2 search must use the closed stdin command"
    )
    check(
        !command.arguments.joined(separator: " ").contains("找回"),
        "Phase 2 search text must never enter argv"
    )
    let queryBody = try! JSONSerialization.jsonObject(
        with: command.standardInput ?? Data()
    ) as! [String: Any]
    check(
        queryBody["query"] as? String == #"PAM "%_ 找回"#,
        "Phase 2 search must preserve special characters in JSON stdin"
    )

    var utcCalendar = Calendar(identifier: .gregorian)
    utcCalendar.timeZone = TimeZone(secondsFromGMT: 0)!
    let timestampParser = ISO8601DateFormatter()
    let filterNow = timestampParser.date(from: "2026-07-30T12:00:00Z")!
    let retentionRange = Phase2FindTimePreset.retentionWindow.utcRange(
        now: filterNow,
        calendar: utcCalendar
    )
    check(
        retentionRange.startAt == "2026-05-02T00:00:00Z"
            && retentionRange.endAt == "2026-07-31T00:00:00Z",
        "the 90-day Find preset must become an explicit local-day UTC range"
    )
    let projectRef = "project_" + String(repeating: "a", count: 32)
    let filteredQuery = try! ContextSnapshotQuery(
        projectRef: projectRef,
        startAt: retentionRange.startAt,
        endAt: retentionRange.endAt,
        limit: 100
    )
    let filteredCommand = try! ContextConsoleCoreCommands.snapshot(filteredQuery)
    let filteredBody = try! JSONSerialization.jsonObject(
        with: filteredCommand.standardInput ?? Data()
    ) as! [String: Any]
    check(
        filteredBody["project_ref"] as? String == projectRef
            && filteredBody["start_at"] as? String == retentionRange.startAt
            && filteredBody["end_at"] as? String == retentionRange.endAt,
        "project and time filters must travel together in JSON stdin"
    )
    check(
        !filteredCommand.arguments.joined().contains(projectRef),
        "the selected project reference must not enter argv"
    )

    var quickCapture = QuickCaptureDraft(
        text: "完成 Phase 2 源码候选",
        explicitlyConfirmedOwner: true
    )
    check(quickCapture.isSubmissionReady, "a bounded explicit owner note must be actionable")
    quickCapture.text = String(repeating: "字", count: 180)
    check(quickCapture.isSubmissionReady, "a 180-character owner note must be accepted")
    quickCapture.text = String(repeating: "字", count: 181)
    check(!quickCapture.isSubmissionReady, "a 181-character owner note must be rejected")
    quickCapture.text = "token=abcdefghijklmnopqrstuvwxyz"
    check(!quickCapture.isSubmissionReady, "credential-like quick capture text must fail closed")

    let busyData = try! JSONSerialization.data(
        withJSONObject: [
            "contract_version": "ContextSnapshotV1",
            "status": "busy",
            "observed_at": "2026-07-30T01:00:00Z",
            "last_successful_read_at": NSNull(),
            "actions_enabled": false,
            "counts": NSNull(),
            "context_bundle_v1": NSNull(),
            "projects": NSNull(),
            "legacy_confirmed": NSNull(),
            "confirmation_questions": NSNull(),
            "capabilities": NSNull(),
            "raw_access": false,
        ],
        options: [.sortedKeys]
    )
    let busy = try! ContextSnapshotV1(data: busyData)
    check(busy.readState == .busy, "database_busy must decode as busy")
    check(busy.projects == nil, "busy must carry null project content")
    check(busy.actionsEnabled == false, "busy must disable owner actions")

    let rejectedHandoffData = try! JSONSerialization.data(
        withJSONObject: [
            "contract_version": "LocalHandoffV1",
            "status": "rejected",
            "observed_at": "2026-07-30T01:00:01Z",
            "last_successful_read_at": "2026-07-30T01:00:00Z",
            "handoff_ref": NSNull(),
            "bundle_hash": NSNull(),
            "project": NSNull(),
            "verified_contexts": NSNull(),
            "unresolved_questions": NSNull(),
            "expires_at": NSNull(),
            "revocation_state": NSNull(),
            "raw_access": false,
        ],
        options: [.sortedKeys]
    )
    let rejectedHandoff = try! LocalHandoffV1(data: rejectedHandoffData)
    check(
        rejectedHandoff.readState == .blocked,
        "a business-level handoff rejection must decode without becoming a database outage"
    )
}

runNamedTest("phase2_product_experiment_binding_is_content_free_and_generation_safe") {
    let refA = String(repeating: "a", count: 32)
    let refB = String(repeating: "b", count: 32)
    let refC = String(repeating: "c", count: 32)
    var refreshGeneration = Phase2RefreshGenerationState()
    let firstRefresh = refreshGeneration.begin()
    let secondRefresh = refreshGeneration.begin()
    check(
        !refreshGeneration.isCurrent(firstRefresh)
            && refreshGeneration.isCurrent(secondRefresh),
        "a newer root refresh must invalidate every remaining step of an older chain"
    )
    let statusObject: [String: Any] = [
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
    let statusData = try! JSONSerialization.data(
        withJSONObject: statusObject,
        options: [.sortedKeys]
    )
    let status = try! ProductExperimentStatusV1(data: statusData)
    check(
        status.runningExperimentRef == "experiment_" + refC,
        "only a safe running experiment reference may bind Find feedback"
    )
    check(
        status.attemptCount == 3 && status.completeAttemptCount == 3,
        "the experiment projection must retain content-free denominator counts"
    )
    let command = ContextConsoleCoreCommands.productExperimentStatus()
    check(
        command.arguments == ["context-console", "experiment-status"]
            && command.standardInput == nil,
        "experiment status must not put an experiment or query body in argv"
    )
    let negativeFeedback = ProductFeedbackRequestV1(
        idempotencyKey: "idem_" + refB,
        sourceKind: .localHandoff,
        sourceRef: "handoff_" + refA,
        projectRef: "project_" + refA,
        useful: false,
        resumeSeconds: 12.5,
        experimentRef: "experiment_" + refC
    )
    let feedbackCommand = try! ContextConsoleCoreCommands.productFeedback(negativeFeedback)
    let feedbackBody = try! JSONSerialization.jsonObject(
        with: feedbackCommand.standardInput ?? Data()
    ) as! [String: Any]
    check(
        feedbackCommand.arguments == ["context-console", "product-feedback", "--payload-stdin"]
            && feedbackBody["useful"] as? Bool == false
            && feedbackBody["resume_seconds"] as? Double == 12.5
            && feedbackBody["experiment_ref"] as? String == "experiment_" + refC,
        "not-useful feedback must bind the measured handoff and running experiment through stdin"
    )
    check(
        Phase2ProductFeedbackChoice.useful.useful
            && Phase2ProductFeedbackChoice.useful.title == "有用，继续工作"
            && !Phase2ProductFeedbackChoice.notUseful.useful
            && Phase2ProductFeedbackChoice.notUseful.title == "没帮助",
        "the two visible feedback choices must map to distinct denominator values"
    )
    var feedbackBinding = Phase2ExperimentFeedbackBindingState()
    check(
        feedbackBinding.applyExperimentStatus(status),
        "a running public status must open the feedback binding"
    )
    let exactPending = feedbackBinding.requestForSubmission(
        negativeFeedback,
        choice: .notUseful
    )
    check(
        exactPending == negativeFeedback
            && feedbackBinding.submissionInFlight
            && !feedbackBinding.canSubmit(sourceRef: "handoff_" + refB),
        "crossing the feedback service boundary must globally close new submissions"
    )
    let changedFeedback = ProductFeedbackRequestV1(
        idempotencyKey: "idem_" + refA,
        sourceKind: .localHandoff,
        sourceRef: "handoff_" + refA,
        projectRef: "project_" + refA,
        useful: true,
        resumeSeconds: 12.5,
        experimentRef: "experiment_" + refC
    )
    check(
        feedbackBinding.requestForSubmission(
            changedFeedback,
            choice: .useful
        ) == nil,
        "an in-flight feedback checkpoint must refuse another choice or payload"
    )
    check(
        feedbackBinding.markTransportUnknown(request: negativeFeedback)
            && feedbackBinding.submissionCheckpoint == .transportUnknown,
        "an ambiguous feedback boundary must preserve a transport-unknown checkpoint"
    )
    check(
        feedbackBinding.requestForSubmission(
            changedFeedback,
            choice: .useful
        ) == nil,
        "a transport-unknown feedback retry must refuse a different choice or payload"
    )
    check(
        feedbackBinding.requestForSubmission(
            negativeFeedback,
            choice: .notUseful
        ) == negativeFeedback,
        "a transport-unknown feedback retry must return the exact stored request"
    )
    check(
        feedbackBinding.markRecordedAwaitingStatusRefresh(request: negativeFeedback)
            && feedbackBinding.statusRefreshPending
            && !feedbackBinding.canSubmit(sourceRef: "handoff_" + refB),
        "a recorded receipt must globally close feedback until status refresh"
    )
    check(
        feedbackBinding.applyExperimentStatus(status)
            && !feedbackBinding.statusRefreshPending,
        "only a current running experiment status may reopen another source"
    )

    var renderCheckpoint = Phase2HandoffRenderCheckpointState()
    let oldRender = renderCheckpoint.stage(
        sourceRef: "handoff_" + refA,
        projectRef: "project_" + refA,
        timerGeneration: 90
    )!
    let currentRender = renderCheckpoint.stage(
        sourceRef: "handoff_" + refA,
        projectRef: "project_" + refA,
        timerGeneration: 91
    )!
    check(
        !renderCheckpoint.consume(
            oldRender,
            currentSourceRef: currentRender.sourceRef,
            currentProjectRef: currentRender.projectRef,
            currentTimerGeneration: currentRender.timerGeneration
        ) && renderCheckpoint.pending == currentRender,
        "an old same-ref render callback must not consume a newer timer generation"
    )
    check(
        renderCheckpoint.consume(
            currentRender,
            currentSourceRef: currentRender.sourceRef,
            currentProjectRef: currentRender.projectRef,
            currentTimerGeneration: currentRender.timerGeneration
        ),
        "the exact current handoff render must consume its one-shot timer capability"
    )

    var forbidden = statusObject
    forbidden["reason_code"] = "minimum_window_not_reached"
    let forbiddenData = try! JSONSerialization.data(
        withJSONObject: forbidden,
        options: [.sortedKeys]
    )
    var rejectedForbiddenStatus = false
    do {
        _ = try ProductExperimentStatusV1(data: forbiddenData)
    } catch {
        rejectedForbiddenStatus = true
    }
    check(
        rejectedForbiddenStatus,
        "the Swift experiment projection must reject diagnostic reason codes"
    )

    var terminalObject = statusObject
    terminalObject["status"] = "passed"
    terminalObject["experiment_ref"] = NSNull()
    terminalObject["attempt_count"] = 10
    terminalObject["complete_attempt_count"] = 10
    terminalObject["useful_rate"] = 0.8
    terminalObject["median_resume_seconds"] = 20.0
    let terminalData = try! JSONSerialization.data(
        withJSONObject: terminalObject,
        options: [.sortedKeys]
    )
    let terminalStatus = try! ProductExperimentStatusV1(data: terminalData)
    check(
        terminalStatus.phase == .passed && terminalStatus.runningExperimentRef == nil,
        "terminal experiment status must not expose a reusable experiment reference"
    )
    check(
        !feedbackBinding.applyExperimentStatus(terminalStatus)
            && feedbackBinding.runningExperimentRef == nil
            && feedbackBinding.pendingRetry == nil
            && !feedbackBinding.canSubmit(sourceRef: "handoff_" + refB),
        "the terminal checkpoint must immediately clear the retry and feedback binding"
    )
    _ = feedbackBinding.applyExperimentStatus(status)
    _ = feedbackBinding.requestForSubmission(negativeFeedback, choice: .notUseful)
    feedbackBinding.failClosed()
    check(
        feedbackBinding.runningExperimentRef == nil
            && feedbackBinding.pendingRetry == nil,
        "a parsed rejected or incomplete receipt must fail closed"
    )

    var fabricatedTerminal = terminalObject
    fabricatedTerminal["attempt_count"] = 0
    fabricatedTerminal["complete_attempt_count"] = 0
    fabricatedTerminal["useful_rate"] = NSNull()
    fabricatedTerminal["median_resume_seconds"] = NSNull()
    let fabricatedTerminalData = try! JSONSerialization.data(
        withJSONObject: fabricatedTerminal,
        options: [.sortedKeys]
    )
    var rejectedFabricatedTerminal = false
    do {
        _ = try ProductExperimentStatusV1(data: fabricatedTerminalData)
    } catch {
        rejectedFabricatedTerminal = true
    }
    check(
        rejectedFabricatedTerminal,
        "a terminal experiment without ten complete attempts must fail closed"
    )

    let busyData = try! JSONSerialization.data(
        withJSONObject: [
            "contract_version": "ProductExperimentStatusV1",
            "read_state": "busy",
            "observed_at": "2026-08-01T06:01:00Z",
            "status": NSNull(),
            "experiment_started": NSNull(),
            "experiment_ref": NSNull(),
            "attempt_count": NSNull(),
            "complete_attempt_count": NSNull(),
            "useful_rate": NSNull(),
            "median_resume_seconds": NSNull(),
            "raw_access": false,
        ],
        options: [.sortedKeys]
    )
    let busy = try! ProductExperimentStatusV1(data: busyData)
    var experimentState = Phase2ConsoleViewState()
    let trustedSnapshot = ContextSnapshotV1(
        readState: .available,
        observedAt: "2026-08-01T06:00:30Z",
        lastSuccessfulReadAt: "2026-08-01T06:00:30Z",
        actionsEnabled: true,
        counts: nil,
        bundle: nil,
        projects: [],
        legacyConfirmed: [],
        confirmationCards: [],
        capabilities: nil
    )
    let todayToken = experimentState.beginRequest(for: .today)
    check(
        experimentState.apply(snapshot: trustedSnapshot, for: .today, token: todayToken),
        "the experiment clearing check must begin with trusted Today content"
    )
    let searchToken = experimentState.beginRequest(for: .search)
    check(
        experimentState.apply(snapshot: trustedSnapshot, for: .search, token: searchToken),
        "the experiment clearing check must begin with trusted Find content"
    )
    let memoryToken = experimentState.beginRequest(for: .memory)
    check(
        experimentState.apply(
            memoryCandidates: MemoryCandidateListV1(
                readState: .available,
                observedAt: "2026-08-01T06:00:30Z",
                candidates: [],
                actionsEnabled: false
            ),
            token: memoryToken
        ),
        "the experiment clearing check must begin with trusted Memory content"
    )
    let deliveryToken = experimentState.beginRequest(for: .delivery)
    check(
        experimentState.apply(
            deliveryStatus: DeliveryStatusV1(
                readState: .available,
                observedAt: "2026-08-01T06:00:30Z",
                dailyReport: nil,
                deliveries: [],
                projects: []
            ),
            token: deliveryToken
        ),
        "the experiment clearing check must begin with trusted Delivery content"
    )
    let oldStatus = experimentState.beginRequest(for: .experiment)
    let currentStatus = experimentState.beginRequest(for: .experiment)
    check(
        !experimentState.apply(productExperimentStatus: status, token: oldStatus),
        "an older experiment response must not replace a newer request"
    )
    check(
        experimentState.apply(productExperimentStatus: status, token: currentStatus),
        "the current experiment response must be accepted"
    )
    let busyStatus = experimentState.beginRequest(for: .experiment)
    check(
        experimentState.apply(productExperimentStatus: busy, token: busyStatus)
            && experimentState.readState == .busy
            && !experimentState.actionsEnabled
            && experimentState.snapshot == nil
            && experimentState.searchSnapshot == nil
            && experimentState.memoryCandidates == nil
            && experimentState.deliveryStatus == nil
            && experimentState.productExperimentStatus == nil,
        "busy experiment status must clear every trusted surface and action"
    )

    let unknownData = try! JSONSerialization.data(
        withJSONObject: [
            "contract_version": "ProductExperimentStatusV1",
            "read_state": "available",
            "observed_at": "2026-08-01T06:02:00Z",
            "status": "unknown",
            "experiment_started": NSNull(),
            "experiment_ref": NSNull(),
            "attempt_count": NSNull(),
            "complete_attempt_count": NSNull(),
            "useful_rate": NSNull(),
            "median_resume_seconds": NSNull(),
            "raw_access": false,
        ],
        options: [.sortedKeys]
    )
    let unknown = try! ProductExperimentStatusV1(data: unknownData)
    let restoredToday = experimentState.beginRequest(for: .today)
    check(
        experimentState.apply(snapshot: trustedSnapshot, for: .today, token: restoredToday),
        "the unknown experiment check must begin with restored trusted content"
    )
    let unknownStatus = experimentState.beginRequest(for: .experiment)
    check(
        experimentState.apply(productExperimentStatus: unknown, token: unknownStatus)
            && experimentState.readState == .unknown
            && !experimentState.actionsEnabled
            && experimentState.snapshot == nil
            && experimentState.productExperimentStatus == nil,
        "an available envelope with unknown experiment semantics must fail closed globally"
    )
    let restoredAfterUnknown = experimentState.beginRequest(for: .today)
    check(
        experimentState.apply(
            snapshot: trustedSnapshot,
            for: .today,
            token: restoredAfterUnknown
        ),
        "the thrown experiment-status check must begin with trusted content"
    )
    let responseBeforeFailure = experimentState.beginRequest(for: .search)
    let thrownStatus = experimentState.beginRequest(for: .experiment)
    check(
        experimentState.applyFailure(
            .unavailable,
            observedAt: "2026-08-01T06:03:00Z",
            token: thrownStatus
        )
            && experimentState.readState == .unavailable
            && !experimentState.actionsEnabled
            && experimentState.snapshot == nil
            && experimentState.searchSnapshot == nil
            && experimentState.memoryCandidates == nil
            && experimentState.deliveryStatus == nil
            && !experimentState.apply(
                snapshot: trustedSnapshot,
                for: .search,
                token: responseBeforeFailure
            ),
        "a thrown experiment-status read must clear every trusted surface and invalidate older responses"
    )

    var refreshFailureBinding = Phase2ExperimentFeedbackBindingState()
    _ = refreshFailureBinding.applyExperimentStatus(status)
    let refreshFailureRequest = refreshFailureBinding.requestForSubmission(
        negativeFeedback,
        choice: .notUseful
    )
    refreshFailureBinding.loseTrustPreservingSubmission()
    check(
        refreshFailureRequest == negativeFeedback
            && refreshFailureBinding.submissionInFlight
            && refreshFailureBinding.pendingRetry?.request == negativeFeedback
            && refreshFailureBinding.runningExperimentRef == nil
            && !refreshFailureBinding.canSubmit(sourceRef: "handoff_" + refB),
        "an independent experiment-status failure must clear trust without erasing an in-flight feedback checkpoint"
    )
    check(
        refreshFailureBinding.markRecordedAwaitingStatusRefresh(
            request: negativeFeedback
        )
            && refreshFailureBinding.statusRefreshPending
            && !refreshFailureBinding.canSubmit(sourceRef: "handoff_" + refB),
        "the old feedback result must advance its exact checkpoint after the status refresh fails"
    )
    check(
        !refreshFailureBinding.applyExperimentStatus(busy)
            && refreshFailureBinding.statusRefreshPending
            && refreshFailureBinding.runningExperimentRef == nil
            && !refreshFailureBinding.canSubmit(sourceRef: "handoff_" + refB),
        "busy or unavailable status must preserve a recorded-awaiting-status checkpoint globally closed"
    )
    check(
        refreshFailureBinding.applyExperimentStatus(status)
            && !refreshFailureBinding.statusRefreshPending
            && refreshFailureBinding.canSubmit(sourceRef: "handoff_" + refB),
        "only a later trusted current experiment status may resolve the preserved checkpoint"
    )

    let projectA = "project_" + refA
    let projectB = "project_" + refB
    let sourceA = "handoff_" + refA
    var timer = Phase2ResumeTimerState()
    let commandKGeneration = timer.beginFindEntry(atUptimeNanoseconds: 500_000_000)
    check(
        timer.beginSearch(atUptimeNanoseconds: 1_500_000_000) == commandKGeneration,
        "executing a search must preserve the earlier Command-K timer generation"
    )
    check(timer.select(projectRef: projectA), "the Command-K attempt must bind its first project")
    check(
        timer.freeze(
            sourceRef: sourceA,
            projectRef: projectA,
            atUptimeNanoseconds: 2_500_000_000,
            generation: commandKGeneration
        )?.resumeSeconds == 2 && !timer.isActive,
        "resume time must include query-entry time and freeze once the handoff is visible"
    )
    let firstGeneration = timer.begin(atUptimeNanoseconds: 1_000_000_000)
    check(timer.select(projectRef: projectA), "the first project must bind the active Find timer")
    let measurement = timer.freeze(
        sourceRef: sourceA,
        projectRef: projectA,
        atUptimeNanoseconds: 4_250_000_000,
        generation: firstGeneration
    )
    check(
        measurement?.resumeSeconds == 3.25,
        "resume time must be derived from monotonic uptime"
    )
    let staleGeneration = timer.beginFindEntry(atUptimeNanoseconds: 10_000_000_000)
    check(
        timer.beginSearch(atUptimeNanoseconds: 11_000_000_000) == staleGeneration,
        "the first search after entering Find must preserve query-entry time"
    )
    let currentGeneration = timer.beginSearch(atUptimeNanoseconds: 20_000_000_000)
    check(
        currentGeneration != staleGeneration,
        "a newer search must invalidate the earlier search timing generation"
    )
    check(timer.select(projectRef: projectA), "the replacement Find timer must bind independently")
    check(
        timer.freeze(
            sourceRef: sourceA,
            projectRef: projectA,
            atUptimeNanoseconds: 21_000_000_000,
            generation: staleGeneration
        ) == nil,
        "a stale handoff must not freeze the current Find timer"
    )
    check(
        timer.freeze(
            sourceRef: sourceA,
            projectRef: projectA,
            atUptimeNanoseconds: 22_000_000_000,
            generation: currentGeneration
        )?.resumeSeconds == 2,
        "the current handoff must freeze the current Find timer"
    )
    _ = timer.begin(atUptimeNanoseconds: 30_000_000_000)
    check(timer.select(projectRef: projectA), "a project may bind once per Find attempt")
    check(
        !timer.select(projectRef: projectB) && timer.measurement == nil,
        "switching projects must clear the previous timing attribution"
    )

    var resultSelection = Phase2SearchResultSelectionState()
    resultSelection.accept(
        timerGeneration: staleGeneration,
        projectRefs: [projectA]
    )
    check(
        resultSelection.canSelect(
            timerGeneration: staleGeneration,
            projectRef: projectA
        ),
        "an accepted search result may bind only its own timer generation"
    )
    resultSelection.beginSearch()
    check(
        !resultSelection.canSelect(
            timerGeneration: currentGeneration,
            projectRef: projectA
        ),
        "beginning Search B must disable every Search A card immediately"
    )
    resultSelection.accept(
        timerGeneration: currentGeneration,
        projectRefs: [projectB]
    )
    check(
        !resultSelection.canSelect(
            timerGeneration: currentGeneration,
            projectRef: projectA
        )
            && resultSelection.canSelect(
                timerGeneration: currentGeneration,
                projectRef: projectB
            ),
        "a delayed Search A click must not attribute Search B's timer"
    )
}

runNamedTest("phase2_cross_channel_responses_cannot_repopulate_stale_content") {
    let available = ContextSnapshotV1(
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
    let handoff = LocalHandoffV1(
        readState: .available,
        observedAt: "2026-07-30T01:00:30Z",
        handoffRef: "handoff_" + String(repeating: "a", count: 32),
        bundleHash: "sha256:" + String(repeating: "b", count: 64),
        project: ContextLabelV1(
            ref: "project_" + String(repeating: "c", count: 32),
            displayLabel: "PAM"
        ),
        objective: "继续 Phase 2",
        results: [],
        blockers: [],
        nextSteps: [],
        sources: [],
        unresolvedQuestions: [],
        expiresAt: "2026-07-31T01:00:30Z",
        revoked: false
    )

    var refreshState = Phase2ConsoleViewState()
    let oldSearch = refreshState.beginRequest(for: .search)
    refreshState.invalidateAllRequests()
    let refresh = refreshState.beginRequest(for: .today)
    check(
        refreshState.apply(snapshot: busy, for: .today, token: refresh),
        "the current busy refresh must be accepted"
    )
    check(
        !refreshState.apply(snapshot: available, for: .search, token: oldSearch),
        "a search response started before refresh must not repopulate content"
    )
    check(
        refreshState.readState == .busy && refreshState.searchSnapshot == nil,
        "busy refresh must remain authoritative after an old search finishes"
    )

    var runtimeState = Phase2ConsoleViewState()
    let trustedBeforeRuntimeLoss = runtimeState.beginRequest(for: .today)
    check(
        runtimeState.apply(
            snapshot: available,
            for: .today,
            token: trustedBeforeRuntimeLoss
        ),
        "runtime trust-loss coverage must begin with trusted content"
    )
    let responseBeforeRuntimeLoss = runtimeState.beginRequest(for: .search)
    check(
        runtimeState.applyRuntimeTrustLoss(
            .unavailable,
            observedAt: "2026-08-01T07:01:00Z"
        )
            && runtimeState.readState == .unavailable
            && !runtimeState.actionsEnabled
            && runtimeState.snapshot == nil
            && runtimeState.searchSnapshot == nil
            && !runtimeState.apply(
                snapshot: available,
                for: .search,
                token: responseBeforeRuntimeLoss
            ),
        "an independent runtime-status trust loss must clear all content and invalidate old responses"
    )
    check(
        !runtimeState.applyRuntimeTrustLoss(
            .available,
            observedAt: "2026-08-01T07:02:00Z"
        ) && runtimeState.snapshot == nil,
        "runtime recovery must require a fresh Context read instead of restoring old content"
    )

    var handoffFailureState = Phase2ConsoleViewState()
    let trustedBeforeHandoffFailure = handoffFailureState.beginRequest(for: .today)
    check(
        handoffFailureState.apply(
            snapshot: available,
            for: .today,
            token: trustedBeforeHandoffFailure
        ),
        "handoff failure coverage must begin with trusted content"
    )
    let unavailableHandoff = LocalHandoffV1(
        readState: .busy,
        observedAt: "2026-08-01T07:03:00Z",
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
    let unavailableHandoffToken = handoffFailureState.beginRequest(for: .handoff)
    check(
        handoffFailureState.apply(
            localHandoff: unavailableHandoff,
            token: unavailableHandoffToken
        )
            && handoffFailureState.readState == .busy
            && !handoffFailureState.actionsEnabled
            && handoffFailureState.snapshot == nil,
        "a nonavailable handoff response must clear the entire trusted console"
    )

    var searchState = Phase2ConsoleViewState()
    let today = searchState.beginRequest(for: .today)
    check(
        searchState.apply(snapshot: available, for: .today, token: today),
        "the initial available snapshot must be accepted"
    )
    let oldHandoff = searchState.beginRequest(for: .handoff)
    searchState.invalidateRequests([.handoff])
    let newSearch = searchState.beginRequest(for: .search)
    check(
        searchState.apply(snapshot: available, for: .search, token: newSearch),
        "the current search response must be accepted"
    )
    check(
        !searchState.apply(localHandoff: handoff, token: oldHandoff),
        "a handoff started before a new search must not replace its preview"
    )
    check(
        searchState.localHandoff == nil && searchState.searchSnapshot != nil,
        "the new search must stay visible after the old handoff finishes"
    )
}

runNamedTest("phase2_system_health_uses_one_three_state_policy") {
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

    check(resolve() == .normal, "a known healthy snapshot must resolve to normal")
    check(resolve(unknown: true) == .error, "unknown must map to menu error")
    check(resolve(danger: true) == .error, "known danger must resolve to error")
    check(resolve(captureError: true) == .error, "capture errors must resolve to error")
    check(resolve(running: false) == .error, "a loaded stopped agent must resolve to error")
    check(resolve(stale: true) == .error, "stale capture must resolve to error")
    check(
        resolve(status: "paused") == .paused,
        "an intentional pause must resolve to paused"
    )
    check(
        resolve(status: "stopped", loaded: false, running: false) == .paused,
        "an intentionally stopped unloaded recorder must not be shown as normal"
    )
    check(
        resolve(status: "capturing", loaded: false, running: false) == .normal,
        "a directly capturing process may be healthy without a loaded launch agent"
    )
    check(
        resolve(loaded: false, running: true) == .error,
        "an incoherent unloaded-but-running service state must fail closed"
    )
    check(resolve(status: "error") == .error, "an explicit Core error must fail closed")
    check(
        resolve(status: "session_state_unknown") == .error,
        "an unknown session state must fail closed"
    )
    check(
        resolve(status: "stopped") == .error,
        "a stopped status that conflicts with a running service must fail closed"
    )
    check(
        resolve(status: "unexpected_status") == .error,
        "an unrecognized production status must fail closed"
    )
    check(
        resolve(status: "paused", stale: true) == .paused,
        "natural freshness staleness must not turn an intentional pause into an error"
    )
    check(
        resolve(status: "locked", stale: true) == .paused,
        "a locked session must remain an intentional pause"
    )
    check(
        resolve(status: "paused", danger: true) == .error,
        "a hard danger must not be hidden by a paused status"
    )
    check(
        resolve(danger: true, privacyPause: true) == .error,
        "a hard danger must not be hidden by a privacy pause"
    )
}

runNamedTest("phase2_presentation_copy_never_exposes_reason_codes") {
    let diagnostic = "private_internal_reason_42"
    for state in [
        Phase2ReadState.busy,
        .unknown,
        .unavailable,
        .blocked,
    ] {
        let copy = Phase2PresentationCopy.unavailableMessage(for: state)
        check(
            !copy.contains(diagnostic)
                && !copy.contains("reason")
                && !copy.contains("database_busy")
                && !copy.localizedCaseInsensitiveContains("busy")
                && !copy.contains("数据库")
                && !copy.isEmpty,
            "ordinary unavailable copy must come from a closed human-readable mapping"
        )
    }
}

runNamedTest("phase2_public_projection_rejects_paths_and_secrets") {
    for unsafeValue in [
        "file:///" + "Users/example/private.txt",
        "/" + "Users/example/private.txt",
        "token=abcdefghijklmnopqrstuvwxyz",
        #"{"raw":"json"}"#,
        #"[1,2,3]"#,
    ] {
        let payload: [String: Any] = [
            "contract_version": "ContextSnapshotV1",
            "status": "busy",
            "observed_at": "2026-07-30T01:00:00Z",
            "last_successful_read_at": NSNull(),
            "actions_enabled": false,
            "counts": NSNull(),
            "context_bundle_v1": NSNull(),
            "projects": NSNull(),
            "legacy_confirmed": NSNull(),
            "confirmation_questions": NSNull(),
            "capabilities": NSNull(),
            "raw_access": false,
            "note": unsafeValue,
        ]
        let data = try! JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        check(
            (try? ContextSnapshotV1(data: data)) == nil,
            "Swift must reject path, URL, and secret-like residue anywhere in a public projection"
        )
    }
    let diagnosticLeak: [String: Any] = [
        "contract_version": "ContextSnapshotV1",
        "status": "busy",
        "reason_code": "database_busy",
        "observed_at": "2026-07-30T01:00:00Z",
        "last_successful_read_at": NSNull(),
        "actions_enabled": false,
        "counts": NSNull(),
        "context_bundle_v1": NSNull(),
        "projects": NSNull(),
        "legacy_confirmed": NSNull(),
        "confirmation_questions": NSNull(),
        "capabilities": NSNull(),
        "raw_access": false,
    ]
    let diagnosticData = try! JSONSerialization.data(
        withJSONObject: diagnosticLeak,
        options: [.sortedKeys]
    )
    check(
        (try? ContextSnapshotV1(data: diagnosticData)) == nil,
        "Swift must reject Core engineering reason codes in the Phase 2 public projection"
    )
}

if let fixturePath = ProcessInfo.processInfo.environment["PAM_PHASE2_SNAPSHOT_FIXTURE"],
   !fixturePath.isEmpty {
    runNamedTest("phase2_real_core_snapshot_fixture") {
        let data = try! Data(contentsOf: URL(fileURLWithPath: fixturePath))
        let snapshot = try! ContextSnapshotV1(data: data)
        check(snapshot.readState == .available, "the real Core fixture must be available")
        check(snapshot.actionsEnabled, "the enabled real Core fixture must expose governed actions")
        check(snapshot.bundle != nil, "the real Core fixture must include a validated public bundle")
        check(
            (snapshot.bundle?.verifiedContexts.count ?? 0) >= 1,
            "the real Core fixture must contain the explicitly confirmed manual note"
        )
        check(
            snapshot.bundle?.unverifiedClues.isEmpty == true,
            "the verified-only real fixture must not invent screen or OCR clues"
        )
    }
}

if let fixturePath = ProcessInfo.processInfo.environment[
    "PAM_PRODUCT_EXPERIMENT_STATUS_FIXTURE"
], !fixturePath.isEmpty {
    runNamedTest("phase2_real_core_product_experiment_fixture") {
        let data = try! Data(contentsOf: URL(fileURLWithPath: fixturePath))
        let status = try! ProductExperimentStatusV1(data: data)
        check(
            status.readState == .available,
            "the real Core product experiment fixture must be readable"
        )
        check(
            status.phase?.isRunning == true && status.runningExperimentRef != nil,
            "the real Core fixture must expose only its safe running experiment binding"
        )
    }
}

if let fixturePath = ProcessInfo.processInfo.environment[
    "PAM_PRODUCT_EXPERIMENT_UNAVAILABLE_FIXTURE"
], !fixturePath.isEmpty {
    runNamedTest("phase2_real_core_unavailable_experiment_fixture") {
        let data = try! Data(contentsOf: URL(fileURLWithPath: fixturePath))
        let status = try! ProductExperimentStatusV1(data: data)
        check(
            status.readState != .available,
            "the unavailable Core fixture must retain its explicit read state"
        )
        check(
            status.phase == nil
                && status.experimentRef == nil
                && status.runningExperimentRef == nil,
            "an unavailable Core fixture must not retain experiment content or actions"
        )
    }
}


runNamedTest("core_output_error_classifier_matrix") {
    let busyJSON = "{\"status\":\"error\",\"error\":\"database_busy\",\"raw_access\":false}"
    let missingJSON = "{\"status\":\"error\",\"error\":\"database_missing\",\"raw_access\":false}"
    check(
        CoreOutputErrorClassifier.classify("personal-ai-monitor-core exited with 1\n" + busyJSON) == .databaseBusy,
        "a wrapped busy payload must classify as databaseBusy"
    )
    check(
        CoreOutputErrorClassifier.classify(busyJSON) == .databaseBusy,
        "a bare busy payload must classify as databaseBusy"
    )
    check(
        CoreOutputErrorClassifier.classify("personal-ai-monitor-core exited with 1\n" + missingJSON) == .databaseMissing,
        "a wrapped missing-database payload must classify as databaseMissing"
    )
    check(
        CoreOutputErrorClassifier.classify("personal-ai-monitor-core timed out after 8s\npartial output") == .timedOut,
        "a timeout wrapper without JSON must classify as timedOut"
    )
    check(
        CoreOutputErrorClassifier.classify("personal-ai-monitor-core timed out after 8s\n" + busyJSON) == .databaseBusy,
        "a timeout wrapper carrying a busy payload must surface databaseBusy"
    )
    check(
        CoreOutputErrorClassifier.classify("personal-ai-monitor-core exited with 2\nusage: __main__.py") == .commandFailed,
        "an argparse usage failure must classify as commandFailed"
    )
    check(
        CoreOutputErrorClassifier.classify("Could not run personal-ai-monitor-core: no python") == .commandFailed,
        "a spawn failure must classify as commandFailed"
    )
    check(
        CoreOutputErrorClassifier.classify("{\"status\":\"ok\",\"cards\":[]}") == nil,
        "a healthy payload must not classify as an error"
    )
    check(
        CoreOutputErrorClassifier.classify("") == nil,
        "empty output must not classify as an error"
    )
    check(
        CoreOutputErrorClassifier.classify("personal-ai-monitor-core exited with 1\n{\"status\":\"error\",\"error\":\"command_failed\",\"raw_access\":false}") == .commandFailed,
        "a generic wrapped CLI failure must classify as commandFailed"
    )
}

do {
    var reveal = MemoryRevealSession()
    let first = reveal.begin("a")
    check(reveal.state == "loading" && reveal.text.isEmpty, "body panel opens in a clear loading state")
    reveal.receive("{\"memory_id\":\"a\",\"text\":\"first\",\"applies_to\":\"personal\"}", token: first)
    check(reveal.state == "ready" && reveal.text == "first", "first row body appears in the panel")
    let middle = reveal.begin("middle")
    check(reveal.text.isEmpty && reveal.scope.isEmpty, "switching a row clears prior private content immediately")
    reveal.receive("{\"memory_id\":\"a\",\"text\":\"late first\"}", token: first)
    check(reveal.state == "loading" && reveal.text.isEmpty, "slow obsolete request cannot overwrite a newer selection")
    reveal.receive("{\"memory_id\":\"middle\",\"text\":\"middle body\"}", token: middle)
    check(reveal.text == "middle body", "middle row receives its own body")
    let last = reveal.begin("last")
    reveal.receive("{\"error\":\"chroma_missing\"}", token: last)
    check(reveal.state == "failed" && reveal.text.contains("来源"), "failed body reads show a safe actionable reason")
    let retry = reveal.begin("last")
    reveal.receive("{\"memory_id\":\"last\",\"text\":\"\"}", token: retry)
    check(reveal.state == "empty", "empty body is distinct from an error")
    let finalRequest = reveal.begin("last")
    reveal.close()
    reveal.receive("{\"memory_id\":\"last\",\"text\":\"late private body\"}", token: finalRequest)
    check(reveal.state == "closed" && reveal.text.isEmpty && reveal.identifier.isEmpty, "close or lock invalidates an in-flight body read")
    let mismatch = reveal.begin("a")
    reveal.receive("{\"memory_id\":\"b\",\"text\":\"wrong record\"}", token: mismatch)
    check(reveal.state == "failed" && !reveal.text.contains("wrong record"), "identity mismatch never exposes another body")
    let oldPanel = UUID(), newPanel = UUID()
    reveal.begin("same-record", presentationID: oldPanel)
    let newerRead = reveal.begin("same-record", presentationID: newPanel)
    check(!reveal.close(presentationID: oldPanel), "a late sheet dismissal cannot close a newer presentation of the same record")
    reveal.receive("{\"memory_id\":\"same-record\",\"text\":\"current panel\"}", token: newerRead)
    check(reveal.text == "current panel" && reveal.presentationID == newPanel, "the new panel retains its own body after old dismissal")
    let retryRead = reveal.begin("same-record", presentationID: newPanel)
    check(reveal.presentationID == newPanel && retryRead != newerRead, "retry replaces the request while preserving panel ownership")
    check(reveal.close(presentationID: newPanel), "the owning panel clears its temporary body")
    reveal.receive("{\"memory_id\":\"same-record\",\"text\":\"late retry\"}", token: retryRead)
    check(reveal.state == "closed" && reveal.text.isEmpty, "closed panel still rejects a late retry")

    let base: [String:Any] = ["interface":"attention-center", "contract":1, "raw_access":false, "status":"ok", "decision_count":0, "decisions":[], "systems":[], "resources":[]]
    func snapshot(_ value: [String:Any]) -> AttentionSnapshot {
        AttentionSnapshot(jsonText: String(data:try! JSONSerialization.data(withJSONObject:value),encoding:.utf8)!)
    }
    check(snapshot(base).decisionCount == 0 && snapshot(base).badge == nil, "only a valid zero snapshot clears the badge")
    var partial = base; partial["status"] = "partial"; partial["decision_count"] = NSNull()
    check(snapshot(partial).decisionCount == nil && snapshot(partial).countLabel == "暂不可读", "failed reads must not display a zero count")
    var malformed = base; malformed["systems"] = [["title":"incomplete"]]
    check(snapshot(malformed).status == "unknown", "malformed system rows cannot silently disappear")
    var mismatchCount = base; mismatchCount["decision_count"] = 777
    check(snapshot(mismatchCount).status == "unknown", "evidence totals cannot masquerade as a decision count")
    let issue: [String:Any] = ["id":"memory-one", "kind":"memory", "title":"时效待核对", "member_count":2, "members":[["id":"one"],["id":"two"]]]
    var grouped = base; grouped["decision_count"] = 1; grouped["decisions"] = [issue]
    check(snapshot(grouped).countLabel == "1 组" && snapshot(grouped).badge == "1", "home and navigation use the same grouped decision unit")
}

do {
    check(LocalStatusTime.date("2026-09-20T05:00:47.000000+00:00") == LocalStatusTime.date("2026-09-20T05:00:47Z"), "fractional UTC timestamps parse to the same local instant")
    check(!LocalStatusTime.label("2026-09-20T05:00:47.000000+00:00").contains("T05:"), "native status does not leak raw timestamp formatting")
    check(LocalStatusTime.label("broken") == "时间暂不可读", "invalid timestamp is explicitly unavailable")
    for state in ["paused", "capturing", "stopped", "idle"] {
        check(RecordingAvailability.allows(status: state, trusted: true, serviceKnown: true), "fresh record control is independent of screen enablement: \(state)")
    }
    for state in ["unknown", "session_state_unknown", "locked", "busy", "unavailable"] {
        check(!RecordingAvailability.allows(status: state, trusted: true, serviceKnown: true), "uncertain or locked record control stays blocked: \(state)")
    }
    check(!RecordingAvailability.allows(status: "paused", trusted: false, serviceKnown: true), "stale recording state cannot allow mutation")
    var value: [String:Any] = ["state":"current", "raw_access":false, "incident_count":176, "analyzed_message_count":400, "pending_message_count":0, "decision_count":166, "installed_count":0, "verified_count":0]
    check(FeedbackProgress(value)?.decisions == 166, "feedback decisions are separate from analyzed messages")
    check(FeedbackProgress(value)?.lastSuccessLabel == "尚无成功分析记录", "no success timestamp never becomes a fabricated success")
    value["verified_count"] = 1
    check(FeedbackProgress(value) == nil, "verified cannot exceed installed")
    value["verified_count"] = 0; value["pending_message_count"] = -1
    check(FeedbackProgress(value) == nil, "invalid progress is unavailable, not zero")
    var coverage = completeCoverage()
    coverage["pending_event_count"] = 5; coverage["resume_pending"] = false
    coverage["is_complete"] = false; coverage["coverage_status"] = "partial"
    var filePayload: [String:Any] = ["source":"filesystem_confirmed", "raw_access":false, "coverage":coverage, "totals":completeTotals(distinct: 0), "items":[], "limitations":["coverage_incomplete"]]
    let processing = DocumentChangesSnapshot(dictionary:filePayload)
    check(processing.coverageLabel == "新变化处理中" && !processing.coverage.isComplete, "pending changes are not a fabricated coverage gap or complete result")
    coverage["pending_event_count"] = 0; coverage["event_gap_count"] = 1
    filePayload["coverage"] = coverage
    check(DocumentChangesSnapshot(dictionary:filePayload).coverageLabel == "存在记录缺口", "real gap remains visible")
    coverage["event_gap_count"] = 0; filePayload["coverage"] = coverage
    filePayload["limitations"] = ["requested_range_not_fully_covered"]
    check(DocumentChangesSnapshot(dictionary:filePayload).coverageLabel == "时段覆盖不完整", "historical time coverage is distinct from current pending work")
    let memory: [String:Any] = ["id":"m", "kind":"memory", "title":"memory", "member_count":1, "members":[["id":"one"]]]
    let feedback: [String:Any] = ["id":"f", "kind":"feedback", "title":"feedback", "member_count":166]
    let payload: [String:Any] = ["interface":"attention-center", "contract":1, "raw_access":false, "status":"ok", "decision_count":2, "memory_pending":1, "decisions":[memory,feedback], "systems":[], "resources":[]]
    let snapshot = AttentionSnapshot(jsonText:String(data:try! JSONSerialization.data(withJSONObject:payload),encoding:.utf8)!)
    check(snapshot.summaryLabel.contains("记忆 1 组") && snapshot.summaryLabel.contains("166"), "mixed home summary represents both memory groups and feedback decisions")
}

runNamedTest("memory coverage states") {
    check(MemoryCoveragePresentation.lines(nil).first?.contains("暂不可读取") == true, "missing coverage is unknown")
    var rows: [[String: Any]] = ["mem0", "business", "agi"].map { ["source": $0, "known": 3, "states": ["judged": 1, "local_only": 1, "integrity_gap": 1], "inventory_complete": true, "fresh": true] }
    let lines = MemoryCoveragePresentation.lines(["available": true, "sources": rows])
    check(lines.count == 3 && lines.allSatisfy { $0.contains("缺口 1") && $0.contains("已采用 0") }, "judged is not adopted and gaps remain")
    rows[0]["fresh"] = false
    check(MemoryCoveragePresentation.lines(["available": true, "sources": rows])[0].contains("待复核"), "stale coverage cannot claim current")
    rows[1]["source"] = "mem0"
    check(MemoryCoveragePresentation.lines(["available": true, "sources": rows]).first?.contains("不完整") == true, "duplicate sources are rejected")
}

runNamedTest("Jev permits governance and actual benefit") {
    let rows: [[String: Any]] = ["mem0", "business", "agi"].map { ["source": $0, "known": 1, "states": ["local_only": 1], "inventory_complete": true, "fresh": true] }
    check(MemoryCoveragePresentation.lines(["available": true, "sources": rows, "ranking_mode": "off"]).last?.contains("已停用") == true, "off mode is explicit despite retained historic permits")
    check(MemoryCoveragePresentation.lines(["available": true, "sources": rows, "ranking_mode": "assist"]).last?.contains("已启用") == true, "assist is distinct from off")
    check(MemoryCoveragePresentation.lines(["available": true, "sources": rows, "ranking_mode": "unavailable"]).last?.contains("暂不可确认") == true, "unknown mode cannot claim disabled")
    let raw: [String: Any] = ["available": true, "sources": rows, "permits": ["available": true, "fresh": true, "valid": 0, "expiring_48h": 0, "fallback_reasons": ["expired": 7]], "governance": ["available": true, "fresh": true, "legacy_total": 3, "categories": ["existing_decision": 1, "direct_source": 1, "missing_source": 1], "waiting_count": 2]]
    let lines = MemoryCoveragePresentation.lines(raw).joined(separator: "\n")
    check(lines.contains("有效 0") && lines.contains("到期 7"), "expired permits show fallback without service activation implication")
    check(lines.contains("已有有效决定 1") && lines.contains("待核实 1") && lines.contains("缺依据 1"), "legacy format does not erase existing reviews")
    check(MemoryCoveragePresentation.lines(["available": true, "sources": rows, "permits": ["available": true, "fresh": false]]).last?.contains("暂不可核对") == true, "stale permits are unknown")
    check(JevEffectivenessPresentation.lines(nil)[0].contains("尚无"), "missing outcome data is unknown")
    check(JevEffectivenessPresentation.lines(["changed_batches": 0, "advanced_processed": 0, "advanced_verified": 0]).joined().contains("尚无证据"), "zero natural outcomes do not claim benefit")
    check(JevEffectivenessPresentation.lines(["changed_batches": 1, "advanced_processed": 1, "advanced_verified": 1]).joined().contains("尚未证明"), "observed outcome is not causal benefit")
}

check(AppTab.reflection.title == "思考与反思", "reflection has a readable independent page")
do {
    var reads = AttentionRefreshReads()
    let first = reads.request(coalescingIfLoading: true)!
    check(reads.request(coalescingIfLoading: true) == nil && !reads.pending, "automatic hints share the in-flight read")
    check(reads.request() == nil && reads.pending, "explicit refresh or write retains a required new read")
    check(reads.finish(first), "required follow-up survives coalesced automatic hints")
    let next = reads.request()!
    check(!reads.finish(first) && reads.isCurrent(next), "late completion cannot release a newer read")
    reads.invalidate()
    check(!reads.isCurrent(next) && !reads.loading && !reads.pending, "source withdrawal revokes all old read work")
    let current = reads.request(coalescingIfLoading: true)!
    check(!reads.finish(next) && reads.isCurrent(current), "late withdrawn work cannot change a fresh read")
    check(!reads.finish(current) && !reads.loading, "an automatic-only chain finishes without duplicate work")
}
do {
    var cache = AttentionHandoffDraftCache()
    let oldPane = cache.lease(for: "retired"), personal = cache.lease(for: "personal")
    cache.save("Generic old draft", revision: 1, using: oldPane)
    cache.save("Personal input", revision: 2, using: personal)
    cache.withdraw("retired")
    check(cache.draft(using: oldPane) == nil && !cache.isCurrent(oldPane), "source withdrawal removes a loaded display draft")
    check(!cache.save("Delayed save", revision: 3, using: oldPane), "a delayed save cannot repopulate a withdrawn source")
    check(!cache.save("Disappearing pane", revision: 4, using: oldPane), "closing an old pane cannot restore a withdrawn draft")
    check(cache.draft(using: personal)?.text == "Personal input", "source withdrawal preserves personal draft siblings")
    let reopened = cache.lease(for: "retired")
    check(cache.draft(using: reopened) == nil && !cache.save("Late prior pane", revision: 5, using: oldPane), "reopening cannot revive a prior source lease")
    check(!AttentionHandoffReceipt.sourceWithdrawn(["error":"source_not_in_display_scope", "outcome_unknown":true]), "an ordinary unread receipt retains input and cannot masquerade as withdrawal")
    for reason in ["source_not_in_display_scope", "display_scope_unavailable"] {
        check(AttentionHandoffReceipt.sourceWithdrawn(["interface":"attention-handoff", "status":"unavailable", "reason":reason]), "explicit source withdrawal clears only display state")
    }
    cache.withdrawAll()
    check(!cache.save("Late personal pane", revision: 6, using: personal), "scope replacement invalidates all pending display saves")
    var snapshot = AttentionSnapshot()
    snapshot.status = "ok"; snapshot.decisionCount = 2
    snapshot.decisions = [AttentionIssue(["id":"retired", "kind":"closure", "title":"Generic old item"])!, AttentionIssue(["id":"personal", "kind":"closure", "title":"Personal choice"])!]
    let withdrawn = snapshot.withdrawingItem("retired")
    check(withdrawn.decisions.map(\.id) == ["personal"] && withdrawn.badge == nil && withdrawn.status == "partial", "withdrawn item leaves no current row or verified zero count")
    var generations = AttentionRefreshReads()
    let lateRead = generations.request()!
    generations.invalidate()
    var displayed = withdrawn
    if generations.isCurrent(lateRead) { displayed = snapshot }
    check(displayed.workbenchItem("retired") == nil && displayed.badge == nil, "a snapshot already in transit cannot revive a withdrawn source")
}
do {
    var old = AttentionSnapshot()
    old.decisions = [AttentionIssue(["id":"retired", "kind":"closure", "title":"Former company item"])!]
    old.displayScopeRevision = "same-scope"
    var partial = AttentionSnapshot()
    partial.status = "partial"; partial.displayScopeRevision = "same-scope"
    let scoped = partial.retainingUnreadRows(from: old)
    check(scoped.decisions.isEmpty && scoped.decisionCount == nil, "partial scoped refresh cannot revive unverified old company rows")
}
check(AppTab.dailyTabs.contains(.reflection) && !AppTab.managementTabs.contains(.reflection), "reflection is a daily user value")

do {
    let thread = "12345678-1234-1234-1234-123456789abc"
    let base: [String: Any] = ["id":"closure:fixture", "kind":"closure", "title":"需要核对", "state":"decision", "thread_id":NSNull(), "codex_thread_id":thread, "codex_handoff_available":false]
    check(AttentionIssue(base)?.closureThreadID == thread, "owner-derived direct conversation remains available without a dispatch thread")
    for invalid in ["", "12345678-1234-1234-1234-123456789abc?prompt=run", "../../anything", "WORKBUDDY:12345678-1234-1234-1234-123456789abc"] {
        var value = base; value["codex_thread_id"] = invalid
        check(AttentionIssue(value)?.closureThreadID == "", "malformed conversation cannot become a navigation URL")
    }
    var first = base; first["codex_thread_id"] = NSNull(); first["codex_handoff_available"] = true
    check(AttentionIssue(first)?.closureCanHandoff == true && AttentionIssue(first)?.closureThreadID == "", "first user handoff is separate from existing-thread navigation")
    var unknown = first; unknown.removeValue(forKey: "codex_handoff_available")
    check(AttentionIssue(unknown)?.closureCanHandoff == false, "missing creation eligibility cannot enable a new handoff")
    var assistance = base; assistance["codex_assistance_available"] = true; assistance["codex_assistance_token"] = "bound-attempt"
    check(AttentionIssue(assistance)?.closureCanAssist == true && AttentionIssue(assistance)?.closureThreadID == thread, "assistance eligibility preserves the original navigation target")
    assistance["codex_assistance_thread_id"] = "87654321-4321-4321-4321-cba987654321"
    check(AttentionIssue(assistance)?.closureAssistanceThreadID == "87654321-4321-4321-4321-cba987654321", "saved assistance has a separate validated target")
    assistance["codex_assistance_thread_id"] = "87654321-4321-4321-4321-cba987654321?prompt=run"
    check(AttentionIssue(assistance)?.closureAssistanceThreadID == "", "invalid assistance cannot become a URL")
    check(AttentionIssue(base)?.closureCanAssist == false, "an old item without an actual failed-open receipt cannot enable assistance")
    check(AttentionIssue(base)?.closureRuntimeLabel == "运行状态待核查", "an owner-only legacy row cannot claim running")
    check(AttentionIssue(base)?.closureRecoveryLabel == "未安排恢复检查", "a legacy row cannot claim an armed recovery")
    var observed = base; observed["closure_runtime_label"] = "当前未执行"; observed["closure_recovery_label"] = "条件已满足，未安排自动执行"
    check(AttentionIssue(observed)?.closureRuntimeLabel == "当前未执行", "native idle state survives attention decoding")
    check(AttentionIssue(observed)?.closureRecoveryLabel == "条件已满足，未安排自动执行", "readiness does not imply background execution")
    observed["closure_runtime_label"] = "正在执行"; observed["runtime"] = ["observed_at": 1000.0, "observed_monotonic": 2000.0, "clock_kind": "continuous_boot_v1"]
    check(AttentionIssue(observed)?.runtimeLabel(at: Date(timeIntervalSince1970: 1001), elapsed: 2001) == "正在执行", "fresh actual observation displays active")
    check(AttentionIssue(observed)?.runtimeLabel(at: Date(timeIntervalSince1970: 1031), elapsed: 2031) == "运行状态待核查", "cached display expires without refresh or user action")
    check(AttentionIssue(base)?.runtimeLabel(at: Date(timeIntervalSince1970: 1001)) == "运行状态待核查", "missing observation never claims running")
    check(AttentionIssue(observed)?.runtimeLabel(at: Date(timeIntervalSince1970: 1001), elapsed: 2031) == "运行状态待核查", "wall clock rollback cannot extend old evidence")
}

do {
    let choice: [String: Any] = ["id":"choice", "kind":"closure", "title":"本次个人选择", "state":"decision"]
    let history: [String: Any] = ["id":"history", "kind":"closure", "title":"历史事项", "state":"resolved", "current_verification":"unknown"]
    let notice: [String: Any] = ["id":"history-unavailable", "kind":"system", "title":"当前依据未读", "current_verification":"unknown"]
    let payload: [String: Any] = ["interface":"attention-center", "contract":1, "raw_access":false,
        "status":"ok", "decision_count":1, "decisions":[choice], "systems":[notice], "resources":[], "closures":[choice, history]]
    let decoded = AttentionSnapshot(jsonText:String(data:try! JSONSerialization.data(withJSONObject:payload), encoding:.utf8)!)
    check(decoded.status == "ok" && decoded.decisionCount == 1 && decoded.decisions.map(\.id) == ["choice"], "historical read unknown preserves the complete current choice count")
    let old = AttentionIssue(history)!
    check(old.workbenchStateLabel == "历史状态待核对" && old.systemImage == "questionmark.circle", "unread historical evidence never presents a current solved label or checkmark")
    check(decoded.closedClosures.map(\.id) == ["history"] && AttentionWorkbenchScope.following.rows(in: decoded).map(\.id) == ["history-unavailable"], "history remains historical with one explicit unknown notice on home and follow-up")
    check(AttentionIssue(choice)?.closureCurrentVerification == "" && AttentionIssue(choice)?.workbenchStateLabel == "待你决定", "a readable actual choice does not inherit historical uncertainty")
    for malformed in [true, 1, "current", NSNull()] as [Any] {
        var value = history; value["current_verification"] = malformed
        check(AttentionIssue(value) == nil, "unknown verification marker must be exact and cannot assert successful acceptance")
    }
}

print("PAM interaction checks passed (\(completed) assertions).")
