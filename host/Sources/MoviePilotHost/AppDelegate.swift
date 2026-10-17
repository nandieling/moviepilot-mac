import AppKit
import ServiceManagement
import WebKit

/// 应用外壳：窗口、菜单栏、服务生命周期与诊断入口。
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var supervisor: ServiceSupervisor?
    private var coordinator: UpdateCoordinator?
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private var startButton: NSMenuItem!
    private var rememberPasswordItem: NSMenuItem?
    private let statusLabel = NSTextField(labelWithString: "正在启动…")
    private var loadDeadline = Date.distantPast
    /// 界面端口探测的轮次号：过期回调直接丢弃，免得几轮探测互相踩。
    private var probeToken = 0
    private var failedLoads = 0
    private var warnedAboutDelay = false
    private var showingSplash = false
    private let headless = CommandLine.arguments.contains("--headless")

    private var mode: ServiceMode = .attached(URL(string: "http://127.0.0.1:3000/")!)
    private var baseURL = URL(string: "http://127.0.0.1:3000/")!

    func applicationDidFinishLaunching(_ notification: Notification) {
        if !headless { buildMainMenu() }
        let layout = BundleLayout.current()
        mode = PortAllocator.resolve(
            backend: envPort("MOVPILOT_BACKEND_PORT", default: 3001),
            frontend: envPort("MOVPILOT_FRONTEND_PORT", default: 3000)
        )
        switch mode {
        case .attached(let url):
            // 已有一套 MoviePilot 在跑（通常是 CLI 版），只显示，不重复起服务。
            baseURL = url
            statusLabel.stringValue = "已连接到运行中的实例 · \(url.absoluteString)"
            startButton?.isHidden = true
        case .owned(let backendPort, let frontendPort):
            baseURL = URL(string: "http://127.0.0.1:\(frontendPort)/")!
            let supervisor = ServiceSupervisor(layout: layout, backendPort: backendPort, frontendPort: frontendPort)
            supervisor.onStateChange = { [weak self] state in self?.render(state) }
            self.supervisor = supervisor
            // 界面里的重启与安装更新都从这里过手；换载荷要等本进程退出后进行。
            coordinator = UpdateCoordinator(layout: layout, supervisor: supervisor) {
                NSApp.terminate(nil)
            }
        }
        buildStatusItem()
        if !headless { buildWindow() }
        supervisor?.start() ?? loadInterface()
        coordinator?.start()
    }

    /// 附加模式下没有子进程，界面直接加载。
    /// 自建服务的首屏可能在后端就绪之前发起，失败后在预算内自动重试，
    /// 否则用户只能对着空白页手动点重新载入。
    /// 先顶本地启动页，探到界面端口能正常响应再整页切过去：
    /// 子进程起来到端口就绪有一两秒，这期间直接加载只会白屏一闪一闪。
    private func loadInterface(withBudget budget: TimeInterval = 35) {
        guard let webView else { return }
        loadDeadline = Date().addingTimeInterval(budget)
        failedLoads = 0
        showSplash(.starting)
        probeToken += 1
        probeInterface(token: probeToken)
    }

    private func showSplash(_ reason: LaunchSplash.Reason) {
        showingSplash = true
        webView?.loadHTMLString(LaunchSplash.html(reason: reason), baseURL: nil)
    }

    private func probeInterface(token: Int) {
        var request = URLRequest(url: baseURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 2)
        request.httpMethod = "GET"
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let ready = (200..<400).contains(status)
            DispatchQueue.main.async { [weak self] in
                guard let self, token == self.probeToken, let webView = self.webView else { return }
                if ready {
                    // 端口通了，之前那些加载失败属于上一轮实例，不算数。
                    self.failedLoads = 0
                    self.showingSplash = false
                    webView.load(URLRequest(url: self.baseURL))
                    return
                }
                // 过了预算还在启动（首次装载插件能到几十秒）就把话说明白，但继续等：
                // 后台起来页面自然会出现，没必要停在死页上让用户自己摸。
                if Date() >= self.loadDeadline, !self.warnedAboutDelay {
                    self.warnedAboutDelay = true
                    self.showSplash(.timedOut)
                    self.statusLabel.stringValue = "后台服务仍在启动"
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + (self.warnedAboutDelay ? 3 : 0.3)) { [weak self] in
                    guard let self, token == self.probeToken else { return }
                    self.probeInterface(token: token)
                }
            }
        }.resume()
    }

    /// 页面加载失败多半是端口刚没；退回启动页继续探，连不上三次就停在提示上不再刷。
    private func retryLoad() {
        failedLoads += 1
        guard failedLoads < 3 else {
            showSplash(.timedOut)
            statusLabel.stringValue = "界面加载失败"
            return
        }
        statusLabel.stringValue = "正在启动…"
        loadInterface(withBudget: max(10, loadDeadline.timeIntervalSinceNow))
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }


    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        supervisor?.stop()
        return .terminateNow
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    // MARK: - 界面

    private func buildWindow() {
        let configuration = WKWebViewConfiguration()
        // 登录态和上次用的用户名都存在这份持久数据仓库里；换成 nonPersistent 每次都得重登。
        configuration.websiteDataStore = .default()
        configuration.userContentController.addUserScript(LoginAutofill.userScript())
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 820), configuration: configuration)
        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = true

        let made = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        // 代码建的窗口默认「关闭即释放」，可我们这边还留着引用；关一次窗口之后再点 Dock 图标，
        // 取到的就是已经没了的对象。改成不自动释放，窗口只是隐藏，重新显示即可。
        made.isReleasedWhenClosed = false
        made.title = "MoviePilot"
        made.titlebarAppearsTransparent = false
        made.contentView = webView
        made.center()
        made.setFrameAutosaveName("MoviePilotMainWindow")
        made.initialFirstResponder = webView
        window = made
        made.makeKeyAndOrderFront(nil)
        made.makeFirstResponder(webView)
        NSApp.activate(ignoringOtherApps: true)
        // 自检：编辑动作能否沿响应链走到网页（熄屏、无头下也能验证）
        if ProcessInfo.processInfo.environment["MOVPILOT_CHECK_EDIT"] == "1" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                let responder = self.window?.firstResponder ?? NSApp
                NSLog("[界面] 活动 \(NSApp.isActive)，keyWindow \(NSApp.keyWindow != nil)，"
                    + "firstResponder \(String(describing: type(of: responder)))")
                NSLog("[界面] WebView 支持 paste: \(self.webView?.responds(to: Selector(("paste:"))) ?? false)")
                NSRunningApplication.current.activate(options: [.activateAllWindows])
                for name in ["paste:", "selectAll:"] {
                    NSLog("[界面] \(name) 可达 \(NSApp.sendAction(Selector((name)), to: nil, from: nil))，"
                        + "活动 \(NSApp.isActive)")
                }
            }
        }
    }

    /// 标准主菜单。网页里的剪切板操作全靠这一组标准选择器：
    /// 没有 Edit 菜单就没有 ⌘X/⌘C/⌘V，WebKit 拼右键菜单时也按这些项来，
    /// 结果就是登录页连粘贴都点不出来。
    private func buildMainMenu() {
        let mainMenu = NSMenu()

        let appItem = mainMenu.addItem(withTitle: "MoviePilot", action: nil, keyEquivalent: "")
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "关于 MoviePilot",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        let servicesItem = appMenu.addItem(withTitle: "服务", action: nil, keyEquivalent: "")
        let services = NSMenu()
        servicesItem.submenu = services
        NSApp.servicesMenu = services
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 MoviePilot",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "隐藏其他",
                                         action: #selector(NSApplication.hideOtherApplications(_:)),
                                         keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "全部显示",
                        action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 MoviePilot",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let editItem = mainMenu.addItem(withTitle: "编辑", action: nil, keyEquivalent: "")
        let edit = NSMenu(title: "编辑")
        editItem.submenu = edit
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        let pastePlain = edit.addItem(withTitle: "粘贴并匹配样式",
                                      action: Selector(("pasteAsPlainText:")), keyEquivalent: "v")
        pastePlain.keyEquivalentModifierMask = [.command, .option, .shift]
        edit.addItem(withTitle: "删除", action: #selector(NSText.delete(_:)), keyEquivalent: "")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        let viewItem = mainMenu.addItem(withTitle: "显示", action: nil, keyEquivalent: "")
        let view = NSMenu(title: "显示")
        viewItem.submenu = view
        let reloadItem = view.addItem(withTitle: "重新载入", action: #selector(reload), keyEquivalent: "r")
        reloadItem.target = self
        let homeItem = view.addItem(withTitle: "回到入口", action: #selector(openInterface), keyEquivalent: "H")
        homeItem.target = self
        let purgeItem = view.addItem(withTitle: "清空界面缓存并重载",
                                     action: #selector(purgeCacheAndReload),
                                     keyEquivalent: "r")
        purgeItem.keyEquivalentModifierMask = [.command, .option]
        purgeItem.target = self
        view.addItem(.separator())
        view.addItem(withTitle: "进入全屏幕",
                     action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
            .keyEquivalentModifierMask = [.control, .command]

        let windowItem = mainMenu.addItem(withTitle: "窗口", action: nil, keyEquivalent: "")
        let windowMenu = NSMenu(title: "窗口")
        windowItem.submenu = windowMenu
        windowMenu.addItem(withTitle: "最小化",
                           action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "关闭",
                           action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = mainMenu
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
        let restartService = menu.addItem(withTitle: "重启后台服务", action: #selector(restartService), keyEquivalent: "")
        restartService.target = self
        restartService.isHidden = supervisor == nil
        rememberPasswordItem = menu.addItem(withTitle: "记住登录密码", action: #selector(toggleRememberPassword), keyEquivalent: "")
        rememberPasswordItem?.target = self
        rememberPasswordItem?.state = remembersPassword ? .on : .off
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

    /// 界面载入完成的时刻；早于后端本轮启动时刻就说明这一页属于上一个实例。
    private var interfaceLoadedAt: Date?

    private func render(_ state: ServiceSupervisor.State) {
        switch state {
        case .starting:
            statusLabel.stringValue = "正在启动…"
            startButton?.title = "停止服务"
        case .running:
            statusLabel.stringValue = "服务运行中 · \(baseURL.absoluteString)"
            startButton?.title = "停止服务"
            if webView != nil, webView.url == nil { loadInterface() }
            // 后端重启过就把这一页丢掉：页面里缓存的初始化状态、登录态都属于上一个实例。
            if let started = supervisor?.backendStartedAt, let loaded = interfaceLoadedAt, started > loaded {
                loadInterface()
            }
            if case .attached(let url) = mode { statusLabel.stringValue = "已连接到运行中的实例 · \(url.absoluteString)" }
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
        guard let window else {
            buildWindow()
            return
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - 菜单动作

    @objc private func reload() { webView?.reload() }

    @objc private func restartService() { supervisor?.restart() }

    @objc private func openInterface() { loadInterface() }

    /// 界面缓存留下过期状态时的兜底入口：先把 CacheStorage 清干净再整页重载。
    @objc private func purgeCacheAndReload() {
        let script = "window.caches ? caches.keys().then(function (names) {"
            + "return Promise.all(names.map(function (name) { return caches.delete(name) }))"
            + "}) : null"
        webView?.evaluateJavaScript(script, completionHandler: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.loadInterface() }
    }

    private var remembersPassword: Bool {
        get { UserDefaults.standard.object(forKey: "rememberLoginPassword") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "rememberLoginPassword") }
    }

    @objc private func toggleRememberPassword() {
        remembersPassword.toggle()
        rememberPasswordItem?.state = remembersPassword ? .on : .off
        applyPasswordPreference()
    }

    /// 开关状态写进页面本地存储，注入脚本每次进登录页时按它决定要不要填密码。
    private func applyPasswordPreference() {
        let script = remembersPassword
            ? "localStorage.setItem('MoviePilotApp.rememberPassword','1')"
            : "localStorage.setItem('MoviePilotApp.rememberPassword','0');localStorage.removeItem('MoviePilotApp.lastPassword')"
        webView?.evaluateJavaScript(script, completionHandler: nil)
    }

    @objc private func toggleService() {
        guard let supervisor else { return }
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
        environment["PYTHONPATH"] = layout.pythonPath()
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

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        retryLoad()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        retryLoad()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loadDeadline = .distantPast
        interfaceLoadedAt = Date()
        failedLoads = 0
        applyPasswordPreference()
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // 只有真正的站外网页链接才交给系统浏览器。启动页那种 about:blank 没有主机名，
        // 按「站外」处理会让 LaunchServices 去找「能打开 about:blank 的应用」，
        // 弹一个「未设定用来打开 URL about:blank 的应用程序」的框，窗口还留在白屏上。
        guard let url = navigationAction.request.url,
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = url.host, host != "127.0.0.1", host != "localhost" else {
            decisionHandler(.allow)
            return
        }
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }
}
