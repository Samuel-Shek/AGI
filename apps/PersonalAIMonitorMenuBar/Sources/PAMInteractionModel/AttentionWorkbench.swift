import Foundation

/// Automatic read hints can share work already in flight. Explicit refreshes
/// and writes still require one fresh follow-up; withdrawal revokes the chain.
public struct AttentionRefreshReads: Equatable, Sendable {
    public private(set) var generation: UInt64 = 0
    public private(set) var loading = false
    public private(set) var pending = false
    public init() {}
    public mutating func request(coalescingIfLoading: Bool = false) -> UInt64? {
        if loading {
            if !coalescingIfLoading { pending = true }
            return nil
        }
        generation &+= 1; loading = true
        return generation
    }
    public func isCurrent(_ token: UInt64) -> Bool { token == generation && loading }
    public mutating func finish(_ token: UInt64) -> Bool {
        guard isCurrent(token) else { return false }
        loading = false
        let followUp = pending; pending = false
        return followUp
    }
    public mutating func invalidate() {
        generation &+= 1; loading = false; pending = false
    }
}

public enum AttentionWorkbenchScope: String, CaseIterable, Identifiable {
    case decisions, following, closed, all
    public var id: String { rawValue }
    public var title: String {
        switch self {
        case .decisions: return "待你决定"
        case .following: return "系统跟进"
        case .closed: return "已结束"
        case .all: return "全部"
        }
    }
    public func rows(in snapshot: AttentionSnapshot) -> [AttentionIssue] {
        let rows: [AttentionIssue]
        switch self {
        case .decisions: rows = snapshot.decisions
        case .following: rows = snapshot.followUpClosures + snapshot.systems
        case .closed: rows = snapshot.closedClosures
        case .all: rows = snapshot.decisions + snapshot.followUpClosures + snapshot.systems + snapshot.closedClosures
        }
        var seen = Set<String>()
        return rows.filter { seen.insert($0.id).inserted }
    }
}

extension AttentionSnapshot {
    /// A failed/partial read may add readable evidence, but cannot erase old rows
    /// or establish a verified count. All retained rows are displayed as stale.
    public func retainingUnreadRows(from previous: AttentionSnapshot) -> AttentionSnapshot {
        guard displayScopeRevision.isEmpty && !displayScopeWithdrawn else { return self }
        guard status != "ok" else { return self }
        var retained = self
        func merge(_ current: [AttentionIssue], _ old: [AttentionIssue]) -> [AttentionIssue] {
            var seen = Set<String>()
            return (current + old).filter { seen.insert($0.id).inserted }
        }
        retained.decisions = merge(decisions, previous.decisions)
        retained.systems = merge(systems, previous.systems)
        retained.resources = merge(resources, previous.resources)
        retained.closures = merge(closures, previous.closures)
        retained.decisionCount = nil
        return retained
    }
    public func workbenchItem(_ id: String) -> AttentionIssue? {
        func find(_ rows: [AttentionIssue]) -> AttentionIssue? {
            for item in rows {
                if item.id == id { return item }
                if let child = find(item.children) { return child }
            }
            return nil
        }
        return find(decisions + closures + systems + resources)
    }
    public func withdrawingItem(_ id: String) -> AttentionSnapshot {
        var next = self
        func contains(_ item: AttentionIssue) -> Bool {
            item.id == id || item.children.contains(where: contains)
        }
        next.decisions.removeAll(where: contains)
        next.closures.removeAll(where: contains)
        next.systems.removeAll(where: contains)
        next.resources.removeAll(where: contains)
        next.status = "partial"; next.decisionCount = nil
        next.memoryPending = nil; next.memoryTotal = nil
        return next
    }
}

/// Only the in-memory display cache is withdrawn. Durable drafts and unknown
/// submissions retain their original history and receipt ownership.
public struct AttentionHandoffDraftCache {
    public struct Lease: Equatable {
        fileprivate let id: String
        fileprivate let scope: UInt64
        fileprivate let source: UInt64
    }
    private var drafts: [String: (text: String, revision: Int64)] = [:]
    private var sources: [String: UInt64] = [:]
    private var scope: UInt64 = 0
    public init() {}
    public func lease(for id: String) -> Lease {
        Lease(id: id, scope: scope, source: sources[id, default: 0])
    }
    public func isCurrent(_ lease: Lease) -> Bool {
        lease.scope == scope && lease.source == sources[lease.id, default: 0]
    }
    public func draft(using lease: Lease) -> (text: String, revision: Int64)? {
        isCurrent(lease) ? drafts[lease.id] : nil
    }
    @discardableResult public mutating func save(_ text: String, revision: Int64, using lease: Lease) -> Bool {
        guard isCurrent(lease) else { return false }
        if revision >= (drafts[lease.id]?.revision ?? 0) { drafts[lease.id] = (text, revision) }
        return true
    }
    public mutating func withdraw(_ id: String) {
        drafts.removeValue(forKey: id); sources[id, default: 0] &+= 1
    }
    public mutating func withdrawAll() {
        drafts.removeAll(); scope &+= 1
    }
}

extension AttentionIssue {
    public var workbenchStateLabel: String {
        guard kind == "closure" else { return kind == "system" ? "系统核查" : "待你决定" }
        if closureCurrentVerification == "unknown" {
            return ["resolved", "canceled", "dismissed"].contains(closureState) ? "历史状态待核对" : "状态待核对"
        }
        if closureRequiresCheck { return "仍需核对" }
        switch closureState {
        case "decision": return "待你决定"
        case "pending": return "等待处理"
        case "running": return "处理中"
        case "verification": return "待验收"
        case "blocked": return "等待条件"
        case "resolved": return "已解决"
        case "canceled": return "已取消"
        case "dismissed": return "已关闭"
        default: return "状态待核对"
        }
    }
    public var workbenchKindLabel: String {
        switch kind {
        case "closure": return "跟进事项"
        case "memory": return "记忆核对"
        case "activity": return "活动确认"
        case "feedback": return "批评与改进"
        case "system": return "系统记录"
        default: return "事项"
        }
    }
    public var workbenchExcerpt: String {
        [impact, recommendation, reason].first { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty } ?? "查看背景与下一步"
    }
}

// A malformed action receipt is not evidence that no request was recorded.
public enum AttentionHandoffReceipt {
    public static func sourceWithdrawn(_ response: [String: Any]) -> Bool {
        response["interface"] as? String == "attention-handoff" && response["status"] as? String == "unavailable"
            && ["source_not_in_display_scope", "display_scope_unavailable"].contains(response["reason"] as? String ?? "")
    }
    public static func verified(_ response: [String: Any]) -> Bool {
        guard response["interface"] as? String == "attention-handoff", response["status"] as? String == "ok", response["outcome_unknown"] as? Bool != true else { return false }
        if response["handoff"] is NSNull { return true }
        guard let handoff = response["handoff"] as? [String: Any],
              let id = handoff["submission_id"] as? String, !id.isEmpty,
              let status = handoff["status"] as? String else { return false }
        return ["queued","preparing","opening","sending","verifying","delivered","failed","uncertain"].contains(status)
    }
}
