// TUBE-RIPPER DELUXE 2000 — native launcher (menu-bar agent).
//
// A real universal Mach-O app so Finder always launches it natively (no
// Rosetta, no instant-quit on Apple Silicon). It runs the bundled server and
// opens the UI in the user's default browser.
//
// It lives in the MENU BAR (no Dock icon) so it's always discoverable while
// running: click the icon for Open / Quit. Clicking the app again, or closing
// the browser tab and re-opening the app, re-opens the page (handled via
// applicationShouldHandleReopen) — fixing the "nothing happens" dead-end.

import Cocoa

func isAppleSilicon() -> Bool {
    var value: Int32 = 0
    var size = MemoryLayout<Int32>.size
    if sysctlbyname("hw.optional.arm64", &value, &size, nil, 0) == 0 {
        return value == 1
    }
    return false
}

class AppDelegate: NSObject, NSApplicationDelegate {
    var server: Process?
    var quitting = false
    var statusItem: NSStatusItem!
    let resDir = Bundle.main.resourcePath ?? "."
    let port = 1337

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)   // menu-bar agent, no Dock icon
        setupMenuBar()
        startServer()
        waitForServer { self.openBrowser() }
    }

    // Clicking the app (or its icon) while it's already running lands here —
    // re-open the page instead of doing nothing.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        openBrowser()
        return true
    }

    func applicationWillTerminate(_ note: Notification) {
        quitting = true
        server?.terminate()
    }

    // -- menu bar --
    func setupMenuBar() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let btn = statusItem.button {
            let img = NSImage(systemSymbolName: "play.rectangle.fill",
                            accessibilityDescription: "TUBE-RIPPER")
            img?.isTemplate = true
            btn.image = img
            btn.toolTip = "TUBE-RIPPER DELUXE 2000"
        }
        let menu = NSMenu()
        let openItem = NSMenuItem(title: "Open TUBE-RIPPER",
                                action: #selector(openClicked), keyEquivalent: "o")
        openItem.target = self
        menu.addItem(openItem)
        menu.addItem(.separator())
        let info = NSMenuItem(title: "Running at localhost:\(port)", action: nil, keyEquivalent: "")
        info.isEnabled = false
        menu.addItem(info)
        menu.addItem(.separator())
        let quitItem = NSMenuItem(title: "Quit TUBE-RIPPER",
                                action: #selector(quitClicked), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        statusItem.menu = menu
    }

    @objc func openClicked() { openBrowser() }
    @objc func quitClicked() { NSApp.terminate(nil) }

    func openBrowser() {
        if let url = URL(string: "http://localhost:\(port)/") {
            NSWorkspace.shared.open(url)
        }
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
        env["TR_NO_OPEN"] = "1"          // the launcher opens the browser, not the server
        env["TR_FFMPEG_DIR"] = ffdir
        env["PORT"] = String(port)
        env["PATH"] = "\(ffdir):\(resDir)/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        p.environment = env
        p.terminationHandler = { [weak self] proc in
            guard let self = self, !self.quitting else { return }
            DispatchQueue.main.async {
                if proc.terminationStatus == 42 {   // update installed → relaunch engine
                    self.startServer()
                } else {                            // engine quit/crashed → exit app
                    NSApp.terminate(nil)
                }
            }
        }
        do { try p.run(); server = p }
        catch {
            let a = NSAlert()
            a.messageText = "TUBE-RIPPER couldn't start"
            a.informativeText = "Failed to launch the engine:\n\(error)"
            a.alertStyle = .critical
            a.runModal()
            NSApp.terminate(nil)
        }
    }

    func waitForServer(attempt: Int = 0, then done: @escaping () -> Void) {
        let url = URL(string: "http://127.0.0.1:\(port)/api/health")!  // loopback: no login
        var req = URLRequest(url: url); req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            if let h = resp as? HTTPURLResponse, h.statusCode == 200 {
                DispatchQueue.main.async { done() }
            } else if attempt < 80 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    self.waitForServer(attempt: attempt + 1, then: done)
                }
            }
        }.resume()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
