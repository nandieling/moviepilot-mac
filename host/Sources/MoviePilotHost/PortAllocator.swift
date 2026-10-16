import Darwin
import Foundation

/// 服务从属关系：自己拉起，或接一个已在运行的实例。
enum ServiceMode: Equatable {
    case attached(URL)
    case owned(backendPort: Int, frontendPort: Int)
}

/// 端口分配：优先用请求的端口，被占则判断是「已有 MoviePilot」还是别的程序。
///
/// CLI 版和开发实例经常同时在跑，直接抢端口只会得到一个启动失败弹窗。
enum PortAllocator {
    private static let defaultsKey = "MoviePilotPortPair"
    private static let maxShift = 20

    /// 测试脚本用 MOVPILOT_PORT_PERSIST=0 关掉记忆，避免上一轮记下的端口把这一轮带偏。
    private static let persistenceEnabled: Bool = {
        guard let raw = ProcessInfo.processInfo.environment["MOVPILOT_PORT_PERSIST"] else { return true }
        return raw == "1" || raw.lowercased() == "true"
    }()

    static func resolve(backend: Int, frontend: Int) -> ServiceMode {
        if isPortFree(backend) && isPortFree(frontend) {
            announce("使用请求端口 前端 \(frontend) / 后端 \(backend)")
            return .owned(backendPort: backend, frontendPort: frontend)
        }
        // 前端端口上已经是 MoviePilot：直接显示它，不再起第二套后端。
        if servesMoviePilot(on: frontend) {
            announce("端口 \(frontend) 已是 MoviePilot，直接接入")
            return .attached(URL(string: "http://127.0.0.1:\(frontend)/")!)
        }
        if let saved = saved(), isPortFree(saved.backendPort), isPortFree(saved.frontendPort) {
            announce("沿用上次端口 前端 \(saved.frontendPort) / 后端 \(saved.backendPort)")
            return .owned(backendPort: saved.backendPort, frontendPort: saved.frontendPort)
        }
        if let pair = firstFreePair(from: (backend, frontend)) {
            persist(pair)
            announce("端口被占，改用 前端 \(pair.frontendPort) / 后端 \(pair.backendPort)")
            return .owned(backendPort: pair.backendPort, frontendPort: pair.frontendPort)
        }
        // 一个可用端口都没有：交给进程报错，界面上能看到具体原因。
        announce("找不到可用端口，仍按 \(frontend)/\(backend) 启动")
        return .owned(backendPort: backend, frontendPort: frontend)
    }

    /// 端口空闲 = 没有进程在监听。用 connect 探活而不是 bind：
    /// bind 会把 TIME_WAIT 里的旧监听套接字也算成占用，退出后立刻重开就会误判成端口冲突、白换一个端口。
    static func isPortFree(_ port: Int) -> Bool {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return false }
        defer { close(descriptor) }
        let flags = fcntl(descriptor, F_GETFL)
        guard flags >= 0, fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) == 0 else { return false }

        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        address.sin_port = in_port_t(UInt16(port)).bigEndian
        let started = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { name in
                connect(descriptor, name, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if started == 0 { return false }            // 连上了：有人在监听
        if errno != EINPROGRESS { return true }     // 立刻被拒：没人监听

        var interest = pollfd(fd: descriptor, events: Int16(POLLOUT), revents: 0)
        guard poll(&interest, 1, 300) > 0 else {
            return false  // 连而不答，端口状态不明，保守当作已占用
        }
        var error: Int32 = 0
        var length = socklen_t(MemoryLayout<Int32>.size)
        guard getsockopt(descriptor, SOL_SOCKET, SO_ERROR, &error, &length) == 0 else { return false }
        return error != 0
    }

    /// 用初始化接口判断端口上是不是 MoviePilot，避免把别的程序当成自己人。
    static func servesMoviePilot(on port: Int) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/v1/login/initialization") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.0
        let semaphore = DispatchSemaphore(value: 0)
        var answer: String?
        let task = URLSession.shared.dataTask(with: request) { data, _, _ in
            answer = data.flatMap { String(data: $0, encoding: .utf8) }
            semaphore.signal()
        }
        task.resume()
        guard semaphore.wait(timeout: .now() + 1.5) == .success else {
            task.cancel()
            return false
        }
        return answer?.contains("\"success\"") == true
    }

    private static func firstFreePair(from start: (backend: Int, frontend: Int)) -> (backendPort: Int, frontendPort: Int)? {
        for offset in 1...maxShift {
            let candidate = (backendPort: start.backend + offset, frontendPort: start.frontend + offset)
            if isPortFree(candidate.backendPort), isPortFree(candidate.frontendPort) {
                return candidate
            }
        }
        return nil
    }

    private static func saved() -> (backendPort: Int, frontendPort: Int)? {
        guard persistenceEnabled,
              let raw = UserDefaults.standard.dictionary(forKey: defaultsKey),
              let backend = raw["backend"] as? Int,
              let frontend = raw["frontend"] as? Int else { return nil }
        return (backend, frontend)
    }

    private static func persist(_ pair: (backendPort: Int, frontendPort: Int)) {
        guard persistenceEnabled else { return }
        UserDefaults.standard.setValue(
            ["backend": pair.backendPort, "frontend": pair.frontendPort],
            forKey: defaultsKey
        )
    }

    private static func announce(_ message: String) {
        FileHandle.standardError.write("[端口] \(message)\n".data(using: .utf8)!)
    }
}
