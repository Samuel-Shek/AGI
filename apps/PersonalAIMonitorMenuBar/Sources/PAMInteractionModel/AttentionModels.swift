import Foundation
#if canImport(Darwin)
import Darwin
#endif

public func closureElapsedTime() -> Double {
    #if canImport(Darwin)
    var value = timespec()
    guard clock_gettime(CLOCK_MONOTONIC_RAW, &value) == 0 else { return .nan }
    return Double(value.tv_sec) + Double(value.tv_nsec) / 1_000_000_000
    #else
    return .nan
    #endif
}

public struct AttentionMember: Identifiable {
    public let id: String
    public let title: String
    public let reason: String
    public init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        title = value["title"] as? String ?? "待复核的记忆"
        reason = value["reason"] as? String ?? ""
    }
}

public struct AttentionIssue: Identifiable {
    public let id: String
    public let kind: String
    public let title: String
    public let summary: String
    public let background: String
    public let reason: String
    public let impact: String
    public let inaction: String
    public let recommendation: String
    public let attempts: String
    public let completionRule: String
    public let revision: String
    public let groupKey: String
    public let memberCount: Int
    public let members: [AttentionMember]
    public let children: [AttentionIssue]
    public let closureState: String
    public let closureCurrentVerification: String
    public let closureRequiresCheck: Bool
    public let closureThreadID: String
    public let closureCanHandoff: Bool
    public let closureOpenViaHandoff: Bool
    public let closureCanAssist: Bool
    public let closureAssistanceToken: String
    public let closureAssistanceThreadID: String
    public let closureRuntimeLabel: String
    public let closureRuntimeObservedAt: Double?
    public let closureRuntimeElapsedAt: Double?
    public let closureRecoveryLabel: String
    public init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty,
              let kind = value["kind"] as? String,
              let title = value["title"] as? String else { return nil }
        self.id = id; self.kind = kind; self.title = title
        summary = value["summary"] as? String ?? ""
        background = value["background"] as? String ?? ""
        reason = value["reason"] as? String ?? ""
        impact = value["impact"] as? String ?? ""
        inaction = value["inaction"] as? String ?? ""
        recommendation = value["recommendation"] as? String ?? ""
        attempts = value["attempts"] as? String ?? ""
        completionRule = value["completion_rule"] as? String ?? ""
        revision = value["revision"] as? String ?? ""
        groupKey = value["group_key"] as? String ?? ""
        closureState = value["state"] as? String ?? ""
        if let raw = value["current_verification"] {
            guard let state = raw as? String, state == "unknown" else { return nil }
            closureCurrentVerification = state
        } else { closureCurrentVerification = "" }
        closureRuntimeLabel = value["closure_runtime_label"] as? String ?? "运行状态待核查"
        closureRuntimeObservedAt = (value["runtime"] as? [String: Any])?["observed_at"] as? Double
        let runtime = value["runtime"] as? [String: Any]
        closureRuntimeElapsedAt = runtime?["clock_kind"] as? String == "continuous_boot_v1" ? runtime?["observed_monotonic"] as? Double : nil
        closureRecoveryLabel = value["closure_recovery_label"] as? String ?? "未安排恢复检查"
        closureRequiresCheck = value["requires_check"] as? Bool ?? false
        let candidate = value["codex_thread_id"] as? String ?? value["thread_id"] as? String ?? ""
        closureThreadID = UUID(uuidString: candidate)?.uuidString.lowercased() == candidate ? candidate : ""
        closureCanHandoff = value["codex_handoff_available"] as? Bool ?? false
        closureOpenViaHandoff = value["codex_open_via_handoff"] as? Bool ?? false
        closureCanAssist = value["codex_assistance_available"] as? Bool ?? false
        closureAssistanceToken = value["codex_assistance_token"] as? String ?? ""
        let helper = value["codex_assistance_thread_id"] as? String ?? ""
        closureAssistanceThreadID = UUID(uuidString: helper)?.uuidString.lowercased() == helper ? helper : ""
        memberCount = value["member_count"] as? Int ?? 0
        let rawMembers = value["members"] as? [[String: Any]] ?? []
        let rawChildren = value["children"] as? [[String: Any]] ?? []
        members = rawMembers.compactMap(AttentionMember.init)
        children = rawChildren.compactMap(AttentionIssue.init)
        guard members.count == rawMembers.count, children.count == rawChildren.count,
              kind != "memory" || memberCount == members.count else { return nil }
    }
    public var systemImage: String {
        switch kind {
        case "memory": return "brain"
        case "activity": return "questionmark.bubble"
        case "system": return "wrench.and.screwdriver"
        case "closure": return closureCurrentVerification == "unknown" ? "questionmark.circle" : "checkmark.circle"
        default: return "archivebox"
        }
    }
    public func runtimeLabel(at date: Date, elapsed: Double = closureElapsedTime()) -> String {
        guard let observed = closureRuntimeObservedAt, observed.isFinite,
              let originalElapsed = closureRuntimeElapsedAt, originalElapsed.isFinite,
              (0...30).contains(date.timeIntervalSince1970 - observed),
              (0...30).contains(elapsed - originalElapsed),
              abs((date.timeIntervalSince1970 - observed) - (elapsed - originalElapsed)) <= 2 else { return "运行状态待核查" }
        return closureRuntimeLabel
    }
}

