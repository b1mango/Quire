import Cocoa

/// 薄壳入口（项目设计.md §3.2）：壳只有窗口，没有业务逻辑。
/// 拉起内嵌 Python 核心 → WKWebView 指向回环 URL → 窗口关闭时终止子进程。
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var core: CoreProcess?
    private var window: QuireWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let core = CoreProcess(
            onReady: { [weak self] url in
                DispatchQueue.main.async { self?.showWindow(url: url) }
            },
            onExit: { [weak self] detail in
                DispatchQueue.main.async { self?.coreFailed(detail) }
            }
        )
        self.core = core
        core.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        core?.stop()
    }

    private func showWindow(url: URL) {
        let window = QuireWindow(url: url)
        window.onClose = { NSApp.terminate(nil) }
        self.window = window
        window.show()
    }

    private func coreFailed(_ detail: String) {
        let alert = NSAlert()
        alert.messageText = "卷帙无法启动"
        alert.informativeText = detail
        alert.runModal()
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
