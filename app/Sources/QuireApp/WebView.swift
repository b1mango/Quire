import WebKit

/// 唯一窗口：WKWebView 指向内嵌核心打印的回环 URL（含本次启动令牌）。
final class QuireWindow: NSObject, NSWindowDelegate {
    var onClose: (() -> Void)?
    private let window: NSWindow

    init(url: URL) {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1120, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        super.init()
        window.title = "卷帙 Quire"
        window.minSize = NSSize(width: 880, height: 600)
        window.center()
        window.delegate = self
        let webView = WKWebView(frame: window.contentView!.bounds)
        webView.autoresizingMask = [.width, .height]
        window.contentView?.addSubview(webView)
        webView.load(URLRequest(url: url))
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) {
        onClose?()
    }
}
