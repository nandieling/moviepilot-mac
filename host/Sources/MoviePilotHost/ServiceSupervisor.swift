import Darwin
import Foundation
import os.log

/// 拉起并守住后端与前端托管层两个子进程。
///
/// 后端不再是 launchd 托管的 detached 守护进程，而是本应用的子进程：
/// 应用退出即整体退出，不会再出现只剩静态页面的假死界面。
final class ServiceSupervisor {
    enum State: Equatable {
        case stopped
        case starting
        case running(backendPort: Int, frontendPort: Int)
        case failed(String)
    }

    let backendPort: Int
    let frontendPort: Int

    private let layout: BundleLayout
    private let log = Logger(subsystem: BundleLayout.bundleIdentifier, category: "supervisor")
    private let queue = DispatchQueue(label: "org.moviepilot.supervisor")
    private var backend: Process?
    private var frontend: Process?
    private var restartCount = 0
    private var lastStart = Date.distantPast

    private(set) var state: State = .stopped
    /// 本轮子进程的启动时刻，界面据此判断自己是不是上一页旧状态。
    private(set) var backendStartedAt: Date?
    var onStateChange: ((State) -> Void)?

    init(layout: BundleLayout, backendPort: Int = 3001, frontendPort: Int = 3000) {
        self.layout = layout
        self.backendPort = backendPort
        self.frontendPort = frontendPort
    }

    var isRunning: Bool {
        backend?.isRunning == true && frontend?.isRunning == true
    }

    /// launchd 会话给子进程的 fd 软上限只有 256，站点连接一多就 accept 失败。
    static func raiseFileDescriptorLimit(to limit: Int = 10240) {
        var limits = rlimit(rlim_cur: 0, rlim_max: 0)
        guard getrlimit(RLIMIT_NOFILE, &limits) == 0 else { return }
        let target = min(UInt64(limit), limits.rlim_max)
        if limits.rlim_cur < target {
            limits.rlim_cur = target
            _ = setrlimit(RLIMIT_NOFILE, &limits)
        }
    }

    func start() { queue.sync { startLocked() } }
    func stop() { queue.sync { stopLocked() } }

    private func startLocked() {
        guard !isRunning else { return }
        Self.raiseFileDescriptorLimit()
        updateState(.starting)
        guard FileManager.default.isExecutableFile(atPath: layout.python.path) else {
            updateState(.failed("未找到内置 Python 运行时：\(layout.python.path)"))
            return
        }
        try? FileManager.default.createDirectory(at: layout.logDir, withIntermediateDirectories: true)
        // 先接好插件目录再起进程：后端一启动就会往那个路径装插件。
        PluginStore.prepare(layout: layout)

        var environment = ProcessInfo.processInfo.environment
        environment["CONFIG_DIR"] = layout.configDir.path
        environment["PORT"] = String(backendPort)
        environment["NGINX_PORT"] = String(frontendPort)
        environment["MOVIEPILOT_WEB_ROOT"] = layout.webRoot.path
        environment["MOVPILOT_HOST_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        environment["PYTHONPATH"] = layout.pythonPath()
        environment.removeValue(forKey: "PYTHONHOME")
        // 子进程输出重定向到文件时默认块缓冲，异常退出会丢掉最后几行
        environment["PYTHONUNBUFFERED"] = "1"

        let backendProcess = makeProcess(arguments: ["-m", "mpweb.backend"], environment: environment, logName: "backend")
        let frontendProcess = makeProcess(
            arguments: ["-m", "mpweb", "--port", String(frontendPort),
                        "--backend-port", String(backendPort)],
            environment: environment,
            logName: "webhost"
        )
        backendProcess.terminationHandler = { [weak self] in self?.handleExit(role: "后端", status: $0.terminationStatus) }
        frontendProcess.terminationHandler = { [weak self] in self?.handleExit(role: "前端托管层", status: $0.terminationStatus) }
        do {
            try backendProcess.run()
            try frontendProcess.run()
        } catch {
            backendProcess.terminate()
            frontendProcess.terminate()
            updateState(.failed("启动失败：\(error.localizedDescription)"))
            return
        }
        backend = backendProcess
        frontend = frontendProcess
        lastStart = Date()
        backendStartedAt = Date()
        updateState(.running(backendPort: backendPort, frontendPort: frontendPort))
    }

    private func stopLocked() {
        for process in [backend, frontend] {
            guard let process, process.isRunning else { continue }
            process.terminate()
            // 后端自身有优雅退出窗口，这里只给有限的等待时间。
            let deadline = Date().addingTimeInterval(35)
            while process.isRunning && Date() < deadline { usleep(100_000) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
        backend = nil
        frontend = nil
        backendStartedAt = nil
        updateState(.stopped)
    }

    private func makeProcess(arguments: [String], environment: [String: String], logName: String) -> Process {
        let process = Process()
        process.executableURL = layout.python
        process.arguments = arguments
        process.currentDirectoryURL = layout.backendRoot
        process.environment = environment
        let logURL = layout.logDir.appendingPathComponent("moviepilot.\(logName).app.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        // 自动重启时续写同一份日志：截断重写会把第一次失败的现场抹掉
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            process.standardOutput = handle
            process.standardError = handle
        }
        return process
    }

    private func handleExit(role: String, status: Int32) {
        queue.async {
            guard self.backend != nil || self.frontend != nil else { return }
            self.log.warning("\(role, privacy: .public) 退出，状态码 \(status)")
            if Date().timeIntervalSince(self.lastStart) > 300 { self.restartCount = 0 }
            self.restartCount += 1
            if self.restartCount >= 3 {
                self.stopLocked()
                self.updateState(.failed("\(role)连续异常退出 \(self.restartCount) 次，已停止自动重启"))
                return
            }
            self.stopLocked()
            self.startLocked()
        }
    }

    private func updateState(_ newState: State) {
        state = newState
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.onStateChange?(self.state)
        }
    }
}
