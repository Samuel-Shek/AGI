import Foundation
import CoreFoundation

public enum LocalStatusTime {
    public static func date(_ value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }
    public static func label(_ value: String) -> String {
        guard let date = date(value) else { return "时间暂不可读" }
        return label(date)
    }
    public static func label(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = .current
        formatter.dateFormat = "M月d日 HH:mm:ss"
        return formatter.string(from: date)
    }
}

public enum RecordingAvailability {
    public static func allows(status: String, trusted: Bool, serviceKnown: Bool) -> Bool {
        trusted && serviceKnown && ["paused", "capturing", "idle", "ready", "stopped", "error", "recording"].contains(status)
    }
}

extension DocumentChangesSnapshot {
    public var coverageLabel: String {
        if !isAvailable { return "暂不可读" }
        if !coverage.enabled { return "追踪已关闭" }
        if coverage.eventGapCount > 0 { return "存在记录缺口" }
        if coverage.baselineStatus != "complete" { return "基线尚未完成" }
        if coverage.resumePending { return "正在恢复追踪" }
        if (coverage.pendingEventCount ?? 0) > 0 { return "新变化处理中" }
        if limitations.contains("requested_range_not_fully_covered") { return "时段覆盖不完整" }
        return coverage.isComplete ? "截至当前已处理" : "完整性待核对"
    }
    public var coverageExplanation: String {
        var parts: [String] = []
        if coverage.eventGapCount > 0 { parts.append("当前追踪存在记录缺口。") }
        if let pending = coverage.pendingEventCount, pending > 0 { parts.append("还有 \(pending) 条新事件正在处理。") }
        if limitations.contains("requested_range_not_fully_covered") { parts.append("所选时段超出已确认的追踪范围。") }
        if parts.isEmpty && !coverage.isComplete { parts.append("所选时段的完整性尚未确认。") }
        if !coverage.isComplete { parts.append("已记录的变化可查看，但不能据此判断没有其他修改。") }
        return parts.joined()
    }
}

public struct FeedbackProgress {
    public let state: String
    public let total: Int
    public let analyzed: Int
    public let pending: Int
    public let decisions: Int
    public let installed: Int
    public let verified: Int
    public let lastSuccess: Double?
    public let lastTick: Double?
    public let retryAt: Double
    public init?(_ object: [String: Any]) {
        guard object["raw_access"] as? Bool == false,
              let state = object["state"] as? String,
              ["paused", "running", "retrying", "stale", "error", "current"].contains(state) else { return nil }
        func number(_ key: String) -> Int? {
            guard let n = object[key] as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(),
                  n.doubleValue >= 0, n.doubleValue.rounded() == n.doubleValue else { return nil }
            return n.intValue
        }
        guard let total = number("incident_count"), let analyzed = number("analyzed_message_count"),
              let pending = number("pending_message_count"), let decisions = number("decision_count"),
              let installed = number("installed_count"), let verified = number("verified_count"),
              decisions <= total, verified <= installed, installed <= total else { return nil }
        self.state = state; self.total = total; self.analyzed = analyzed; self.pending = pending
        self.decisions = decisions; self.installed = installed; self.verified = verified
        self.lastSuccess = object["last_analysis_success_at"] as? Double
        self.lastTick = object["last_tick"] as? Double
        self.retryAt = object["next_retry_at"] as? Double ?? 0
    }
    public var label: String {
        switch state {
        case "paused": return "已暂停"
        case "running": return "正在分析"
        case "retrying": return "分析暂缓 · 等待重试"
        case "stale": return "后台进度待核对"
        case "error": return "最近分析未完成"
        default: return pending > 0 ? "等待分批分析" : "定时收录与检查"
        }
    }
    public var lastSuccessLabel: String {
        lastSuccess.map { LocalStatusTime.label(Date(timeIntervalSince1970: $0)) } ?? "尚无成功分析记录"
    }
}
