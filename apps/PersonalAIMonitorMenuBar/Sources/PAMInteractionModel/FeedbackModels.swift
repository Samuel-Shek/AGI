import Foundation
import CoreFoundation

/// Presentation consumes only deliberately supplied prose, never arbitrary evidence JSON.
public struct AutonomyWorkItem: Identifiable {
    public let id: String
    public let kind: String
    public let state: String
    public let summary: String
    public let projectLabel: String
    public let evidenceSummaries: [String]
    public let resultSummary: String
    public let nextAction: String
    public let nextTrigger: String
    public let changes: [String]
    public let verificationSummary: String
    public let evidenceChanged: Bool
    public let nextCheck: Date?
    public let updated: Date?

    public init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty,
              let state = value["state"] as? String,
              Self.stateLabels[state] != nil else { return nil }
        self.id = id; self.state = state
        kind = value["kind"] as? String ?? ""
        summary = Self.prose(value["summary"]) ?? (kind == "memory" ? "记忆核查" : "改进事项")
        switch value["project_id"] as? String {
        case "agi", "personal-ai-monitor": projectLabel = "AGI"
        case "meeting-assistant", "meeting_assistant": projectLabel = "Meeting Assistant"
        default: projectLabel = "关联项目待核对"
        }
        evidenceSummaries = (value["evidence"] as? [[String: Any]] ?? []).compactMap { Self.prose($0["summary"]) }
        let result = value["result"] as? [String: Any] ?? [:]
        resultSummary = Self.prose(result["summary"]) ?? ""
        let action = Self.prose(result["action"]) ?? ""
        let rawChanges = (result["changes"] as? [String]) ?? (result["changes"] as? String).map { [$0] } ?? []
        var actualChanges = rawChanges.compactMap { Self.displayText($0, actionLabels: Self.completedActions) }
        // An applied memory action is a result, never a future instruction.
        if state == "verified", kind == "memory", actualChanges.isEmpty,
           let completed = Self.completedActions[action] { actualChanges = [completed] }
        changes = actualChanges
        nextAction = ["observing", "verified", "resolved"].contains(state) ? "" : Self.displayText(action, actionLabels: Self.pendingActions) ?? ""
        if let trigger = Self.prose(result["next_trigger"]) {
            nextTrigger = Self.triggerLabels[trigger] ?? Self.displayText(trigger) ?? "相关证据或处理状态变化后重新核对。"
        } else {
            nextTrigger = ["waiting_evidence", "blocked"].contains(state) ? "相关证据或处理状态变化后重新核对。" : ""
        }
        verificationSummary = Self.prose(result["verification_summary"]) ?? ""
        let source = value["source_revision"] as? String ?? ""
        let processed = value["processed_revision"] as? String ?? ""
        evidenceChanged = !source.isEmpty && !processed.isEmpty && source != processed
        nextCheck = Self.date(value["next_check"])
        updated = Self.date(value["updated"])
    }

    public static let stateLabels = [
        "queued": "等待系统处理", "investigating": "系统正在查证", "waiting_evidence": "等待新证据",
        "ready": "已完成查证 · 等待处理", "running": "系统正在处理", "verifying": "正在核对结果",
        "observing": "已处理 · 持续观察", "verified": "结果已验证", "resolved": "处理已完成",
        "human_required": "需要你选择", "blocked": "系统处理暂缓"
    ]
    public var statusLabel: String { evidenceChanged ? "证据已变化 · 等待重新核对" : Self.stateLabels[state] ?? "状态待核对" }
    public var requiresHuman: Bool { state == "human_required" && !evidenceChanged }
    public var limitation: String {
        if evidenceChanged { return "旧结果不能作为当前证据，系统需要重新查证。" }
        switch state {
        case "waiting_evidence": return "系统保留问题，相关证据变化后继续核查；无需你凭记忆作答。"
        case "blocked": return "这属于系统处理问题；尚未确认解决，也不会因此要求你作选择。"
        case "observing": return "处理已记录，实际效果仍需后续样本验证。"
        case "resolved": return "处理记录已完成；不等同于软件实际效果已经验证。"
        case "verified": return verificationSummary.isEmpty ? "后台已记录验证结果；详细验证说明暂不可读。" : ""
        case "human_required": return "涉及需要你取舍的事项，请在“需要你决定”查看背景和影响。"
        default: return "查证和处理进度不代表问题已经解决。"
        }
    }
    private static let completedActions = [
        "keep_active": "确认继续有效", "mark_legacy": "保留原文并标为历史",
        "supersede": "新建正确记忆并关联旧记录"
    ]
    private static let pendingActions = [
        "keep_active": "核对依据后确认这条记忆是否继续有效。",
        "mark_legacy": "核对依据后将这条记忆保留为历史。",
        "supersede": "核对新旧依据后建立正确记忆与旧记录的关联。"
    ]
    private static let triggerLabels = [
        "memory_or_authoritative_source_changed": "这条记忆或对应权威来源更新后继续核对。",
        "evidence_or_operator_recovery_changed": "依据更新或审核处理恢复后继续核对。"
    ]
    private static func displayText(_ raw: Any?, actionLabels: [String: String] = [:]) -> String? {
        guard let text = prose(raw) else { return nil }
        if let label = actionLabels[text] { return label }
        // Unknown machine identifiers are not human-facing instructions.
        if text.range(of: "^[A-Za-z][A-Za-z0-9_.:/-]*$", options: .regularExpression) != nil { return nil }
        return text
    }
    private static func prose(_ raw: Any?) -> String? {
        guard let text = raw as? String else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : String(trimmed.prefix(2000))
    }
    private static func date(_ raw: Any?) -> Date? {
        if let value = raw as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(), value.doubleValue > 0 {
            return Date(timeIntervalSince1970: value.doubleValue)
        }
        if let text = raw as? String { return ISO8601DateFormatter().date(from: text) }
        return nil
    }
}

