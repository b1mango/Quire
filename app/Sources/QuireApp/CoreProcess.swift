import Foundation

/// 内嵌 Python 核心进程的生命周期管理。
///
/// 以 `python3.12 -I -B -u -m quire ui --no-browser` 启动（隔离模式：忽略
/// PYTHONPATH 等环境变量与用户 site-packages；-u 关闭输出缓冲，保证
/// URL 行即时可读），从 stdout 解析
/// `http://127.0.0.1:<port>/?token=<random>`；停止时先 SIGTERM，
/// 2 秒未退出则 SIGKILL，保证窗口关闭后无残留进程。
final class CoreProcess {
    private var process: Process?
    private var stopping = false
    private var ready = false
    private var logPath = ""
    private let onReady: (URL) -> Void
    private let onExit: (String) -> Void

    init(onReady: @escaping (URL) -> Void, onExit: @escaping (String) -> Void) {
        self.onReady = onReady
        self.onExit = onExit
    }

    func start() {
        guard let resources = Bundle.main.resourceURL else {
            onExit("应用包缺少 Resources")
            return
        }
        clearQuarantineIfNeeded()
        let log = prepareLog()
        logPath = log.path
        let process = Process()
        process.executableURL = resources.appendingPathComponent("python/bin/python3.12")
        process.arguments = ["-I", "-B", "-u", "-m", "quire", "ui", "--no-browser"]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = try! FileHandle(forWritingTo: log)
        // 核心进程据此在壳死亡（含 SIGKILL）后自行退出，保证无残留进程。
        var environment = ProcessInfo.processInfo.environment
        environment["QUIRE_SHELL_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        process.environment = environment
        process.terminationHandler = { [weak self] proc in
            guard let self, !self.stopping else { return }
            self.onExit(self.failureMessage(proc))
        }
        self.process = process
        do {
            try process.run()
        } catch {
            onExit(error.localizedDescription)
            return
        }
        DispatchQueue.global().async { self.readLoop(output.fileHandleForReading) }
        DispatchQueue.global().asyncAfter(deadline: .now() + 20) { [weak self] in
            guard let self, !self.ready, !self.stopping else { return }
            self.stopping = true
            process.terminate()
            self.onExit("核心进程 20 秒内未就绪。\n日志：\(self.logPath)")
        }
    }

    func stop() {
        stopping = true
        guard let process, process.isRunning else { return }
        process.terminate()
        let deadline = Date().addingTimeInterval(2)
        while process.isRunning && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
        }
    }

    // 浏览器下载的包带 com.apple.quarantine：应用经用户批准后自身能运行，
    // 但隔离标记会让 Gatekeeper 拦截任何子进程（弹"无法验证"框，用户点
    // 「完成」即 SIGTERM 杀掉核心，表现为"退出码 15"）。此时连 xattr 子
    // 进程都会被拦，只能在进程内逐个 removexattr。应用已在运行即用户已
    // 批准，拉起核心前清除自身包的隔离标记；App Translocation 的只读
    // 映射下删除失败也无害（该路径下 Gatekeeper 不拦截子进程）。
    private func clearQuarantineIfNeeded() {
        let bundle = Bundle.main.bundleURL.path
        let python = bundle + "/Contents/Resources/python/bin/python3.12"
        guard hasQuarantine(bundle) || hasQuarantine(python) else { return }
        removexattr(bundle, "com.apple.quarantine", XATTR_NOFOLLOW)
        guard let enumerator = FileManager.default.enumerator(atPath: bundle) else { return }
        for case let item as String in enumerator {
            removexattr(bundle + "/" + item, "com.apple.quarantine", XATTR_NOFOLLOW)
        }
    }

    private func hasQuarantine(_ path: String) -> Bool {
        getxattr(path, "com.apple.quarantine", nil, 0, 0, XATTR_NOFOLLOW) >= 0
    }

    // 核心的 stderr 落到数据目录日志，启动失败时弹窗给出路径便于定位。
    private func prepareLog() -> URL {
        let base = ProcessInfo.processInfo.environment["QUIRE_HOME"].map {
            URL(fileURLWithPath: $0)
        } ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/quire")
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        let log = base.appendingPathComponent("quire-core.log")
        try? "".write(to: log, atomically: true, encoding: .utf8)
        return log
    }

    private func failureMessage(_ process: Process) -> String {
        let how =
            process.terminationReason == .uncaughtSignal
            ? "被信号 \(process.terminationStatus) 终止" : "退出码 \(process.terminationStatus)"
        var text = "内嵌核心进程异常退出（\(how)）。\n日志：\(logPath)"
        if hasQuarantine(Bundle.main.bundleURL.path) {
            text +=
                "\n\n应用仍带下载隔离标记，核心可能因此被系统拦截。请到「系统设置 → 隐私与安全性」底部点 quire 的「仍要打开」；或在终端执行 xattr -dr com.apple.quarantine \"\(Bundle.main.bundleURL.path)\" 后重新打开。"
        } else {
            text += "\n\n若应用刚安装：右键点应用图标 →「打开」再试一次。"
        }
        return text
    }

    private func readLoop(_ handle: FileHandle) {
        let pattern = try! NSRegularExpression(
            pattern: #"http://127\.0\.0\.1:\d+/\?token=\S+"#
        )
        var pending = Data()
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { return }
            FileHandle.standardOutput.write(chunk)
            guard !ready else { continue }
            pending.append(chunk)
            guard let text = String(data: pending, encoding: .utf8) else { continue }
            let fullRange = NSRange(text.startIndex..., in: text)
            guard let match = pattern.firstMatch(in: text, range: fullRange),
                let range = Range(match.range, in: text),
                let url = URL(string: String(text[range]))
            else { continue }
            ready = true
            onReady(url)
        }
    }
}
