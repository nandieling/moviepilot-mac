import AppKit
import ServiceManagement
import WebKit

/// 应用外壳：窗口、菜单栏、服务生命周期与诊断入口。
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var supervisor: ServiceSupervisor!
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private var startButton: NSMenuItem!
    private let statusLabel = NSTextField(labelWithString: "正在启动…")
    private let headless = CommandLine.arguments.contains("--headless")

    private var baseURL: URL {
        URL(string: "http://127.0.0.1:\(supervisor.frontendPort)/")!
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let layout = BundleLayout.current()
        supervisor = ServiceSupervisor(
            layout: layout,
            backendPort: envPort("MOVPILOT_BACKEND_PORT", default: 3001),
            frontendPort: envPort("MOVPILOT_FRONTEND_PORT", default: 3000)
        )
        supervisor.onStateChange = { [weak self] state in self?.render(state) }
        buildStatusItem()
        if !headless { buildWindow() }
        supervisor.start()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        supervisor.stop()
        return .terminateNow
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    // MARK: - 界面

    private func buildWindow() {
        let configuration = WKWebViewConfiguration()
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 820), configuration: configuration)
        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = true

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "MoviePilot"
        window.titlebarAppearsTransparent = false
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("MoviePilotMainWindow")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "film.stack", accessibilityDescription: "MoviePilot")
        let menu = NSMenu()
        menu.addItem(withTitle: "打开 MoviePilot", action: #selector(showMainWindow), keyEquivalent: "o").target = self
        menu.addItem(withTitle: "重新载入界面", action: #selector(reload), keyEquivalent: "r").target = self
        menu.addItem(.separator())
        startButton = menu.addItem(withTitle: "启动服务", action: #selector(toggleService), keyEquivalent: "")
        startButton?.target = self
        menu.addItem(withTitle: "打开日志目录", action: #selector(openLogDir), keyEquivalent: "").target = self
        menu.addItem(withTitle: "运行诊断", action: #selector(runDiagnostics), keyEquivalent: "d").target = self
        menu.addItem(.separator())
        let login = menu.addItem(withTitle: "开机自启", action: #selector(toggleLoginItem), keyEquivalent: "")
        login.target = self
        login.state = loginItemEnabled ? .on : .off
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 MoviePilot", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        statusItem.menu = menu
    }

    private func render(_ state: ServiceSupervisor.State) {
        switch state {
        case .starting:
            statusLabel.stringValue = "正在启动…"
            startButton?.title = "停止服务"
        case .running:
            statusLabel.stringValue = "服务运行中 · \(baseURL.absoluteString)"
            startButton?.title = "停止服务"
            if webView != nil, webView.url == nil { webView.load(URLRequest(url: baseURL)) }
        case .stopped:
            statusLabel.stringValue = "服务已停止"
            startButton?.title = "启动服务"
        case .failed(let reason):
            statusLabel.stringValue = "启动失败"
            startButton?.title = "重试启动"
            presentFailure(reason)
        }
    }

    private func presentFailure(_ reason: String) {
        guard !headless else { return }
        let alert = NSAlert()
        alert.messageText = "MoviePilot 未能启动"
        alert.informativeText = reason
        alert.addButton(withTitle: "查看日志")
        alert.addButton(withTitle: "好")
        if alert.runModal() == .alertFirstButtonReturn { openLogDir() }
    }

    @objc private func showMainWindow() {
        guard let window else { return }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - 菜单动作

    @objc private func reload() { webView?.reload() }

    @objc private func toggleService() {
        supervisor.isRunning ? supervisor.stop() : supervisor.start()
    }

    @objc private func openLogDir() {
        NSWorkspace.shared.open(BundleLayout.current().logDir)
    }

    @objc private func runDiagnostics() {
        let layout = BundleLayout.current()
        let process = Process()
        process.executableURL = layout.python
        process.arguments = ["-m", "app.cli", "doctor", "--json", "--deep"]
        process.currentDirectoryURL = layout.backendRoot
        var environment = ProcessInfo.processInfo.environment
        environment["CONFIG_DIR"] = layout.configDir.path
        environment["PYTHONPATH"] = layout.pythonPath(sitePackages: nil)
        process.environment = environment
        let output = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("moviepilot-doctor-\(Int(Date().timeIntervalSince1970)).json")
        FileManager.default.createFile(atPath: output.path, contents: nil)
        guard let handle = try? FileHandle(forWritingTo: output) else { return }
        process.standardOutput = handle
        process.standardError = handle
        process.terminationHandler = { _ in NSWorkspace.shared.open(output) }
        try? process.run()
    }

    private var loginItemEnabled: Bool {
        if #available(macOS 13.0, *) { SMAppService.mainApp.status == .enabled } else { false }
    }

    @objc private func toggleLoginItem() {
        guard #available(macOS 13.0, *) else { return }
        if loginItemEnabled {
            try? SMAppService.mainApp.unregister()
        } else {
            try? SMAppService.mainApp.register()
        }
        statusItem.menu?.update()
    }

    private func envPort(_ name: String, default fallback: Int) -> Int {
        let raw = ProcessInfo.processInfo.environment[name] ?? ""
        return Int(raw) ?? fallback
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // 站外链接交给系统浏览器，避免界面里跳出无关页面。
        if let url = navigationAction.request.url, url.host != "127.0.0.1", url.host != "localhost" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}