public enum MemoryCoveragePresentation {
    public static func lines(_ raw: Any?) -> [String] {
        guard let value = raw as? [String: Any], value["available"] as? Bool == true,
              let sources = value["sources"] as? [[String: Any]], sources.count == 3 else {
            return ["记忆覆盖状态暂不可读取，未将未知状态记为零。"]
        }
        let labels = ["mem0": "Mem0", "business": "业务记忆库", "agi": "AGI 事项"]
        var seen = Set<String>(); var lines: [String] = []
        for row in sources {
            guard let name = row["source"] as? String, let label = labels[name], seen.insert(name).inserted,
                  let known = row["known"] as? Int, known >= 0,
                  let states = row["states"] as? [String: Int], states.values.allSatisfy({ $0 >= 0 }),
                  states.values.reduce(0, +) == known else {
                return ["记忆覆盖数据不完整，请等待重新核对。"]
            }
            let current = row["inventory_complete"] as? Bool == true && row["fresh"] as? Bool == true
            let gaps = ["source_unavailable", "pending_review", "integrity_gap", "owner_invalid"].reduce(0) { $0 + (states[$1] ?? 0) }
            let judged = (states["judged"] ?? 0) + (states["adopted"] ?? 0)
            lines.append("\(label)：已知 \(known) 项 · \(current ? "清单已核对" : "清单待复核，总数与缺口待确认") · 已识别缺口 \(gaps) · Jev 已判断 \(judged) · 已采用 \(states["adopted"] ?? 0)")
        }
        if let permits = value["permits"] as? [String: Any] {
            if permits["available"] as? Bool == true, permits["fresh"] as? Bool == true,
               let valid = permits["valid"] as? Int, let expiring = permits["expiring_48h"] as? Int {
                var expiry = "无可用许可"
                if let stamp = permits["next_expiry"] as? Double {
                    let formatter = DateFormatter(); formatter.dateStyle = .short; formatter.timeStyle = .short
                    expiry = formatter.string(from: Date(timeIntervalSince1970: stamp))
                }
                let reasons = permits["fallback_reasons"] as? [String: Int] ?? [:]
                lines.append("Jev 外发许可：有效 \(valid) 条 · 48 小时内到期 \(expiring) 条 · 最近到期 \(expiry)")
                if reasons.values.reduce(0, +) > 0 { lines.append("本地回退：到期 \(reasons["expired"] ?? 0) 条 · 来源或许可待复核 \((reasons["source_or_binding_changed"] ?? 0) + (reasons["source_or_permission_unavailable"] ?? 0)) 条") }
                if (reasons["service_or_caller_disabled"] ?? 0) > 0 { lines.append("Jev 服务或调用方已停用，当前使用本地流程。") }
                lines.append("服务启用不等于每条内容可发送；自由文本许可不会自动续期。")
            } else { lines.append("Jev 许可状态暂不可核对，未将未知状态记为零。") }
        }
        if let governance = value["governance"] as? [String: Any] {
            if governance["available"] as? Bool == true, governance["fresh"] as? Bool == true,
               let total = governance["legacy_total"] as? Int, let counts = governance["categories"] as? [String: Int] {
                lines.append("旧格式记忆 \(total) 条：已有有效决定 \(counts["existing_decision"] ?? 0) · 已登记直接来源待核实 \(counts["direct_source"] ?? 0) · 缺依据 \(counts["missing_source"] ?? 0) · 冲突 \(counts["conflict"] ?? 0) · 暂不可核对 \(counts["unknown"] ?? 0)")
                lines.append("等待证据的记忆事项 \(governance["waiting_count"] as? Int ?? 0) 件；恢复条件已登记，来源变化后重新核对。")
            } else { lines.append("旧记忆治理状态暂不可核对，既有审核决定保持。") }
        }
        if let mode = value["ranking_mode"] as? String {
            switch mode {
            case "off": lines.append("AGI 的 Jev 排序辅助已停用，按本地等待顺序处理；历史判断记录保留。")
            case "assist": lines.append("AGI 的 Jev 排序辅助已启用，仅对获准事项按需判断。")
            case "shadow": lines.append("AGI 的 Jev 当前仅观察，不改变处理顺序。")
            default: lines.append("AGI 的 Jev 排序开关暂不可确认，以本地流程为准。")
            }
        }
        return lines
    }
}

