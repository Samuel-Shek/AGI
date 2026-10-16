import CoreGraphics
import Foundation
import IOKit

struct SessionState: Codable {
    let state: String
    let locked: Bool
    let on_console: Bool?
    let evidence_source: String
    let cgs_lock_key_present: Bool
    let io_console_locked_present: Bool
}

struct ProbeRequest: Codable {
    let type: String
    let nonce: String
}

struct ProbeResponse: Codable {
    let nonce: String
    let session: SessionState
}

func ioConsoleLocked() -> Bool? {
    let root = IORegistryGetRootEntry(kIOMainPortDefault)
    guard root != MACH_PORT_NULL else { return nil }
    defer { IOObjectRelease(root) }
    guard let property = IORegistryEntryCreateCFProperty(
        root,
        "IOConsoleLocked" as CFString,
        kCFAllocatorDefault,
        0
    )?.takeRetainedValue() else {
        return nil
    }
    return property as? Bool
}

func probeSessionState() -> SessionState {
    // These probes deliberately stay inside this function. In --serve mode
    // every input line must re-read both sources; an earlier unlocked result is
    // never cached or reused as authorization for a later content read.
    let session = CGSessionCopyCurrentDictionary() as NSDictionary? ?? NSDictionary()
    let lockedValue = session["CGSSessionScreenIsLocked"] as? Bool
    let onConsole = session["kCGSSessionOnConsoleKey"] as? Bool
    let ioLockedValue = ioConsoleLocked()

    let stateName: String
    let evidenceSource: String
    if lockedValue == true || ioLockedValue == true {
        stateName = "locked"
        evidenceSource = lockedValue == true ? "session_state_helper_cgs" : "session_state_helper_io_registry"
    } else if lockedValue == false && onConsole == true {
        stateName = "unlocked"
        evidenceSource = "session_state_helper_cgs"
    } else if lockedValue == nil && ioLockedValue == false && onConsole == true {
        // Current macOS commonly omits CGSSessionScreenIsLocked while unlocked.
        // Require a second explicit local signal instead of treating absence alone
        // as proof. Any locked signal above still wins and fails closed.
        stateName = "unlocked"
        evidenceSource = "session_state_helper_io_registry"
    } else {
        stateName = "unknown"
        evidenceSource = "session_state_helper"
    }

    // Preserve the legacy Boolean for older callers, while making unknown and
    // off-console states fail closed. New callers should use `state` directly.
    return SessionState(
        state: stateName,
        locked: stateName != "unlocked",
        on_console: onConsole,
        evidence_source: evidenceSource,
        cgs_lock_key_present: lockedValue != nil,
        io_console_locked_present: ioLockedValue != nil
    )
}

func writeSessionState(_ state: SessionState, prettyPrinted: Bool) throws {
    let encoder = JSONEncoder()
    encoder.outputFormatting = prettyPrinted ? [.prettyPrinted, .sortedKeys] : [.sortedKeys]
    let data = try encoder.encode(state)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func requestNonce(_ line: String) -> String {
    guard let data = line.data(using: .utf8),
          let request = try? JSONDecoder().decode(ProbeRequest.self, from: data),
          request.type == "probe",
          request.nonce.count == 32,
          request.nonce.unicodeScalars.allSatisfy({
              CharacterSet(charactersIn: "0123456789abcdef").contains($0)
          }) else {
        return ""
    }
    return request.nonce
}

func writeProbeResponse(_ response: ProbeResponse) throws {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let data = try encoder.encode(response)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

if CommandLine.arguments.dropFirst().elementsEqual(["--serve"]) {
    while let line = readLine(strippingNewline: true) {
        // Probe even for a malformed request so every received line performs a
        // fresh security observation. An empty nonce makes the response
        // unusable, so the Python caller fails closed and retires this process.
        let response = ProbeResponse(
            nonce: requestNonce(line),
            session: probeSessionState()
        )
        try writeProbeResponse(response)
    }
} else {
    try writeSessionState(probeSessionState(), prettyPrinted: true)
}
