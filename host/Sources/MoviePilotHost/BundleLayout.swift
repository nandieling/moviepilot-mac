import Foundation

/// 应用包内的目录布局，以及供本地调试用的开发态覆盖。
///
/// 正式包里 Python 运行时、后端源码和前端产物都在 Resources 下；
/// 设置 MOVPILOT_DEV_ROOT 指向一份 CLI 版仓库即可直接调试，无需完整打包。
struct BundleLayout {
    let backendRoot: URL
    let webRoot: URL
    let pythonHost: URL
    let python: URL
    let configDir: URL
    let logDir: URL
    /// 是否跑在正式包里：开发态那份是用户自己的 git 工作区，不往里塞软链。
    let isPackaged: Bool

    static let bundleIdentifier = "org.moviepilot.app"

    static func current(bundle: Bundle = .main) -> BundleLayout {
        let resources = bundle.resourceURL ?? URL(fileURLWithPath: "/")
        // 沿用上游同名环境变量：既能继承 CLI 版的既有数据目录，也方便测试指到临时目录。
        let configured = (ProcessInfo.processInfo.environment["CONFIG_DIR"] ?? "").trimmingCharacters(in: .whitespaces)
        let configDir = configured.isEmpty
            ? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/MoviePilot")
            : URL(fileURLWithPath: (configured as NSString).expandingTildeInPath)
        if let devRoot = ProcessInfo.processInfo.environment["MOVPILOT_DEV_ROOT"],
           !devRoot.isEmpty {
            let root = URL(fileURLWithPath: (devRoot as NSString).expandingTildeInPath)
            return BundleLayout(
                backendRoot: root,
                webRoot: root.appendingPathComponent("public"),
                pythonHost: resources.appendingPathComponent("pythonhost"),
                python: root.appendingPathComponent("venv/bin/python"),
                configDir: configDir,
                logDir: configDir.appendingPathComponent("logs"),
                isPackaged: false
            )
        }
        let runtime = resources.appendingPathComponent("runtime")
        return BundleLayout(
            backendRoot: resources.appendingPathComponent("app"),
            webRoot: resources.appendingPathComponent("web"),
            pythonHost: resources.appendingPathComponent("pythonhost"),
            python: runtime.appendingPathComponent("bin/python3"),
            configDir: configDir,
            logDir: configDir.appendingPathComponent("logs"),
            isPackaged: true
        )
    }

    /// 包外的插件仓库，由 PluginStore 接上上游认的那个路径。
    var pluginMirror: URL { configDir.appendingPathComponent("plugin_packages") }
    /// 出厂那份插件包标记的副本：数据目录被清空时靠它把 `app.plugins` 重新撑起来。
    var pluginPackageSeed: URL {
        pythonHost.deletingLastPathComponent().appendingPathComponent("plugin-package-init.py")
    }

    /// 传给两个子进程的 PYTHONPATH：只需随包的托管层。
    /// 解释器自己会找到包内 site-packages，猜路径反而容易猜错。
    func pythonPath() -> String { pythonHost.path }
}
