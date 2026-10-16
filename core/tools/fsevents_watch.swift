import CoreServices
import Darwin
import Foundation

private func flagNames(_ flags: FSEventStreamEventFlags) -> [String] {
    var names: [String] = []
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagMustScanSubDirs) != 0 { names.append("must_scan_subdirs") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagUserDropped) != 0 { names.append("user_dropped") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagKernelDropped) != 0 { names.append("kernel_dropped") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagEventIdsWrapped) != 0 { names.append("event_ids_wrapped") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagRootChanged) != 0 { names.append("root_changed") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagMount) != 0 { names.append("mount") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagUnmount) != 0 { names.append("unmount") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemCreated) != 0 { names.append("item_created") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemRemoved) != 0 { names.append("item_removed") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemRenamed) != 0 { names.append("item_renamed") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemModified) != 0 { names.append("item_modified") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemInodeMetaMod) != 0 { names.append("inode_metadata_modified") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemIsFile) != 0 { names.append("item_is_file") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemIsDir) != 0 { names.append("item_is_dir") }
    if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemIsSymlink) != 0 { names.append("item_is_symlink") }
    return names
}

private let outputLock = NSLock()

private func emit(_ object: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(object),
          let data = try? JSONSerialization.data(withJSONObject: object, options: []),
          let line = String(data: data, encoding: .utf8) else { return }
    outputLock.lock()
    FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
    outputLock.unlock()
}

var since = FSEventStreamEventId(kFSEventStreamEventIdSinceNow)
var roots: [String] = []
var index = 1
let arguments = CommandLine.arguments
while index < arguments.count {
    if arguments[index] == "--since", index + 1 < arguments.count {
        if let value = UInt64(arguments[index + 1]) { since = FSEventStreamEventId(value) }
        index += 2
    } else {
        roots.append((arguments[index] as NSString).expandingTildeInPath)
        index += 1
    }
}

// Persistable first-run boundary: using the opaque SinceNow sentinel would
// make a Python crash during the first baseline permanently lose that window.
if since == FSEventStreamEventId(kFSEventStreamEventIdSinceNow) {
    since = FSEventsGetCurrentEventId()
}

guard !roots.isEmpty else {
    FileHandle.standardError.write("usage: fsevents_watch [--since EVENT_ID] ROOT...\n".data(using: .utf8)!)
    exit(64)
}

let callback: FSEventStreamCallback = { _, _, count, eventPaths, eventFlags, eventIds in
    let paths = unsafeBitCast(eventPaths, to: NSArray.self) as! [String]
    for offset in 0..<count {
        let flags = eventFlags[offset]
        emit([
            "path": paths[offset],
            "event_id": String(eventIds[offset]),
            "observed_at": ISO8601DateFormatter().string(from: Date()),
            "flags": flagNames(flags),
            "change_kind": (flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemRemoved) != 0) ? "deleted" :
                ((flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemCreated) != 0) ? "created" :
                ((flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemRenamed) != 0) ? "renamed" : "modified"))
        ])
    }
}

var context = FSEventStreamContext(version: 0, info: nil, retain: nil, release: nil, copyDescription: nil)
let createFlags = FSEventStreamCreateFlags(
    kFSEventStreamCreateFlagFileEvents |
    kFSEventStreamCreateFlagNoDefer |
    kFSEventStreamCreateFlagUseCFTypes |
    kFSEventStreamCreateFlagWatchRoot |
    kFSEventStreamCreateFlagFullHistory
)
guard let stream = FSEventStreamCreate(
    kCFAllocatorDefault,
    callback,
    &context,
    roots as CFArray,
    since,
    0.25,
    createFlags
) else {
    FileHandle.standardError.write("failed to create FSEvents stream\n".data(using: .utf8)!)
    exit(70)
}

let eventQueue = DispatchQueue(label: "com.exampleuser.pam.fsevents.events")
let controlQueue = DispatchQueue(label: "com.exampleuser.pam.fsevents.control")
FSEventStreamSetDispatchQueue(stream, eventQueue)
guard FSEventStreamStart(stream) else {
    FSEventStreamInvalidate(stream)
    FSEventStreamRelease(stream)
    FileHandle.standardError.write("failed to start FSEvents stream\n".data(using: .utf8)!)
    exit(70)
}
emit([
    "type": "ready",
    "event_id": String(since),
    "root_count": roots.count
])

// Python requests a barrier after a baseline scan.  Capture the global event
// boundary before FlushSync, then emit the acknowledgement on the same serial
// queue as callbacks.  Therefore every watched event through that boundary is
// written before the barrier line.
let standardInput = FileHandle.standardInput
var inputBuffer = Data()
standardInput.readabilityHandler = { handle in
    let chunk = handle.availableData
    if chunk.isEmpty { return }
    controlQueue.async {
        inputBuffer.append(chunk)
        while let newline = inputBuffer.firstIndex(of: 0x0A) {
            let lineData = inputBuffer.prefix(upTo: newline)
            inputBuffer.removeSubrange(...newline)
            guard let object = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any],
                  object["type"] as? String == "barrier",
                  let token = object["token"] as? String,
                  !token.isEmpty else {
                emit(["type": "protocol_error"])
                continue
            }
            let boundary = FSEventsGetCurrentEventId()
            FSEventStreamFlushSync(stream)
            eventQueue.async {
                emit([
                    "type": "barrier",
                    "token": token,
                    "event_id": String(boundary),
                    "observed_at": ISO8601DateFormatter().string(from: Date())
                ])
            }
        }
    }
}
let parentPID = getppid()
let parentMonitor = DispatchSource.makeTimerSource(queue: DispatchQueue.main)
parentMonitor.schedule(deadline: .now() + 2.0, repeating: 2.0)
parentMonitor.setEventHandler {
    let currentParent = getppid()
    if currentParent == 1 || currentParent != parentPID {
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
        FSEventStreamRelease(stream)
        exit(0)
    }
}
parentMonitor.resume()
dispatchMain()
