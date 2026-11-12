import AppKit

/// A native hit-test layer: blank toolbar pixels drag, controls still reach WKWebView.
/// WKWebView ignores Chromium's CSS app-region, so use the DOM's actual layout instead.
final class WindowDragView: NSView {
    private var toolbar = NSRect.zero
    private var controls: [NSRect] = []
    override var isFlipped: Bool { true }

    func updateGeometry(_ body: Any) {
        guard let body = body as? [String: Any],
            let toolbar = Self.rect(body["toolbar"]),
            let items = body["controls"] as? [[Double]], items.count <= 256
        else {
            toolbar = .zero
            controls = []
            return
        }
        let controls = items.compactMap { Self.rect($0) }
        guard controls.count == items.count else {
            self.toolbar = .zero
            self.controls = []
            return
        }
        self.toolbar = toolbar
        self.controls = controls
    }

    private static func rect(_ value: Any?) -> NSRect? {
        guard let values = value as? [Double], values.count == 4,
            values.allSatisfy({ $0.isFinite }), values[2] >= 0, values[3] >= 0
        else { return nil }
        return NSRect(x: values[0], y: values[1], width: values[2], height: values[3])
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        let local = convert(point, from: superview)
        guard !isHidden, bounds.contains(local), toolbar.contains(local),
            !controls.contains(where: { $0.contains(local) })
        else { return nil }
        return self
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            // Mirror the title bar: follow the system's double-click action setting.
            if UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") == "Minimize" {
                window?.miniaturize(nil)
            } else {
                window?.zoom(nil)
            }
            return
        }
        window?.performDrag(with: event)
    }

    static let geometryScript = #"""
        (() => {
          // 顶部 52px 整条是拖拽带：侧栏顶部 + 当前视图工具条；其中的控件照常可点。
          const zones = () => [...document.querySelectorAll('.sb-top, .toolbar')]
            .filter((el) => el.getClientRects().length > 0);
          const selector = '.seg, button, a, input, textarea, select, label, '
            + '[contenteditable], [role="button"], [data-no-drag]';
          const rect = (element) => {
            const r = element.getBoundingClientRect();
            return [r.x, r.y, r.width, r.height];
          };
          let pending = false;
          const update = () => {
            if (pending) return;
            pending = true;
            requestAnimationFrame(() => {
              pending = false;
              const visible = zones();
              const height = Math.max(0, ...visible.map((el) => el.getBoundingClientRect().bottom));
              window.webkit.messageHandlers.windowDrag.postMessage({
                toolbar: visible.length ? [0, 0, window.innerWidth, height] : [0, 0, 0, 0],
                controls: visible.flatMap((el) => [...el.querySelectorAll(selector)]).map(rect)
                  .filter((r) => r[2] > 0 && r[3] > 0).slice(0, 256)
              });
            });
          };
          new ResizeObserver(update).observe(document.documentElement);
          new MutationObserver(update).observe(document.documentElement, {
            attributes: true, childList: true, subtree: true, characterData: true
          });
          window.addEventListener('resize', update);
          window.addEventListener('scroll', update, true);
          document.fonts.ready.then(update);
          update();
        })();
        """#
}
