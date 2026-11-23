import AppKit

enum ConsoleLaunchIntent {
    static func shouldShowOnReopen(isActive: Bool, event: NSAppleEventDescriptor?) -> Bool {
        // LaunchServices' current open -gj event explicitly disables restore
        // (rsto: 'no  '). Honor that even if this app was already foreground.
        // Unknown events still require actual foreground activation.
        let noRestore = event?.paramDescriptor(forKeyword: 0x7273746f)?.enumCodeValue == 0x6e6f2020
        return isActive && !noRestore
    }
}
