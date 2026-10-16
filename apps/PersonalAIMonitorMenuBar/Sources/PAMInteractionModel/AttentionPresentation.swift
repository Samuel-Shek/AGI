import Foundation

public enum AttentionPresentationMode: String, Sendable {
    case inline, sheet, pane
}

/// Each opening owns its reads, callbacks and version-bound action controls.
public struct AttentionPresentation: Identifiable, Equatable, Sendable {
    public let id: UUID
    public let issueID: String
    public var revision: String
    public let mode: AttentionPresentationMode
    public let anchorID: String

    public init(issueID: String, revision: String, mode: AttentionPresentationMode, anchorID: String? = nil) {
        id = UUID(); self.issueID = issueID; self.revision = revision; self.mode = mode; self.anchorID = anchorID ?? issueID
    }

    public func matches(issueID: String, revision: String) -> Bool {
        self.issueID == issueID && self.revision == revision
    }

    public func owns(_ other: AttentionPresentation) -> Bool {
        id == other.id && issueID == other.issueID && revision == other.revision && mode == other.mode && anchorID == other.anchorID
    }
}

public enum AttentionWriteOutcome: Sendable {
    case recorded, rejected, unknown
    public static func closure(_ result: [String: Any]?, id: String, revision: String) -> Self {
        guard let result else { return .unknown }
        if let returnedID = result["id"] as? String, returnedID != id { return .unknown }
        if let returnedID = result["issue_id"] as? String, returnedID != id { return .unknown }
        if result["id"] as? String == id, let next = result["revision"] as? Int,
           next == (Int(revision) ?? -2) + 1, ["pending", "verification", "canceled", "decision"].contains(result["state"] as? String ?? "") { return .recorded }
        if result["interface"] as? String == "task-closure", result["status"] as? String == "blocked",
           let error = result["error"] as? String,
           ["choice_display_binding_mismatch", "choice_terms_changed", "choice_invalid"].contains(error) { return .rejected }
        return .unknown
    }
    public static func memory(_ result: [String: Any]) -> Self {
        if result["interface"] as? String == "memory-review", result["contract"] as? Int == 2,
           result["applied_count"] is Int, result["failed_count"] as? Int == 0 { return .recorded }
        let prewrite = ["事项已变化，请重新查看成员与影响。", "所选成员不属于当前事项。",
            "成员的适用范围不一致或尚未明确，请分别选择处理方式。", "批量操作需要相同处理方式。",
            "内容或成员已变化，请重新预览后确认。", "confirmation digest does not match the previewed batch"]
        if result["interface"] as? String == "attention-center", result["contract"] as? Int == 1,
           result["status"] as? String == "unavailable", let error = result["error"] as? String,
           prewrite.contains(error) || error.hasPrefix("record_changed:") || error.hasPrefix("evidence_changed:") { return .rejected }
        return .unknown
    }
    public static func activity(_ result: [String: Any]?, groupKey: String) -> Self {
        guard let result else { return .unknown }
        if let returnedKey = result["group_key"] as? String, returnedKey != groupKey { return .unknown }
        if result["status"] as? String == "recorded", result["group_key"] as? String == groupKey { return .recorded }
        if ["blocked", "stale"].contains(result["status"] as? String ?? ""), let error = result["error"] as? String,
           ["group_missing", "context_confirmation_linked_group_immutable", "group_regrouped", "group_members_changed", "information_insufficient"].contains(error) { return .rejected }
        return .unknown
    }
}

/// Submission ownership outlives a card being collapsed or replaced.
public struct AttentionSubmissions {
    public private(set) var pending: Set<String> = []
    public private(set) var receipts: [String: String] = [:]
    public private(set) var uncertain: Set<String> = []
    private var deferredRecorded: Set<String> = []
    private var reviewLaterRecorded: [String: Set<String>] = [:]
    public private(set) var rejected: Set<String> = []
    public private(set) var receiptHistory: [String: [String]] = [:]
    public private(set) var completed: Set<String> = []
    public init() {}
    public static func key(_ id: String, _ revision: String) -> String { id + "\n" + revision }
    public func blocked(_ key: String) -> Bool { pending.contains(key) || uncertain.contains(key) || completed.contains(key) || rejected.contains(key) }
    public mutating func begin(_ key: String) -> Bool {
        guard !blocked(key) else { return false }
        pending.insert(key); return true
    }
    public mutating func finish(_ key: String, receipt: String, outcome: AttentionWriteOutcome, deferred: Bool = false, reviewLaterMembers: Set<String> = []) {
        pending.remove(key); receipts[key] = receipt
        receiptHistory[key, default: []].append(receipt)
        switch outcome {
        case .recorded:
            completed.insert(key); if deferred { deferredRecorded.insert(key) }
            if !reviewLaterMembers.isEmpty { reviewLaterRecorded[key] = reviewLaterMembers }
        case .rejected: rejected.insert(key)
        case .unknown: uncertain.insert(key)
        }
    }
    public mutating func verifiedRead(id: String, revision: String, unresolvedActivity: Bool = false, reviewLaterMembers: Set<String> = []) {
        let key = Self.key(id, revision)
        rejected.remove(key)
        if unresolvedActivity && deferredRecorded.remove(key) != nil { completed.remove(key) }
        if let submitted = reviewLaterRecorded[key], submitted.isSubset(of: reviewLaterMembers) {
            reviewLaterRecorded.removeValue(forKey: key); completed.remove(key)
        }
    }
}

extension AttentionSnapshot {
    public var followUpClosures: [AttentionIssue] {
        let decisionIDs = Set(decisions.map(\.id))
        return closures.filter { !decisionIDs.contains($0.id) &&
            (!["resolved", "canceled", "dismissed"].contains($0.closureState) || $0.closureRequiresCheck) }
    }
    public var closedClosures: [AttentionIssue] {
        let decisionIDs = Set(decisions.map(\.id))
        return closures.filter { !decisionIDs.contains($0.id) &&
            ["resolved", "canceled", "dismissed"].contains($0.closureState) && !$0.closureRequiresCheck }
    }
}

public enum AttentionMemoryPreview {
    public static func matches(_ value: [String: Any], revision: String, decisions: [String: String]) -> Bool {
        guard !decisions.isEmpty, value["interface"] as? String == "memory-review", value["contract"] as? Int == 2,
              value["raw_access"] as? Bool == false, value["attention_revision"] as? String == revision,
              let digest = value["preview_digest"] as? String, digest.count == 64,
              let changes = value["changes"] as? [[String: Any]], changes.count == decisions.count,
              value["item_count"] as? Int == decisions.count else { return false }
        var seen = Set<String>()
        for change in changes {
            guard let id = change["memory_id"] as? String, seen.insert(id).inserted,
                  let action = change["action"] as? String, decisions[id] == action else { return false }
        }
        return true
    }
}
