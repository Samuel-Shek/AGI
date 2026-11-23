import AppKit
import Foundation
import PAMInteractionModel

private final class CustomIconContent: NSView {}

@main struct PublicationExitIntegrationChecks {
    @MainActor static func main() throws {
        _ = NSApplication.shared
        let backgroundOpen = NSAppleEventDescriptor.appleEvent(
            withEventClass: 0x61657674, eventID: 0x72617070,
            targetDescriptor: nil, returnID: -1, transactionID: 0
        )
        backgroundOpen.setParam(NSAppleEventDescriptor(enumCode: 0x6e6f2020), forKeyword: 0x7273746f)
        assert(!ConsoleLaunchIntent.shouldShowOnReopen(isActive: false, event: backgroundOpen))
        assert(!ConsoleLaunchIntent.shouldShowOnReopen(isActive: true, event: backgroundOpen))
        assert(ConsoleLaunchIntent.shouldShowOnReopen(isActive: true, event: nil))
        assert(!ConsoleLaunchIntent.shouldShowOnReopen(isActive: false, event: nil))
        let root = FileManager.default.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("agi-exit-check-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: root) }
        let requestURL = root.appendingPathComponent("ui-exit-request.json")
        let receiptURL = root.appendingPathComponent("ui-exit-status.json")
        var request: [String: Any] = [:]
        func writeRequest(age: Double = 0, revision: String = "fixture") throws {
            request = ["pid":Int(ProcessInfo.processInfo.processIdentifier), "revision":revision,
                       "nonce":UUID().uuidString.lowercased(), "created":Date().timeIntervalSince1970-age]
            try JSONSerialization.data(withJSONObject: request).write(to: requestURL)
            try FileManager.default.setAttributes([.posixPermissions:0o600], ofItemAtPath: requestURL.path)
        }
        func receipt() throws -> [String: Any] {
            try JSONSerialization.jsonObject(with: Data(contentsOf: receiptURL)) as! [String: Any]
        }
        func expectSources(_ expected: [String]) throws {
            let value = try receipt()
            assert(value["interface_sources"] as? [String] == expected)
        }
        func settle(_ condition: () -> Bool) {
            let deadline = Date().addingTimeInterval(2)
            while !condition() && Date() < deadline { RunLoop.current.run(until:Date().addingTimeInterval(0.01)) }
            assert(condition(), "Native window state did not settle")
        }
        var attempts = 0, reply = NSApplication.TerminateReply.terminateCancel
        var beforeFinal: (@MainActor () -> Void)?
        var workbenchDirty = false
        var coordinator: IdlePublicationCoordinator!
        coordinator = IdlePublicationCoordinator(requestURL: requestURL, revision:"fixture", workbenchHasPendingInput: { workbenchDirty }) {
            attempts += 1; beforeFinal?(); reply = coordinator.shouldTerminate()
        }
        // Explicit console exit protects input without stopping any daemon.
        let manual = IdlePublicationCoordinator(requestURL: requestURL, revision: "fixture")
        assert(manual.shouldTerminate() == .terminateNow)
        PublicationDrafts.shared.update("manual-exit/text", initial: "", value: "synthetic draft")
        assert(manual.shouldTerminate() == .terminateCancel)
        let ticket = PublicationDrafts.shared.begin("manual-exit")
        assert(manual.shouldTerminate() == .terminateCancel)
        PublicationDrafts.shared.finish(ticket, .unknown)
        assert(manual.shouldTerminate() == .terminateCancel)
        PublicationDrafts.shared.finish(ticket, .clean)
        PublicationDrafts.shared.discard("manual-exit")
        assert(manual.shouldTerminate() == .terminateNow)
        let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "PSI 检查"
        defer { NSStatusBar.system.removeStatusItem(statusItem) }
        settle { statusItem.button?.window?.isVisible == true }
        assert(NSApp.windows.contains { $0 === statusItem.button?.window })
        try writeRequest()
        coordinator.poll()
        assert(attempts == 0, "An unidentified visible window must still block")
        let unidentified = try receipt()
        assert(unidentified["reasons"] as? [String] == ["interface_in_use"])
        assert(unidentified["interface_sources"] as? [String] == ["window"])
        coordinator.registerStatusItem(statusItem)
        let otherStatusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        otherStatusItem.button?.title = "PSI 检查 2"
        settle { otherStatusItem.button?.window?.isVisible == true }
        try writeRequest()
        coordinator.poll()
        assert(attempts == 0, "An unregistered status button must still block")
        NSStatusBar.system.removeStatusItem(otherStatusItem)
        settle { otherStatusItem.button?.window?.isVisible == false }
        let generic = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 80, height: 30),
                               styleMask: .borderless, backing: .buffered, defer: false)
        generic.isReleasedWhenClosed = false
        defer { generic.close() }
        generic.level = .statusBar
        generic.orderFront(nil)
        assert(!generic.canBecomeKey && !generic.canBecomeMain)
        try writeRequest()
        coordinator.poll()
        assert(attempts == 0, "A generic non-key window at status-bar level must block")
        generic.orderOut(nil)
        let custom = CustomIconContent(frame: .zero)
        statusItem.button?.addSubview(custom)
        try writeRequest()
        coordinator.poll()
        assert(attempts == 0, "Custom content inside an icon window must block")
        custom.removeFromSuperview()
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 60, height: 20))
        statusItem.button?.addSubview(input)
        try writeRequest()
        coordinator.poll()
        assert(attempts == 0, "An input control inside an icon window must block")
        input.removeFromSuperview()
        let gesture = NSClickGestureRecognizer()
        statusItem.button?.addGestureRecognizer(gesture)
        try writeRequest()
        coordinator.poll()
        assert(attempts == 0, "Added interaction inside an icon window must block")
        statusItem.button?.removeGestureRecognizer(gesture)
        statusItem.button?.window?.isDocumentEdited = true
        try writeRequest()
        coordinator.poll()
        assert(attempts == 0, "Edited state still blocks on the status item's window")
        statusItem.button?.window?.isDocumentEdited = false
        workbenchDirty = true
        try writeRequest()
        coordinator.poll()
        assert(attempts == 0, "A pending workbench draft or handoff must block an update")
        workbenchDirty = false
        try writeRequest()
        coordinator.poll()
        assert(attempts == 1 && reply == .terminateNow)
        let first = try receipt()
        assert(first["nonce"] as? String == request["nonce"] as? String)
        assert(Set(first.keys) == Set(["schema","instance","pid","revision","nonce","request_created","checked_at","reasons","can_exit","interface_sources"]))
        assert(first["interface_sources"] as? [String] == [])
        let window = NSWindow(contentRect:NSRect(x:-10000,y:-10000,width:160,height:80),
                              styleMask:[.titled,.miniaturizable], backing:.buffered, defer:false)
        window.isReleasedWhenClosed = false
        defer { window.close() }
        window.orderFront(nil)
        assert(window.isVisible)
        coordinator.poll(); assert(attempts == 1)
        try expectSources(["window"])
        window.miniaturize(nil)
        settle { window.isMiniaturized }
        try writeRequest()
        coordinator.poll(); assert(attempts == 1)
        try expectSources(["window"])
        window.deminiaturize(nil); settle { !window.isMiniaturized }; window.orderOut(nil)
        try writeRequest()
        window.isDocumentEdited = true
        coordinator.poll(); assert(attempts == 1)
        try expectSources(["edited"])
        window.isDocumentEdited = false
        coordinator.menuOpen = true
        coordinator.poll(); assert(attempts == 1)
        let blocked = try receipt(); assert(blocked["reasons"] as? [String] == ["interface_in_use"])
        assert(blocked["interface_sources"] as? [String] == ["menu_open"])
        coordinator.menuOpen = false
        coordinator.poll()
        let windowStates = NSApp.windows.map {
            ["class": String(describing: type(of: $0)), "visible": String($0.isVisible),
             "mini": String($0.isMiniaturized), "edited": String($0.isDocumentEdited),
             "status": String($0 === statusItem.button?.window)]
        }
        assert(attempts == 2 && reply == .terminateNow,
               "Menu-close state: active=\(NSApp.isActive), windows=\(windowStates)")
        let scope = "exit-fixture"
        PublicationDrafts.shared.update(scope + "/text", initial:"", value:"private local input")
        coordinator.poll(); assert(attempts == 2)
        let privateReceipt = try String(contentsOf: receiptURL, encoding:.utf8); assert(!privateReceipt.contains("private local input"))
        PublicationDrafts.shared.discard(scope)
        beforeFinal = { PublicationDrafts.shared.update(scope + "/text", initial:"", value:"arrived after check") }
        coordinator.poll(); assert(attempts == 3 && reply == .terminateCancel)
        PublicationDrafts.shared.discard(scope)
        beforeFinal = { coordinator.menuOpen = true }
        coordinator.poll(); assert(attempts == 4 && reply == .terminateCancel)
        try expectSources(["menu_open"])
        coordinator.menuOpen = false
        beforeFinal = { try! FileManager.default.removeItem(at: requestURL) }
        coordinator.poll(); assert(attempts == 5 && reply == .terminateCancel)
        let expired = try receipt(); assert(expired["reasons"] as? [String] == ["request_expired"])
        assert(expired["interface_sources"] == nil)
        beforeFinal = nil
        try writeRequest(age:3); coordinator.poll(); assert(attempts == 5)
        try writeRequest(revision:"other"); coordinator.poll(); assert(attempts == 5)
        try writeRequest(); coordinator.poll(); assert(attempts == 6 && reply == .terminateNow)
        beforeFinal = { window.orderFront(nil) }
        coordinator.poll(); assert(attempts == 7 && reply == .terminateCancel)
        try expectSources(["window"])
        window.orderOut(nil)
        beforeFinal = nil
        coordinator.start()
        coordinator.menuOpen = true
        try writeRequest()
        let trackingNonce = request["nonce"] as! String
        let trackingDeadline = Date().addingTimeInterval(2)
        while (try? receipt()["nonce"] as? String) != trackingNonce && Date() < trackingDeadline {
            _ = RunLoop.current.run(mode: .eventTracking, before: Date().addingTimeInterval(0.02))
        }
        let trackingReceipt = try receipt()
        assert(trackingReceipt["nonce"] as? String == trackingNonce, "Menu tracking must produce a fresh timer-driven receipt")
        assert(trackingReceipt["reasons"] as? [String] == ["interface_in_use"] && attempts == 7)
        assert(trackingReceipt["interface_sources"] as? [String] == ["menu_open"])
        coordinator.menuOpen = false
        try writeRequest()
        let closedNonce = request["nonce"] as! String
        let closedDeadline = Date().addingTimeInterval(2)
        while (try? receipt()["nonce"] as? String) != closedNonce && Date() < closedDeadline {
            _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
        let closedReceipt = try receipt()
        assert(closedReceipt["nonce"] as? String == closedNonce && closedReceipt["can_exit"] as? Bool == true)
        assert(closedReceipt["interface_sources"] as? [String] == [])
        assert(attempts > 7 && reply == .terminateNow)
        print("Native coordinator checks passed: real registered status-item window and AppKit display replicas, unidentified status window, input inside status window and edited status window, default state, real timer in menu tracking and after menu close, visible/minimized/edited windows, draft, final input/menu/window race, withdrawn request, expiry, identity, private receipt. Production delegate exit is a separate runtime check.")
    }
}
