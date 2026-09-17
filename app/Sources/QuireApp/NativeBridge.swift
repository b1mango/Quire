import WebKit

/// The page-world clipboard endpoint is read-only. Drag geometry stays in an isolated world.
final class NativeBridge: NSObject, WKScriptMessageHandlerWithReply, WKScriptMessageHandler,
    WKNavigationDelegate
{
    weak var webView: WKWebView?
    weak var dragView: WindowDragView?
    private let origin: LoopbackOrigin?

    init(url: URL) {
        origin = LoopbackOrigin(url: url)
        super.init()
    }

    func install(on controller: WKUserContentController) {
        controller.addScriptMessageHandler(self, contentWorld: .page, name: "clipboard")
        controller.add(self, contentWorld: .defaultClient, name: "windowDrag")
        controller.addUserScript(
            WKUserScript(
                source: WindowDragView.geometryScript, injectionTime: .atDocumentEnd,
                forMainFrameOnly: true, in: .defaultClient
            )
        )
    }

    private func isTrusted(_ message: WKScriptMessage) -> Bool {
        guard let webView, message.webView === webView, let origin else { return false }
        let frame = message.frameInfo
        let security = frame.securityOrigin
        return origin.allowsMessage(
            isMainFrame: frame.isMainFrame, frameURL: frame.request.url, webViewURL: webView.url,
            securityProtocol: security.protocol, securityHost: security.host,
            securityPort: security.port
        )
    }

    // await window.webkit.messageHandlers.clipboard.postMessage({action: "readText"}) -> string
    // No text resolves to ""; invalid requests reject the Promise. Never writes the pasteboard.
    func userContentController(
        _ userContentController: WKUserContentController, didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        guard message.name == "clipboard", isTrusted(message) else {
            replyHandler(nil, "Clipboard access requires the trusted main frame.")
            return
        }
        guard let body = message.body as? [String: String], body.count == 1,
            body["action"] == "readText"
        else {
            replyHandler(nil, "Unsupported clipboard request.")
            return
        }
        replyHandler(NSPasteboard.general.string(forType: .string) ?? "", nil)
    }

    func userContentController(
        _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
    ) {
        guard message.name == "windowDrag", isTrusted(message) else { return }
        dragView?.updateGeometry(message.body)
    }

    func webView(
        _ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        // Never load external documents (or new windows) into a view with native capabilities.
        let allowed =
            navigationAction.targetFrame != nil
            && origin?.contains(navigationAction.request.url) == true
        decisionHandler(allowed ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        dragView?.updateGeometry([String: Any]())
    }
}
