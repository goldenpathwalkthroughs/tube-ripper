// TUBE-RIPPER DELUXE 2000 — native launcher.
//
// A real universal Mach-O app so Finder always launches it natively (no
// Rosetta, no "won't be supported" prompt, no instant-quit on Apple Silicon
// machines without Rosetta — the bug a shell-script launcher caused).
//
// It picks the correct-arch bundled Python, starts the local server, and the
// server opens the UI in the user's default browser. We stay alive (no window,
// agent app) to manage the server: a self-update makes the server exit 42, and
// we relaunch it; a clean quit (the in-page QUIT button) exits the app.

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
    let resDir = Bundle.main.resourcePath ?? "."

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)   // background agent, no Dock icon
        startServer()
    }

    func startServer() {
        let arch = isAppleSilicon() ? "arm64" : "x86_64"
        let py = "\(resDir)/python/\(arch)/bin/python3"
        let ffdir = "\(resDir)/bin/\(arch)"

        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = ["\(resDir)/app/server.py", "--app"]
        var env = ProcessInfo.processInfo.environment
        env["TR_APP"] = "1"                       // app mode: server opens the browser
        env["TR_FFMPEG_DIR"] = ffdir
        env["PATH"] = "\(ffdir):\(resDir)/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        p.environment = env
        p.terminationHandler = { [weak self] proc in
            guard let self = self, !self.quitting else { return }
            DispatchQueue.main.async {
                if proc.terminationStatus == 42 {   // update installed → relaunch engine
                    self.startServer()
                } else {                            // clean quit / crash → exit app
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

    func applicationWillTerminate(_ note: Notification) {
        quitting = true
        server?.terminate()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