public enum JevEffectivenessPresentation {
    public static func lines(_ raw: Any?) -> [String] {
        guard let v = raw as? [String: Any], let batches = v["changed_batches"] as? Int,
              let advanced = v["advanced_processed"] as? Int, let verified = v["advanced_verified"] as? Int else {
            return ["Jev 实际效果尚无可核对数据。"]
        }
        var lines = ["Jev 实际效果：改变顺序 \(batches) 批 · 提前处理 \(advanced) 项 · 对应版本已验证 \(verified) 项"]
        lines.append(verified == 0 ? "尚无证据证明实际效率提升。" : "已记录处理结果；尚未证明相对原流程的因果收益。")
        if let waiting = v["waiting_count"] as? Int, let human = v["human_required"] as? Int, let recurrences = v["recurrences"] as? Int {
            lines.append("当前队列：等待证据 \(waiting) 项 · 需要本人选择 \(human) 项 · 已标记复发 \(recurrences) 项")
        }
        if let hours = v["oldest_wait_hours"] as? Double { lines.append(String(format: "当前最长等待 %.1f 小时；未作为 Jev 改善量。", hours)) }
        return lines
    }
}

public struct AutonomySnapshot {
    public let enabled: Bool
    public let paused: Bool
    public let items: [AutonomyWorkItem]
    public let counts: [String: Int]
    public let memoryCoverageLines: [String]
    public init?(_ value: [String: Any]) {
        guard let enabled = value["enabled"] as? Bool,
              let rows = value["items"] as? [[String: Any]],
              let rawCounts = value["counts"] as? [String: Any] else { return nil }
        let items = rows.compactMap(AutonomyWorkItem.init)
        guard items.count == rows.count, Set(items.map(\.id)).count == items.count else { return nil }
        var counts: [String: Int] = [:]
        for (key, raw) in rawCounts {
            guard AutonomyWorkItem.stateLabels[key] != nil,
                  let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue >= 0, number.doubleValue.rounded() == number.doubleValue,
                  number.doubleValue < Double(Int.max) else { return nil }
            counts[key] = number.intValue
        }
        // A partial item list is valid, but cannot contain more members than the totals.
        for (state, members) in Dictionary(grouping: items, by: \.state) {
            guard (counts[state] ?? 0) >= members.count else { return nil }
        }
        self.enabled = enabled; self.paused = value["paused"] as? Bool ?? !enabled
        self.items = items; self.counts = counts
        self.memoryCoverageLines = MemoryCoveragePresentation.lines(value["memory_coverage"]) + JevEffectivenessPresentation.lines(value["jev_ranking"])
    }
    public var processing: Int { ["queued", "investigating", "ready", "running", "verifying"].reduce(0) { $0 + (counts[$1] ?? 0) } }
    public var waiting: Int { ["waiting_evidence", "blocked"].reduce(0) { $0 + (counts[$1] ?? 0) } }
    public var summary: String {
        "系统处理中 \(processing) 件 · 等待证据或恢复 \(waiting) 件 · 持续观察 \(counts["observing"] ?? 0) 件 · 已验证 \(counts["verified"] ?? 0) 件"
    }
}

public struct FeedbackPresentedChoice: Identifiable, Equatable {
    public let id: String
    public let label: String
}

public enum FeedbackDecisionPresentation {
    public static func choices(_ proposal: [String: Any]) -> [FeedbackPresentedChoice] {
        let offered = proposal["choices"] as? [String] ?? []
        let alternatives = proposal["alternatives"] as? [String] ?? []
        let fixed = ["investigate": "继续核查原因", "defer": "暂缓，保留待决策", "dismiss": "这是误收录，排除本事项"]
        var seen = Set<String>()
        return offered.compactMap { identifier in
            guard seen.insert(identifier).inserted else { return nil }
            if let label = fixed[identifier] { return FeedbackPresentedChoice(id: identifier, label: label) }
            guard ["option_1", "option_2", "option_3"].contains(identifier),
                  let index = Int(identifier.suffix(1)), alternatives.indices.contains(index - 1) else { return nil }
            let label = alternatives[index - 1].trimmingCharacters(in: .whitespacesAndNewlines)
            guard !label.isEmpty else { return nil }
            return FeedbackPresentedChoice(id: identifier, label: label)
        }
    }

    public static func requiresHuman(state: String) -> Bool { ["decision", "human_required"].contains(state) }
    public static func pendingDecisions(_ rows: [[String: Any]], state: String) -> [[String: Any]] {
        guard requiresHuman(state: state) else { return [] }
        return rows.filter { ($0["state"] as? String ?? $0["status"] as? String ?? "pending") == "pending" }
    }
}
