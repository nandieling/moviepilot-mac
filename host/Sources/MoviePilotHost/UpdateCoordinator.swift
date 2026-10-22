import Foundation
import AppKit
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
        let url: String?
    }

    /// 手动检查的回话，界面直接拿去显示。
    enum CheckOutcome {
        case installing
        case upToDate(String)
        case needsPackage(String)
        case noChannel
        case failed

        var menuText: String {
            switch self {
            case .installing: return "正在安装更新…"
            case .upToDate(let version): return "已是最新（\(version)）"
            case .needsPackage(let version): return "新版本 \(version) 要装完整安装包"
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
    /// 包里写了发布通道、也没被环境变量禁掉，才允许打这个网络
    private let channelAvailable: Bool
    private var prefs: HostPrefs
    /// 定时检查的节奏，start 时定下来，开关来回切也沿用同一份
    private var checkSchedule: (first: TimeInterval, every: TimeInterval) = (90, 12 * 3600)

    /// 界面上「自动检查更新」的开关状态。
    var autoCheck: Bool { channelAvailable && prefs.autoUpdateCheck }
    /// 环境变量硬关掉时，界面上那个开关也没意义了。
    var canToggleAutoCheck: Bool { channelAvailable }

    init(layout: BundleLayout, supervisor: ServiceSupervisor, onQuit: @escaping () -> Void) {
        self.layout = layout
        self.supervisor = supervisor
        self.onQuit = onQuit
        let disabled = (ProcessInfo.processInfo.environment["MOVPILOT_UPDATE_CHECK"] ?? "").lowercased()
        channelAvailable = layout.isPackaged && disabled != "0" && disabled != "false"
        prefs = HostPrefs.load(layout)
    }

    func start(interval: TimeInterval = 2, firstCheck: TimeInterval = 90, checkInterval: TimeInterval = 12 * 3600) {
        let source = DispatchSource.makeTimerSource(queue: queue)
        source.schedule(deadline: .now() + interval, repeating: interval)
        source.setEventHandler { [weak self] in self?.collect() }
        timer = source
        source.resume()
        checkSchedule = (firstCheck, checkInterval)
        scheduleChecker()
    }

    /// 按当前开关决定要不要定时查版本：启动后先等一会儿，
    /// 那会儿服务正在装载插件，抢带宽和 CPU 不划算。
    private func scheduleChecker() {
        checker?.cancel()
        checker = nil
        guard autoCheck else { return }
        let poller = DispatchSource.makeTimerSource(queue: queue)
        poller.schedule(deadline: .now() + checkSchedule.first, repeating: checkSchedule.every)
        poller.setEventHandler { [weak self] in self?.checkForUpdates(manual: false) { _ in } }
        checker = poller
        poller.resume()
    }

    /// 界面上「自动检查更新」：改完立刻生效，并落到数据目录里，下次启动还认。
    func setAutoCheck(_ on: Bool) {
        prefs.autoUpdateCheck = on
        prefs.save(layout)
        note(on ? "自动检查已开启" : "自动检查已关闭，只剩手动检查")
        queue.async { self.scheduleChecker() }
    }

    private static let stamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }()

    /// 开关动作也写进更新流水账：离线用时得能确认自己关掉了什么。
    private func note(_ message: String) {
        guard let data = "\(Self.stamp.string(from: Date())) 发布通道：\(message)\n".data(using: .utf8) else { return }
        try? FileManager.default.createDirectory(at: layout.logDir, withIntermediateDirectories: true)
        if let handle = try? FileHandle(forWritingTo: layout.updateLog) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: layout.updateLog)
        }
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
        case "open":
            openInBrowser(request.url)
        default:
            log.notice("界面请求重启服务")
            supervisor.restart()
        }
    }

    /// 用系统默认浏览器打开地址：界面里点「更新」时用它去发布页下载安装包。
    private func openInBrowser(_ target: String?) {
        guard let target, let url = URL(string: target) else { return }
        log.notice("界面请求打开更新页面：\(target, privacy: .public)")
        DispatchQueue.main.async {
            NSWorkspace.shared.open(url)
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
    /// `manual` 是用户在菜单里点的：依赖清单变了那种装不上的情况，只有手点才顺手开浏览器。
    func checkForUpdates(manual: Bool = true, _ completion: @escaping (CheckOutcome) -> Void) {
        guard !installing else { return }
        guard channelAvailable else {
            completion(.noChannel)
            return
        }
        var arguments = [
            "-m", "mpweb.release",
            "--app", layout.bundleURL.path,
            "--config-dir", layout.configDir.path,
        ]
        if manual { arguments.append("--interactive") }
        let checker = spawnedProcess(arguments: arguments)
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
            case 30:
                let version = text.components(separatedBy: "（").last?.components(separatedBy: "）").first ?? ""
                outcome = .needsPackage(version)
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
