import Foundation
import os.log

/// 接管界面交给宿主的两件事：重启服务，以及把已下载的更新换进包里。
///
/// 上游的受管重启只认 Docker 里的 supervisor 和 CLI 留下的状态文件，应用版两样都没有；
/// 托管层因此把重启写成一份投递文件，这里取件执行。载荷落地交给随包的 mpweb.updater，
/// 因为整包重新签名会把正在运行的主程序页作废，必须等应用退出之后动手。
final class UpdateCoordinator {
    private struct Request: Decodable {
        let action: String
    }

    private let layout: BundleLayout
    private let supervisor: ServiceSupervisor
    private let onQuit: () -> Void
    private let log = Logger(subsystem: BundleLayout.bundleIdentifier, category: "update")
    private let queue = DispatchQueue(label: "org.moviepilot.update")
    private var timer: DispatchSourceTimer?
    private var installing = false

    init(layout: BundleLayout, supervisor: ServiceSupervisor, onQuit: @escaping () -> Void) {
        self.layout = layout
        self.supervisor = supervisor
        self.onQuit = onQuit
    }

    func start(interval: TimeInterval = 2) {
        let source = DispatchSource.makeTimerSource(queue: queue)
        source.schedule(deadline: .now() + interval, repeating: interval)
        source.setEventHandler { [weak self] in self?.collect() }
        timer = source
        source.resume()
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    private func collect() {
        guard let action = read() else { return }
        try? FileManager.default.removeItem(at: layout.hostRequestFile)
        switch action {
        case "update":
            installStagedUpdate()
        default:
            log.notice("界面请求重启服务")
            supervisor.restart()
        }
    }

    private func read() -> String? {
        guard let data = FileManager.default.contents(atPath: layout.hostRequestFile.path),
              let request = try? JSONDecoder().decode(Request.self, from: data) else { return nil }
        return request.action
    }

    /// 交给包内的更新器落地，本进程随即退出；它装完会把应用再叫回来。
    private func installStagedUpdate() {
        guard !installing else { return }
        installing = true
        let updater = Process()
        updater.executableURL = layout.python
        updater.arguments = [
            "-m", "mpweb.updater", "apply-staged",
            "--app", layout.bundleURL.path,
            "--config-dir", layout.configDir.path,
            "--wait-for-pid", String(ProcessInfo.processInfo.processIdentifier),
            "--relaunch",
        ]
        updater.currentDirectoryURL = layout.backendRoot
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = layout.pythonPath()
        environment["CONFIG_DIR"] = layout.configDir.path
        environment.removeValue(forKey: "PYTHONHOME")
        environment["PYTHONUNBUFFERED"] = "1"
        updater.environment = environment
        try? FileManager.default.createDirectory(at: layout.logDir, withIntermediateDirectories: true)
        let logURL = layout.updateLog
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            updater.standardOutput = handle
            updater.standardError = handle
        }
        log.notice("开始安装已下载的更新，应用即将退出")
        do {
            try updater.run()
        } catch {
            installing = false
            log.error("更新器启动失败：\(error.localizedDescription, privacy: .public)")
            supervisor.restart()
            return
        }
        onQuit()
    }
}
