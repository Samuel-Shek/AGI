import AppKit
import CoreImage
import CoreMedia
import CryptoKit
import Darwin
import Foundation
import ScreenCaptureKit

struct CaptureResult: Codable {
    let display_id: String
    let status: String
    let image_path: String?
    let frame_hash: String?
    let is_main: Bool?
    let width: Int?
    let height: Int?
    let display_frame: RectPayload?
    let visible_windows: [WindowPayload]?
    let error: String?
}

struct RectPayload: Codable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct WindowPayload: Codable {
    let window_id: String
    let app_name: String
    let bundle_id: String
    let title: String
    let frame: RectPayload
    let overlap_ratio: Double
}

struct WrittenFrame {
    let url: URL
    let frameHash: String
}

final class FrameSink: NSObject, SCStreamOutput {
    private(set) var pixelBuffer: CVPixelBuffer?
    private let semaphore = DispatchSemaphore(value: 0)
    private var didCapture = false

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, !didCapture else { return }
        guard sampleBuffer.isValid, let pixelBuffer = sampleBuffer.imageBuffer else { return }
        self.pixelBuffer = pixelBuffer
        didCapture = true
        semaphore.signal()
    }

    func waitForFrame(timeoutSeconds: TimeInterval) throws -> CVPixelBuffer {
        let deadline = DispatchTime.now() + timeoutSeconds
        guard semaphore.wait(timeout: deadline) == .success, let pixelBuffer else {
            throw CaptureError.timeout
        }
        return pixelBuffer
    }
}

enum CaptureError: Error, LocalizedError {
    case noDisplays
    case timeout
    case imageConversionFailed

    var errorDescription: String? {
        switch self {
        case .noDisplays:
            return "No displays available from ScreenCaptureKit"
        case .timeout:
            return "Timed out waiting for ScreenCaptureKit frame"
        case .imageConversionFailed:
            return "Could not convert captured frame to PNG"
        }
    }
}

enum SCKKeyframeCapture {
    static func captureAllDisplays(outputDir: URL) async throws -> [CaptureResult] {
        let content = try await SCShareableContent.current
        guard !content.displays.isEmpty else { throw CaptureError.noDisplays }

        var results: [CaptureResult] = []
        let mainDisplayID = CGMainDisplayID()
        for display in content.displays {
            let displayFrame = rectPayload(CGDisplayBounds(display.displayID))
            let windows = visibleWindows(for: display, windows: content.windows)
            do {
                let frame = try await capture(display: display, outputDir: outputDir)
                results.append(
                    CaptureResult(
                        display_id: String(display.displayID),
                        status: "captured",
                        image_path: frame.url.path,
                        frame_hash: frame.frameHash,
                        is_main: display.displayID == mainDisplayID,
                        width: Int(display.width),
                        height: Int(display.height),
                        display_frame: displayFrame,
                        visible_windows: windows,
                        error: nil
                    )
                )
            } catch {
                results.append(
                    CaptureResult(
                        display_id: String(display.displayID),
                        status: "error",
                        image_path: nil,
                        frame_hash: nil,
                        is_main: display.displayID == mainDisplayID,
                        width: Int(display.width),
                        height: Int(display.height),
                        display_frame: displayFrame,
                        visible_windows: windows,
                        error: error.localizedDescription
                    )
                )
            }
        }
        return results
    }

    static func capture(display: SCDisplay, outputDir: URL) async throws -> WrittenFrame {
        let config = SCStreamConfiguration()
        config.width = Int(display.width)
        config.height = Int(display.height)
        config.minimumFrameInterval = CMTime(value: 1, timescale: 2)
        config.queueDepth = 3
        config.showsCursor = false
        config.capturesAudio = false

        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let sink = FrameSink()
        let stream = SCStream(filter: filter, configuration: config, delegate: nil)

        try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: DispatchQueue(label: "sck-keyframe-output"))
        try await stream.startCapture()
        let pixelBuffer = try sink.waitForFrame(timeoutSeconds: 5)
        try? await stream.stopCapture()
        return try writePNG(pixelBuffer: pixelBuffer, displayID: display.displayID, outputDir: outputDir)
    }

    static func writePNG(pixelBuffer: CVPixelBuffer, displayID: CGDirectDisplayID, outputDir: URL) throws -> WrittenFrame {
        let ciImage = CIImage(cvPixelBuffer: pixelBuffer)
        let context = CIContext(options: nil)
        guard let cgImage = context.createCGImage(ciImage, from: ciImage.extent) else {
            throw CaptureError.imageConversionFailed
        }

        let bitmap = NSBitmapImageRep(cgImage: cgImage)
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            throw CaptureError.imageConversionFailed
        }

        let url = outputDir.appendingPathComponent("sck-\(displayID)-\(UUID().uuidString).png")
        try png.write(to: url, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        let digest = SHA256.hash(data: png)
        let frameHash = digest.map { String(format: "%02x", $0) }.joined()
        return WrittenFrame(url: url, frameHash: frameHash)
    }

    static func visibleWindows(for display: SCDisplay, windows: [SCWindow]) -> [WindowPayload] {
        let displayRect = CGDisplayBounds(display.displayID)
        guard displayRect.width > 0, displayRect.height > 0 else { return [] }
        let displayArea = displayRect.width * displayRect.height
        return windows.compactMap { window in
            guard let app = window.owningApplication else { return nil }
            let frame = window.frame
            guard frame.width > 16, frame.height > 16 else { return nil }
            let intersection = displayRect.intersection(frame)
            guard !intersection.isNull, intersection.width > 8, intersection.height > 8 else { return nil }
            let overlap = min(1.0, max(0.0, (intersection.width * intersection.height) / displayArea))
            guard overlap > 0.01 else { return nil }
            return WindowPayload(
                window_id: String(window.windowID),
                app_name: app.applicationName,
                bundle_id: app.bundleIdentifier,
                title: String((window.title ?? "").prefix(160)),
                frame: rectPayload(frame),
                overlap_ratio: Double(overlap)
            )
        }
        .sorted { left, right in
            if left.overlap_ratio == right.overlap_ratio {
                return left.app_name < right.app_name
            }
            return left.overlap_ratio > right.overlap_ratio
        }
        .prefix(8)
        .map { $0 }
    }

    static func rectPayload(_ rect: CGRect) -> RectPayload {
        RectPayload(
            x: Double(rect.origin.x),
            y: Double(rect.origin.y),
            width: Double(rect.size.width),
            height: Double(rect.size.height)
        )
    }

    static func printJSON<T: Encodable>(_ value: T) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(value)
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: sck_keyframe.swift <output-dir>\n".utf8))
    exit(2)
}

umask(0o077)

let outputDir = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try? FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)
try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: outputDir.path)

Task {
    do {
        let results = try await SCKKeyframeCapture.captureAllDisplays(outputDir: outputDir)
        try SCKKeyframeCapture.printJSON(results)
        exit(0)
    } catch {
        let result = CaptureResult(
            display_id: "unknown",
            status: "error",
            image_path: nil,
            frame_hash: nil,
            is_main: nil,
            width: nil,
            height: nil,
            display_frame: nil,
            visible_windows: nil,
            error: error.localizedDescription
        )
        try? SCKKeyframeCapture.printJSON([result])
        exit(1)
    }
}

dispatchMain()
