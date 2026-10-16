// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "PersonalAIMonitorMenuBar",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "PersonalAIMonitorMenuBar", targets: ["PersonalAIMonitorMenuBar"]),
        .executable(name: "PAMInteractionModelChecks", targets: ["PAMInteractionModelChecks"])
    ],
    targets: [
        .target(name: "PAMInteractionModel"),
        .executableTarget(
            name: "PersonalAIMonitorMenuBar",
            dependencies: ["PAMInteractionModel"]
        ),
        .executableTarget(
            name: "PAMInteractionModelChecks",
            dependencies: ["PAMInteractionModel"]
        ),
        .testTarget(
            name: "PAMInteractionModelTests",
            dependencies: ["PAMInteractionModel"]
        ),
    ]
)
