import AppKit
import Foundation

// Package the text-free tree artwork in a consistent macOS icon silhouette.
struct IconSize { let filename: String; let pixels: Int }
let sizes = [
    IconSize(filename: "icon_16x16.png", pixels: 16),
    IconSize(filename: "icon_16x16@2x.png", pixels: 32),
    IconSize(filename: "icon_32x32.png", pixels: 32),
    IconSize(filename: "icon_32x32@2x.png", pixels: 64),
    IconSize(filename: "icon_128x128.png", pixels: 128),
    IconSize(filename: "icon_128x128@2x.png", pixels: 256),
    IconSize(filename: "icon_256x256.png", pixels: 256),
    IconSize(filename: "icon_256x256@2x.png", pixels: 512),
    IconSize(filename: "icon_512x512.png", pixels: 512),
    IconSize(filename: "icon_512x512@2x.png", pixels: 1024),
]
guard CommandLine.arguments.count == 2 else { exit(2) }
let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
let source = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
    .deletingLastPathComponent().appendingPathComponent("assets/agi/AGI-app-icon.png")
guard let artwork = NSImage(contentsOf: source) else {
    fputs("Missing approved PSI icon artwork\n", stderr)
    exit(3)
}
for size in sizes {
    let image = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size.pixels, pixelsHigh: size.pixels,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: image)
    NSGraphicsContext.current?.imageInterpolation = .high
    let side = CGFloat(size.pixels)
    let tile = NSRect(x: side * 0.09, y: side * 0.09, width: side * 0.82, height: side * 0.82)
    NSBezierPath(roundedRect: tile, xRadius: side * 0.18, yRadius: side * 0.18).addClip()
    artwork.draw(in: tile,
        from: .zero, operation: .copy, fraction: 1)
    NSGraphicsContext.restoreGraphicsState()
    try image.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent(size.filename))
}