public struct AttentionSnapshot {
    public var displayScopeRevision = ""
    public var displayScopeWithdrawn = false
    public var status = "unknown"
    public var decisionCount: Int?
    public var memoryPending: Int?
    public var memoryTotal: Int?
    public var decisions: [AttentionIssue] = []
    public var systems: [AttentionIssue] = []
    public var resources: [AttentionIssue] = []
    public var closures: [AttentionIssue] = []
    public var revision = ""
    public var generatedAt = ""
    public var message = "事项暂不可读取"
    public init() {}
    public init(jsonText: String) {
        guard let data = jsonText.data(using: .utf8),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        displayScopeRevision = value["display_scope_revision"] as? String ?? ""
        displayScopeWithdrawn = ["display_scope_unavailable", "source_not_in_display_scope"].contains(value["reason"] as? String ?? "")
        guard
              value["interface"] as? String == "attention-center",
              value["contract"] as? Int == 1,
              value["raw_access"] as? Bool == false,
              let status = value["status"] as? String,
              ["ok", "partial"].contains(status),
              let rawDecisions = value["decisions"] as? [[String: Any]],
              let rawSystems = value["systems"] as? [[String: Any]],
              let rawResources = value["resources"] as? [[String: Any]] else { return }
        let parsed = rawDecisions.compactMap(AttentionIssue.init)
        guard parsed.count == rawDecisions.count else { return }
        let count = value["decision_count"] as? Int
        guard count == nil || count == parsed.count else { return }
        let parsedSystems = rawSystems.compactMap(AttentionIssue.init)
        let parsedResources = rawResources.compactMap(AttentionIssue.init)
        let rawClosures = value["closures"] as? [[String: Any]] ?? []
        let parsedClosures = rawClosures.compactMap(AttentionIssue.init)
        guard parsedSystems.count == rawSystems.count, parsedResources.count == rawResources.count,
              parsedClosures.count == rawClosures.count, status != "ok" || count != nil else { return }
        self.status = status; decisionCount = count
        decisions = parsed
        systems = parsedSystems
        resources = parsedResources
        closures = parsedClosures
        memoryPending = value["memory_pending"] as? Int
        memoryTotal = value["memory_total"] as? Int
        revision = value["revision"] as? String ?? ""
        generatedAt = value["generated_at"] as? String ?? ""
        message = count == nil ? "部分事项暂不可读取" : ""
    }
    public var summaryLabel: String {
        guard decisionCount != nil else { return "部分事项暂不可读取" }
        let memoryGroups = decisions.filter { $0.kind == "memory" }.count
        let feedback = decisions.filter { $0.kind == "feedback" }.reduce(0) { $0 + $1.memberCount }
        let other = decisions.filter { !["memory", "feedback"].contains($0.kind) }.count
        var parts: [String] = []
        if memoryGroups > 0 { parts.append("记忆 \(memoryGroups) 组 · \(memoryPending ?? 0) 条待复核") }
        if feedback > 0 { parts.append("批评与改进 \(feedback) 件待处理") }
        if other > 0 { parts.append("其他 \(other) 组") }
        return parts.isEmpty ? "目前无需你做决定" : parts.joined(separator: "；")
    }
    public var countLabel: String { decisionCount.map { "\($0) 组" } ?? "暂不可读" }
    public var badge: String? { decisionCount.flatMap { $0 > 0 ? String($0) : nil } }
}

public enum MemoryReasonPresentation {
    public static func title(_ codes: [String]) -> String {
        let labels = codes.map { code -> String in
            switch code {
            case "MEM-AUDIT-TEMPORAL-001": return "时效待核对"
            case "MEM-AUDIT-STALE-001": return "旧信息待复核"
            case "MEM-AUDIT-RAW-001": return "内容需整理"
            case "MEM-AUDIT-SUBJECT-001": return "归属字段待补核"
            case "MEM-AUDIT-DUP-001": return "相似记忆"
            default: return "需要复核"
            }
        }
        return labels.isEmpty ? "长期记忆" : Array(NSOrderedSet(array: labels)).compactMap { $0 as? String }.joined(separator: " · ")
    }
}

/// A dismissed or superseded read can never re-populate private body text.
public struct MemoryRevealSession {
    public private(set) var generation = UUID()
    public private(set) var presentationID = UUID()
    public private(set) var identifier = ""
    public private(set) var state = "closed"
    public private(set) var text = ""
    public private(set) var scope = ""
    public init() {}
    @discardableResult public mutating func begin(_ identifier: String, presentationID: UUID? = nil) -> UUID {
        generation = UUID(); self.identifier = identifier
        self.presentationID = presentationID ?? UUID()
        state = "loading"; text = ""; scope = ""
        return generation
    }
    public mutating func receive(_ raw: String, token: UUID) {
        guard generation == token, !identifier.isEmpty else { return }
        if let data = raw.data(using: .utf8),
           let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let error = value["error"] as? String {
            state = "failed"
            if error.contains("missing") || error.contains("not_found") {
                text = "正文来源尚不可用，记忆库可能未挂载或记录已变化。连接恢复后可以重试。"
            } else if error.contains("owner") || error.contains("subject") {
                text = "这条记录未通过归属核对，本次没有显示正文。"
            } else {
                text = "读取未完成。请检查记忆库连接后重试；原记录没有被修改。"
            }
            return
        }
        guard let data = raw.data(using: .utf8),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              value["memory_id"] as? String == identifier,
              let body = value["text"] as? String else {
            state = "failed"; text = "正文暂时无法读取。请检查记忆库是否可用后重试。"; return
        }
        text = body
        scope = value["applies_to"] as? String ?? ""
        state = body.isEmpty ? "empty" : "ready"
    }
    @discardableResult public mutating func close(presentationID: UUID? = nil) -> Bool {
        if let presentationID, presentationID != self.presentationID { return false }
        generation = UUID(); self.presentationID = UUID()
        identifier = ""; state = "closed"; text = ""; scope = ""
        return true
    }
}
