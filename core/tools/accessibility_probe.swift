import AppKit
import ApplicationServices
import Foundation

struct AXNode: Codable {
    let role: String
    let subrole: String?
    let text: String
    let is_secure: Bool
}

struct AXPayload: Codable {
    let trusted: Bool
    let app_name: String
    let bundle_id: String
    let pid: Int32
    let secure_input_detected: Bool
    let text: String
    let nodes: [AXNode]
    let error: String?
}

let maxNodes = 80
let maxDepth = 5
let maxTextChars = 8000
var nodes: [AXNode] = []
var secureDetected = false
var textParts: [String] = []

func attribute(_ element: AXUIElement, _ name: String) -> Any? {
    var value: CFTypeRef?
    let result = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    guard result == .success else { return nil }
    return value
}

func stringAttribute(_ element: AXUIElement, _ name: String) -> String {
    return attribute(element, name) as? String ?? ""
}

func boolAttribute(_ element: AXUIElement, _ name: String) -> Bool {
    return attribute(element, name) as? Bool ?? false
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    guard let raw = attribute(element, kAXChildrenAttribute as String) else { return [] }
    return raw as? [AXUIElement] ?? []
}

func collect(_ element: AXUIElement, depth: Int) {
    guard depth <= maxDepth, nodes.count < maxNodes else { return }
    let role = stringAttribute(element, kAXRoleAttribute as String)
    let subrole = stringAttribute(element, kAXSubroleAttribute as String)
    let isSecure = role == "AXSecureTextField" || boolAttribute(element, "AXSecure")
    if isSecure {
        secureDetected = true
    }

    var text = ""
    if !isSecure {
        for attr in [kAXValueAttribute as String, kAXTitleAttribute as String, kAXDescriptionAttribute as String, "AXHelp"] {
            let candidate = stringAttribute(element, attr).trimmingCharacters(in: .whitespacesAndNewlines)
            if !candidate.isEmpty {
                text = candidate
                break
            }
        }
    }

    if !role.isEmpty || !text.isEmpty || isSecure {
        let trimmed = text.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).prefix(240)
        nodes.append(AXNode(role: role, subrole: subrole.isEmpty ? nil : subrole, text: String(trimmed), is_secure: isSecure))
        if !trimmed.isEmpty && textParts.joined(separator: " ").count < maxTextChars {
            textParts.append(String(trimmed))
        }
    }

    for child in children(element) {
        collect(child, depth: depth + 1)
        if nodes.count >= maxNodes { break }
    }
}

let trusted = AXIsProcessTrusted()
let frontmost = NSWorkspace.shared.frontmostApplication
let appName = frontmost?.localizedName ?? "Unknown"
let bundleId = frontmost?.bundleIdentifier ?? ""
let pid = frontmost?.processIdentifier ?? 0

if trusted, pid > 0 {
    let appElement = AXUIElementCreateApplication(pid)
    if let focusedWindow = attribute(appElement, kAXFocusedWindowAttribute as String) {
        collect(focusedWindow as! AXUIElement, depth: 0)
    } else {
        collect(appElement, depth: 0)
    }
}

let payload = AXPayload(
    trusted: trusted,
    app_name: appName,
    bundle_id: bundleId,
    pid: pid,
    secure_input_detected: secureDetected,
    text: textParts.joined(separator: "\n"),
    nodes: nodes,
    error: trusted ? nil : "accessibility_permission_not_granted"
)

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
let data = try encoder.encode(payload)
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))
