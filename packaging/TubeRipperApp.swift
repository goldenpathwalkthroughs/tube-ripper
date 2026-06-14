// TUBE-RIPPER DELUXE 2000 — native macOS wrapper.
//
// A real universal Mach-O app (so Finder always launches it natively — no
// Rosetta, no "won't be supported" prompt, no instant-quit on Apple Silicon),
// hosting the local UI in its OWN WKWebView window instead of a browser tab.
//
// It starts the bundled Python server as a subprocess on a private port,
// waits for it, then loads it. On a self-update the server exits with code 42;
// we relaunch it and reload the window. Closing the window quits everything.

import Cocoa
import WebKit

// ---- helpers ---------------------------------------------------------------

func isAppleSilicon() -> Bool {
    var value: Int32 = 0
    var size = MemoryLayout<Int32>.size
    if sysctlbyname("hw.optional.arm64", &value, &size, nil, 0) == 0 {
        return value == 1
    }
    return false
}

func freePort() -> Int {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    if fd < 0 { return 7654 }
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    addr.sin_port = 0  // let the OS assign
    let bound = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    if bound != 0 { return 7654 }
    var len = socklen_t(MemoryLayout<sockaddr_in>.size)
    let got = withUnsafeMutablePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            getsockname(fd, $0, &len)
        }
    }
    if got != 0 { return 7654 }
    return Int(UInt16(bigEndian: addr.sin_port))
}

func randomToken() -> String {
    var bytes = [UInt8](repeating: 0, count: 18)
    _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
    return Data(bytes).base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}

// ---- app -------------------------------------------------------------------

class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, NSWindowDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    let port = freePort()
    let token = randomToken()
    var resDir = Bundle.main.resourcePath ?? "."
    var quitting = false

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildWindow()
        startServer()
        waitForServer { [weak self] in self?.loadApp() }
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ note: Notification) {
        quitting = true
        server?.terminate()
    }

    // -- server subprocess --
    func startServer() {
        let arch = isAppleSilicon() ? "arm64" : "x86_64"
        let py = "\(resDir)/python/\(arch)/bin/python3"
        let ffdir = "\(resDir)/bin/\(arch)"

        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = ["\(resDir)/app/server.py", "--app"]
        var env = ProcessInfo.processInfo.environment
        env["TR_APP"] = "1"
        env["TR_WRAPPED"] = "1"
        env["TR_FFMPEG_DIR"] = ffdir
        env["PORT"] = String(port)
        env["ACCESS_TOKEN"] = token
        env["PATH"] = "\(ffdir):\(resDir)/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        p.environment = env
        p.terminationHandler = { [weak self] proc in
            guard let self = self, !self.quitting else { return }
            DispatchQueue.main.async {
                if proc.terminationStatus == 42 {       // update installed → relaunch
                    self.startServer()
                    self.waitForServer { self.loadApp() }
                } else {
                    NSApp.terminate(nil)                 // clean quit or crash
                }
            }
        }
        do { try p.run(); server = p } catch { showFatal("Couldn't start the engine:\n\(error)") }
    }

    // -- window + webview --
    func buildWindow() {
        let cfg = WKWebViewConfiguration()
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 940, height: 920), configuration: cfg)
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")  // let the page's dark bg show

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 940, height: 920),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "TUBE-RIPPER DELUXE 2000"
        window.minSize = NSSize(width: 700, height: 560)
        window.center()
        window.contentView = webView
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ note: Notification) { NSApp.terminate(nil) }

    // -- load / poll --
    func waitForServer(attempt: Int = 0, then done: @escaping () -> Void) {
        let url = URL(string: "http://127.0.0.1:\(port)/api/health?key=\(token)")!
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                DispatchQueue.main.async { done() }
            } else if attempt < 80 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    self.waitForServer(attempt: attempt + 1, then: done)
                }
            } else {
                DispatchQueue.main.async { self.showFatal("The engine didn't start in time.") }
            }
        }.resume()
    }

    func loadApp() {
        let url = URL(string: "http://127.0.0.1:\(port)/?key=\(token)")!
        webView.load(URLRequest(url: url))
    }

    func showFatal(_ msg: String) {
        let a = NSAlert()
        a.messageText = "TUBE-RIPPER couldn't start"
        a.informativeText = msg
        a.alertStyle = .critical
        a.runModal()
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
