import Foundation

public enum IdlePublicationPolicy {
    public static func reasons(drafts: [PublicationDraftState.Status], active: Bool, menuOpen: Bool,
                               modal: Bool, windowAtRisk: Bool, age: TimeInterval) -> [String] {
        guard age.isFinite, age >= 0, age <= 2.5 else { return ["request_expired"] }
        var result: [String] = []
        for status in [PublicationDraftState.Status.unknown, .saveFailed, .saving, .unsaved] {
            if drafts.contains(status) { result.append(status.rawValue) }
        }
        if active || menuOpen || modal || windowAtRisk { result.append("interface_in_use") }
        return result
    }
}
