import Foundation

public enum IdlePublicationPolicy {
    public static func permitsExit(untouched: Bool, active: Bool, modal: Bool,
                                   windowAtRisk: Bool, age: TimeInterval) -> Bool {
        untouched && !active && !modal && !windowAtRisk && age >= 0 && age <= 2.5
    }
}
