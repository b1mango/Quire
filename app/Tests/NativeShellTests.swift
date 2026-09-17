import AppKit

/// Headless checks: no application launch, window, event dispatch or pasteboard access.
@main
struct NativeShellTests {
    static func main() {
        testOriginBoundary()
        testDragHitTesting()
        testEditMenu()
        print("PASS: loopback/main-frame boundaries, drag hit-testing, Edit menu shortcuts")
    }

    private static func testOriginBoundary() {
        let launch = URL(string: "http://127.0.0.1:43129/?token=test")!
        let origin = LoopbackOrigin(url: launch)!
        precondition(origin.contains(URL(string: "http://127.0.0.1:43129/#library")))
        let rejected = [
            "https://127.0.0.1:43129/", "http://127.0.0.1:43130/", "http://127.0.0.1/",
            "http://localhost:43129/", "http://[::1]:43129/", "http://127.0.0.1.evil.test:43129/",
            "http://user@127.0.0.1:43129/", "file:///tmp/index.html", "about:blank",
            "http://127.0.0.1:0/", "http://127.0.0.1:65536/",
        ]
        for value in rejected {
            precondition(!origin.contains(URL(string: value)), "Accepted unexpected URL: \(value)")
        }
        precondition(!origin.contains(nil))
        precondition(LoopbackOrigin(url: URL(string: "https://example.com/")!) == nil)
        func allowed(
            main: Bool = true, frame: URL? = launch, view: URL? = launch,
            proto: String = "http", host: String = "127.0.0.1", port: Int = 43129
        ) -> Bool {
            origin.allowsMessage(
                isMainFrame: main, frameURL: frame, webViewURL: view,
                securityProtocol: proto, securityHost: host, securityPort: port
            )
        }
        precondition(allowed())
        precondition(!allowed(main: false))  // Same-origin iframes are not privileged.
        precondition(!allowed(frame: URL(string: "http://127.0.0.1:43130/")))
        precondition(!allowed(view: URL(string: "https://example.com/")))
        precondition(!allowed(frame: nil))
        precondition(!allowed(view: nil))
        precondition(!allowed(proto: "https"))
        precondition(!allowed(host: "localhost"))
        precondition(!allowed(host: ""))  // Opaque/sandboxed security origin.
        precondition(!allowed(port: 43130))
    }

    private static func testDragHitTesting() {
        let parent = NSView(frame: NSRect(x: 0, y: 0, width: 1120, height: 760))
        let drag = WindowDragView(frame: parent.bounds)
        parent.addSubview(drag)
        func hit(_ x: Double, _ y: Double) -> Bool {
            drag.hitTest(drag.convert(NSPoint(x: x, y: y), to: parent)) === drag
        }
        precondition(!hit(120, 20))  // Fail closed before geometry arrives.
        // Match WebKit's Foundation/NSNumber representation of a JavaScript message.
        let json = #"{"toolbar":[0,0,1120,64],"controls":[[720,14,376,36]]}"#
        drag.updateGeometry(try! JSONSerialization.jsonObject(with: Data(json.utf8)))
        precondition(hit(120, 20))  // Brand and blank area.
        precondition(hit(400, 32))
        precondition(!hit(800, 32))  // Theme controls still go to WebKit.
        precondition(!hit(120, 120))  // Page content is not draggable.
        precondition(!hit(-1, 20))
        precondition(!hit(1121, 20))
        drag.updateGeometry([
            "toolbar": [0.0, 0, 880, 100] as [Double],
            "controls": [[88.0, 60, 760, 36]] as [[Double]],
        ])
        precondition(hit(400, 30))
        precondition(!hit(400, 80))  // Wrapped toolbar controls.
        for malformed: Any in [
            [String: Any](),
            ["toolbar": [0.0, 0, -1, 64], "controls": [[Double]]()],
            ["toolbar": [0.0, 0, Double.infinity, 64], "controls": [[Double]]()],
            ["toolbar": [0.0, 0, 1120, 64] as [Double], "controls": [[1.0, 2, 3]]],
            [
                "toolbar": [0.0, 0, 1120, 64] as [Double],
                "controls": Array(repeating: [0.0, 0, 1, 1], count: 257),
            ],
        ] {
            drag.updateGeometry(malformed)
            precondition(!hit(120, 20))
        }
    }

    private static func testEditMenu() {
        let menu = makeApplicationMenu()
        let edit = menu.items.first(where: { $0.title == "编辑" })!.submenu!
        for (selector, key) in [
            ("cut:", "x"), ("copy:", "c"), ("paste:", "v"), ("selectAll:", "a"), ("undo:", "z"),
        ] {
            let item = edit.items.first(where: { $0.action == Selector(selector) })!
            precondition(item.keyEquivalent == key)
            precondition(item.keyEquivalentModifierMask == .command)
            precondition(item.target == nil)
        }
        let redo = edit.items.first(where: { $0.action == Selector(("redo:")) })!
        precondition(redo.keyEquivalent == "z")
        precondition(redo.keyEquivalentModifierMask == [.command, .shift])
    }
}
