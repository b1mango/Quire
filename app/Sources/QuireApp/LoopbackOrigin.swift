import Foundation

/// Bind native privileges to this launch's exact core origin, not arbitrary localhost services.
struct LoopbackOrigin {
    let port: Int

    init?(url: URL) {
        guard url.scheme == "http", url.host == "127.0.0.1",
            url.user == nil, url.password == nil,
            let port = url.port, (1...65535).contains(port)
        else { return nil }
        self.port = port
    }

    func contains(_ url: URL?) -> Bool {
        guard let url, let candidate = LoopbackOrigin(url: url) else { return false }
        return candidate.port == port
    }

    func allowsMessage(
        isMainFrame: Bool, frameURL: URL?, webViewURL: URL?,
        securityProtocol: String, securityHost: String, securityPort: Int
    ) -> Bool {
        isMainFrame && contains(frameURL) && contains(webViewURL)
            && securityProtocol == "http" && securityHost == "127.0.0.1" && securityPort == port
    }
}
