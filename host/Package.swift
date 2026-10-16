// swift-tools-version:6.0
import PackageDescription

// 用 v5 语言模式编译：AppKit 主线程约定与 Swift 6 严格并发检查
// 会在这一层产生大量与正确性无关的噪声。
let package = Package(
    name: "MoviePilotHost",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "MoviePilotHost",
            path: "Sources/MoviePilotHost",
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
