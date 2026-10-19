import Foundation
import os.log

/// 接管界面交给宿主的两件事：重启服务，以及把更新换进包里。
///
/// 上游的受管重启只认 Docker 里的 supervisor 和 CLI 留下的状态文件，应用版两样都没有；
/// 托管层因此把重启写成一份投递文件，这里取件执行。载荷落地交给随包的 mpweb.updater，
/// 因为整包重新签名会把正在运行的主程序页作废，必须等应用退出之后动手。
///
/// 版本从哪来也归这里管：增分包发在本仓库自己的 GitHub Release 上，
/// 查版本、下载、校验都在随包的 mpweb.release 里，这里负责定时唤起它和用户点的那一下。
final class UpdateCoordinator {
    private struct Request: Decodable {
        let action: String
        let zip: String?
    }

    /// 手动检查的回话，界面直接拿去显示。
    enum CheckOutcome {
        case installing
        case upToDate(String)
        case noChannel
        case failed

        var menuText: String {
            switch self {
            case .installing: return "正在安装更新…"
            case .upToDate(let version): return "已是最新（\(version)）"
            case .noChannel: return "这个包没配发布通道"
            case .failed: return "检查更新失败，详见日志"
            }
        }
    }

    private let layout: BundleLayout
    private let supervisor: ServiceSupervisor
    private let onQuit: () -> Void
    private let log = Logger(subsystem: BundleLayout.bundleIdentifier, category: "update")
    private let queue = DispatchQueue(label: "org.moviepilot.update")
    private var timer: DispatchSourceTimer?
    private var checker: DispatchSourceTimer?
    private var installing = false
    /// 组包时没写发布通道（或用户用环境变量关掉），就不再打网络
    private let checksEnabled: Bool

    init(layout: BundleLayout, supervisor: ServiceSupervisor, onQuit: @escaping () -> Void) {
        self.layout = layout
        self.supervisor = supervisor
        self.onQuit = onQuit
        let disabled = (ProcessInfo.processInfo.environment["MOVPILOT_UPDATE_CHECK"] ?? "").lowercased()
        checksEnabled = layout.isPackaged && disabled != "0" && disabled != "false"
    }

    func start(interval: TimeInterval = 2, firstCheck: TimeInterval = 90, checkInterval: TimeInterval = 12 * 3600) {
        let source = DispatchSource.makeTimerSource(queue: queue)
        source.schedule(deadline: .now() + interval, repeating: interval)
        source.setEventHandler { [weak self] in self?.collect() }
        timer = source
        source.resume()
        guard checksEnabled else { return }
        // 启动后先等一会儿：服务正在装载插件，这时候抢带宽和 CPU 不划算
        let poller = DispatchSource.makeTimerSource(queue: queue)
        poller.schedule(deadline: .now() + firstCheck, repeating: checkInterval)
        poller.setEventHandler { [weak self] in self?.checkForUpdates { _ in } }
        checker = poller
        poller.resume()
    }

    func stop() {
        timer?.cancel()
        timer = nil
        checker?.cancel()
        checker = nil
    }

    private func collect() {
        guard let request = read() else { return }
        try? FileManager.default.removeItem(at: layout.hostRequestFile)
        switch request.action {
        case "update":
            installUpdate(zip: request.zip)
        default:
            log.notice("界面请求重启服务")
            supervisor.restart()
        }
    }

    private func read() -> Request? {
        guard let data = FileManager.default.contents(atPath: layout.hostRequestFile.path),
              let request = try? JSONDecoder().decode(Request.self, from: data) else { return nil }
        return request
    }

    /// 交给包内的更新器落地，本进程随即退出；它装完会把应用再叫回来。
    /// 带 zip 的是本仓库增分包，不带的是上游自己下好的那份。
    private func installUpdate(zip: String?) {
        guard !installing else { return }
        installing = true
        var arguments = [
            "-m", "mpweb.updater", zip == nil ? "apply-staged" : "apply-zip",
            "--app", layout.bundleURL.path,
            "--config-dir", layout.configDir.path,
            "--wait-for-pid", String(ProcessInfo.processInfo.processIdentifier),
            "--relaunch",
        ]
        if let zip { arguments += ["--zip", zip] }
        let updater = spawnedProcess(arguments: arguments)
        updater.terminationHandler = { [weak self] process in
            // 没退成（比如载荷被拒）就得把服务拉回来，别让界面停在半路
            if process.terminationStatus != 0 {
                self?.installing = false
                self?.log.error("更新器退出码 \(process.terminationStatus)")
            }
        }
        log.notice("开始安装更新，应用即将退出")
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

    /// 查一次发布通道：取到新包会写投递文件，两秒内这次安装就接上了。
    func checkForUpdates(_ completion: @escaping (CheckOutcome) -> Void) {
        guard !installing else { return }
        guard checksEnabled else {
            completion(.noChannel)
            return
        }
        let checker = spawnedProcess(arguments: [
            "-m", "mpweb.release",
            "--app", layout.bundleURL.path,
            "--config-dir", layout.configDir.path,
        ])
        var output = Data()
        let pipe = Pipe()
        checker.standardOutput = pipe
        checker.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            output.append(handle.availableData)
        }
        checker.terminationHandler = { process in
            pipe.fileHandleForReading.readabilityHandler = nil
            let text = String(data: output, encoding: .utf8) ?? ""
            let outcome: CheckOutcome
            switch process.terminationStatus {
            case 0: outcome = .installing
            case 10:
                let version = text.components(separatedBy: "（").last?.components(separatedBy: "）").first ?? ""
                outcome = .upToDate(version)
            case 20: outcome = .noChannel
            default:
                outcome = .failed
                self.log.notice("检查更新：\(text.trimmingCharacters(in: .whitespacesAndNewlines), privacy: .public)")
            }
            completion(outcome)
        }
        do {
            try checker.run()
        } catch {
            log.error("检查更新启动失败：\(error.localizedDescription, privacy: .public)")
            completion(.failed)
        }
    }

    /// 包内解释器跑一个模块：更新器和发布通道查询共用这套环境。
    private func spawnedProcess(arguments: [String]) -> Process {
        let process = Process()
        process.executableURL = layout.python
        process.arguments = arguments
        process.currentDirectoryURL = layout.backendRoot
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = layout.pythonPath()
        environment["CONFIG_DIR"] = layout.configDir.path
        environment.removeValue(forKey: "PYTHONHOME")
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        try? FileManager.default.createDirectory(at: layout.logDir, withIntermediateDirectories: true)
        let logURL = layout.updateLog
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            process.standardOutput = handle
            process.standardError = handle
        }
        return process
    }
}
