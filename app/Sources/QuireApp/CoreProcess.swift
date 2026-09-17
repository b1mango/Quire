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
        let process = Process()
        process.executableURL = resources.appendingPathComponent("python/bin/python3.12")
        process.arguments = ["-I", "-B", "-u", "-m", "quire", "ui", "--no-browser"]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.standardError
        // 核心进程据此在壳死亡（含 SIGKILL）后自行退出，保证无残留进程。
        var environment = ProcessInfo.processInfo.environment
        environment["QUIRE_SHELL_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        process.environment = environment
        process.terminationHandler = { [weak self] proc in
            guard let self, !self.stopping else { return }
            self.onExit("退出码 \(proc.terminationStatus)")
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
            self.onExit("核心进程 20 秒内未就绪")
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
