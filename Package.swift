// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "LungPrometheus",
    platforms: [
        .macOS(.v13)
    ],
    targets: [
        .executableTarget(
            name: "LungPrometheus"
        )
    ]
)
