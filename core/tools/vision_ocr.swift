import AppKit
import Foundation
import Vision

struct OCRBoundingBox: Codable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct OCRLine: Codable {
    let text: String
    let confidence: Float
    let bounding_box: OCRBoundingBox
}

struct OCRPayload: Codable {
    let lines: [OCRLine]
    let image_width: Int
    let image_height: Int
    let coordinate_space: String
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: vision_ocr.swift <image-path>\n".utf8))
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: imageURL) else {
    FileHandle.standardError.write(Data("unable to load image\n".utf8))
    exit(3)
}

var proposedRect = CGRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &proposedRect, context: nil, hints: nil) else {
    FileHandle.standardError.write(Data("unable to create cgImage\n".utf8))
    exit(4)
}

var lines: [OCRLine] = []
let request = VNRecognizeTextRequest { request, error in
    if let error = error {
        FileHandle.standardError.write(Data("vision error: \(error.localizedDescription)\n".utf8))
        return
    }
    let observations = request.results as? [VNRecognizedTextObservation] ?? []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let rect = observation.boundingBox
        let box = OCRBoundingBox(
            x: Double(rect.origin.x),
            y: Double(rect.origin.y),
            width: Double(rect.width),
            height: Double(rect.height)
        )
        lines.append(OCRLine(text: candidate.string, confidence: candidate.confidence, bounding_box: box))
    }
}

request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
    let payload = OCRPayload(
        lines: lines,
        image_width: cgImage.width,
        image_height: cgImage.height,
        coordinate_space: "vision_normalized_bottom_left"
    )
    let data = try JSONEncoder().encode(payload)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    FileHandle.standardError.write(Data("ocr failed: \(error.localizedDescription)\n".utf8))
    exit(5)
}
