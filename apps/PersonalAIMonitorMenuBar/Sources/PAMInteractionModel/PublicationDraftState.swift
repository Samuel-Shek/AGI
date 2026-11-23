import Foundation

/// Only equality tokens enter this reducer. Neither receipts nor diagnostics contain text.
public struct PublicationDraftState {
    public enum Status: String, Codable { case clean, unsaved, saving, saveFailed = "save_failed", unknown }
    public struct Save: Equatable {
        public let id: UUID
        public let revision: UInt64
        public let value: String
    }
    public private(set) var current: String
    public private(set) var saved: String
    public private(set) var revision: UInt64 = 0
    private var savedRevision: UInt64 = 0
    private var confirmedRevision: UInt64? = 0
    private var pending: [UUID: Save] = [:]
    private var uncertain: [UUID: Save] = [:]
    private var failedRevision: UInt64?

    public init(_ value: String = "") { current = value; saved = value }
    public var status: Status {
        if !uncertain.isEmpty { return .unknown }
        if !pending.isEmpty { return .saving }
        if current == saved && confirmedRevision == revision { return .clean }
        if failedRevision == revision { return .saveFailed }
        return .unsaved
    }
    public mutating func edit(_ value: String) {
        guard current != value else { return }
        current = value; revision &+= 1; failedRevision = nil
        confirmedRevision = value == saved ? revision : nil
    }
    public mutating func beginSave() -> Save {
        let save = Save(id: UUID(), revision: revision, value: current)
        pending[save.id] = save; return save
    }
    /// A success must bind the exact submitted value/version at the call site.
    public mutating func finish(_ save: Save, outcome: Status) {
        guard pending.removeValue(forKey: save.id) == save || uncertain[save.id] == save else { return }
        if outcome == .unknown { uncertain[save.id] = save; return }
        uncertain.removeValue(forKey: save.id)
        if outcome == .clean {
            if save.revision >= savedRevision { saved = save.value; savedRevision = save.revision }
            if save.revision == revision { failedRevision = nil; confirmedRevision = revision }
        } else if outcome == .saveFailed, save.revision == revision { failedRevision = revision }
    }
    /// Discard never clears an operation whose outcome is still unconfirmed.
    public mutating func discard() { edit(saved); confirmedRevision = revision }
}
