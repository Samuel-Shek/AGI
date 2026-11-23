import Foundation
import SwiftUI
import CryptoKit
import PAMInteractionModel

protocol PublicationDraftValue: Codable, Equatable { static var emptyDraft: Self { get } }
extension String: PublicationDraftValue { static var emptyDraft: String { "" } }
extension Bool: PublicationDraftValue { static var emptyDraft: Bool { false } }
extension Date: PublicationDraftValue { static var emptyDraft: Date { Date(timeIntervalSince1970: 0) } }
extension Dictionary: PublicationDraftValue where Key: Codable, Value: Codable & Equatable {
    static var emptyDraft: Self { [:] }
}

@MainActor
fileprivate protocol PublicationSlot: AnyObject {
    var state: PublicationDraftState { get set }
    func begin() -> PublicationDraftState.Save
    func finish(_ save: PublicationDraftState.Save, _ outcome: PublicationDraftState.Status)
    func discard()
    func reset()
    func redact()
}

@MainActor
fileprivate final class DraftSlot<Value: PublicationDraftValue>: ObservableObject, PublicationSlot {
    @Published var value: Value { didSet { state.edit(Self.token(value)) } }
    var state: PublicationDraftState
    private var original: Value
    private var initial: Value
    private var submitted: [UUID: Value] = [:]
    init(_ value: Value) { self.value = value; original = value; initial = value; state = PublicationDraftState(Self.token(value)) }
    static func token(_ value: Value) -> String {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(value) else { preconditionFailure("Unsupported draft value") }
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
    func initialize(_ value: Value) {
        guard state.revision == 0, state.status == .clean else { return }
        self.value = value; original = value; initial = value
        state = PublicationDraftState(Self.token(value))
    }
    func discard() { value = original; state.discard() }
    func reset() { guard state.status == .clean else { return }; value = initial; original = initial; state = PublicationDraftState(Self.token(initial)) }
    func redact() { value = .emptyDraft; original = .emptyDraft; initial = .emptyDraft; submitted = [:] }
    func begin() -> PublicationDraftState.Save {
        let save = state.beginSave(); submitted[save.id] = value; return save
    }
    func finish(_ save: PublicationDraftState.Save, _ outcome: PublicationDraftState.Status) {
        state.finish(save, outcome: outcome)
        if outcome == .clean, state.saved == save.value, let value = submitted[save.id] { original = value }
        if outcome != .unknown { submitted.removeValue(forKey: save.id) }
    }
}

/// In-memory owners outlive a SwiftUI mount. No new persistence or submission is introduced.
@MainActor
final class PublicationDrafts {
    static let shared = PublicationDrafts()
    final class Ticket {
        let id = UUID()
        fileprivate let saves: [(String, PublicationDraftState.Save)]
        fileprivate(set) var outcome: PublicationDraftState.Status?
        fileprivate init(saves: [(String, PublicationDraftState.Save)]) { self.saves = saves }
    }
    private var slots: [String: any PublicationSlot] = [:]
    private var readbacks: [UUID: (Ticket, @MainActor () async -> PublicationDraftState.Status)] = [:]
    private var reading: Set<UUID> = []
    private var observers: [UUID: @MainActor (PublicationDraftState.Status) -> Void] = [:]
    private var withdrawn: [String: PublicationDraftState] = [:]
    private var activeTickets: [UUID: Ticket] = [:]
    private var withdrawnTickets: Set<UUID> = []
    static func fingerprint(_ value: String) -> String { DraftSlot<String>.token(value) }
    static func readback(_ raw: String, interface: String) -> PublicationDraftState.Status {
        guard let value = try? JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [String: Any],
              value["interface"] as? String == interface, value["raw_access"] as? Bool == false,
              value["status"] as? String == "confirmed" else { return .unknown }
        return .clean
    }
    static func mutation(_ raw: String) -> PublicationDraftState.Status {
        guard let value = try? JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [String: Any] else { return .unknown }
        if value["save_state"] as? String == "unknown" { return .unknown }
        if value["raw_access"] as? Bool == false && ["rejected", "blocked", "disabled", "stale"].contains(value["status"] as? String ?? "") { return .saveFailed }
        return .unknown
    }
    var statuses: [PublicationDraftState.Status] { slots.values.map { $0.state.status } + withdrawn.values.map(\.status) }
    /// Privacy withdrawal removes retained content and resolvers. Only equality
    /// tokens for unconfirmed operations survive; old tickets cannot rehydrate UI.
    func withdraw(_ scope: String) {
        for key in slots.keys.filter({ $0.hasPrefix(scope + "/") || $0.hasPrefix(scope + ".") }) {
            let slot = slots.removeValue(forKey: key)!
            let state = slot.state
            slot.redact()
            if state.status == .saving || state.status == .unknown { withdrawn[key] = state }
        }
        for (id, ticket) in activeTickets where ticket.saves.contains(where: { $0.0.hasPrefix(scope + "/") || $0.0.hasPrefix(scope + ".") }) {
            withdrawnTickets.insert(id); readbacks.removeValue(forKey: id); observers.removeValue(forKey: id)
        }
    }
    private func slot<Value: PublicationDraftValue>(_ key: String, initial: Value) -> DraftSlot<Value> {
        if let existing = slots[key] {
            guard let typed = existing as? DraftSlot<Value> else { preconditionFailure("Draft field type changed") }
            return typed
        }
        let slot = DraftSlot(initial); slots[key] = slot; return slot
    }
    func update<Value: PublicationDraftValue>(_ key: String, initial: Value, value: Value) {
        slot(key, initial: initial).value = value
    }
    func initialize<Value: PublicationDraftValue>(_ key: String, value: Value) {
        slot(key, initial: value).initialize(value)
    }
    func begin(_ scope: String) -> Ticket {
        // Even a default-value confirmation has an in-flight operation.
        _ = slot(scope + "/operation", initial: "")
        let ticket = Ticket(saves: slots.keys.filter { $0.hasPrefix(scope + "/") }.sorted().map {
            ($0, slots[$0]!.begin())
        })
        activeTickets[ticket.id] = ticket; return ticket
    }
    func finish(_ ticket: Ticket, _ outcome: PublicationDraftState.Status) {
        guard activeTickets[ticket.id] != nil else { return }
        ticket.outcome = outcome
        for (key, save) in ticket.saves {
            slots[key]?.finish(save, outcome)
            if var state = withdrawn[key] {
                state.finish(save, outcome: outcome)
                if state.status == .clean || state.status == .saveFailed || state.status == .unsaved { withdrawn.removeValue(forKey: key) }
                else { withdrawn[key] = state }
            }
        }
        if outcome != .unknown {
            readbacks.removeValue(forKey: ticket.id); activeTickets.removeValue(forKey: ticket.id); withdrawnTickets.remove(ticket.id)
            observers.removeValue(forKey: ticket.id)?(outcome)
        }
    }
    func whenResolved(_ ticket: Ticket, _ observer: @escaping @MainActor (PublicationDraftState.Status) -> Void) {
        guard !withdrawnTickets.contains(ticket.id) else { return }
        if let outcome = ticket.outcome, outcome == .clean || outcome == .saveFailed { observer(outcome) }
        else { observers[ticket.id] = observer }
    }
    /// Read-only reconciliation stays on the original form's read path. It never resubmits.
    func verify(_ ticket: Ticket, using read: @escaping @MainActor () async -> PublicationDraftState.Status) async {
        guard !withdrawnTickets.contains(ticket.id) else { return }
        readbacks[ticket.id] = (ticket, read)
        await retry(ticket.id)
    }
    private func retry(_ id: UUID) async {
        guard !reading.contains(id), let (ticket, read) = readbacks[id] else { return }
        reading.insert(id); defer { reading.remove(id) }
        finish(ticket, await read())
    }
    func retry(_ scope: String) async {
        for id in readbacks.keys.filter({ id in
            readbacks[id]!.0.saves.contains { $0.0.hasPrefix(scope + "/") }
        }) { await retry(id) }
    }
    func value<Value: PublicationDraftValue>(_ key: String, initial: Value) -> Value { slot(key, initial: initial).value }
    func isFieldClean(_ key: String) -> Bool { slots[key]?.state.status == .clean || slots[key] == nil }
    func discardValue<Value: PublicationDraftValue>(_ key: String, initial: Value) -> Value {
        let entry = slot(key, initial: initial); entry.discard(); return entry.value
    }
    func completeAfterReadback(_ ticket: Ticket, scope: String, completion: @escaping @MainActor () -> Void) {
        whenResolved(ticket) { [weak self] outcome in
            guard let self, outcome == .clean, self.isCurrent(ticket) else { return }
            self.resetSaved(scope); completion()
        }
    }
    func matches(_ key: String, fingerprint: String) -> Bool { slots[key]?.state.current == fingerprint }
    func isClean(_ scope: String) -> Bool {
        slots.filter { $0.key.hasPrefix(scope + "/") }.values.allSatisfy { $0.state.status == .clean }
    }
    func needsReadback(_ scope: String) -> Bool {
        slots.contains { $0.key.hasPrefix(scope + "/") && $0.value.state.status == .unknown }
    }
    func isCurrent(_ ticket: Ticket) -> Bool {
        ticket.saves.allSatisfy { key, save in slots[key]?.state.revision == save.revision && slots[key]?.state.current == save.value }
    }
    func resetSaved(_ scope: String) {
        for (key, slot) in slots where key.hasPrefix(scope + "/") { slot.reset() }
    }
    func discard(_ scope: String) {
        for (key, slot) in slots where key.hasPrefix(scope + "/") { slot.discard() }
    }
    fileprivate func retained<Value: PublicationDraftValue>(_ key: String, initial: Value) -> DraftSlot<Value> {
        slot(key, initial: initial)
    }
}

@MainActor
@propertyWrapper
struct RetainedDraft<Value: PublicationDraftValue>: DynamicProperty {
    @ObservedObject private var slot: DraftSlot<Value>
    init(wrappedValue: Value, _ scope: String, _ field: String) {
        slot = PublicationDrafts.shared.retained(scope + "/" + field, initial: wrappedValue)
    }
    var wrappedValue: Value { get { slot.value } nonmutating set { slot.value = newValue } }
    var projectedValue: Binding<Value> { Binding(get: { slot.value }, set: { slot.value = $0 }) }
}
