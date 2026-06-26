import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var serverProcess: Process?

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureMenu()

        let config = WKWebViewConfiguration()
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        config.preferences.javaScriptCanOpenWindowsAutomatically = true

        webView = WKWebView(frame: .zero, configuration: config)
        webView.uiDelegate = self
        webView.navigationDelegate = self

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1440, height: 940),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "LungPrometheus"
        window.center()
        window.minSize = NSSize(width: 1180, height: 760)
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)

        if let serverURL = startPythonServer() {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
                self?.webView.load(URLRequest(url: serverURL))
            }
        } else if let indexURL = Self.locateIndexHTML() {
            webView.loadFileURL(indexURL, allowingReadAccessTo: indexURL.deletingLastPathComponent())
        } else {
            webView.loadHTMLString("<h1>Не найден public/index.html</h1>", baseURL: nil)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        serverProcess?.terminate()
    }

    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @MainActor @Sendable ([URL]?) -> Void
    ) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.allowedContentTypes = []
        panel.prompt = "Загрузить"

        panel.begin { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    private func configureMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)

        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "Quit LungPrometheus",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        appMenuItem.submenu = appMenu
        NSApplication.shared.mainMenu = mainMenu
    }

    private func startPythonServer() -> URL? {
        guard let scriptURL = Self.locateBackendServer(),
              let publicURL = Self.locatePublicDirectory(),
              let pythonPath = Self.locatePython()
        else {
            return nil
        }

        let port = "8765"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [
            scriptURL.path,
            "--host", "127.0.0.1",
            "--port", port,
            "--public", publicURL.path
        ]
        process.standardOutput = Pipe()
        process.standardError = Pipe()

        do {
            try process.run()
            serverProcess = process
            return URL(string: "http://127.0.0.1:\(port)/")
        } catch {
            return nil
        }
    }

    private static func locateIndexHTML() -> URL? {
        let fileManager = FileManager.default
        var candidates: [URL] = []

        if let resourceURL = Bundle.main.resourceURL {
            candidates.append(resourceURL.appendingPathComponent("web/index.html"))
            candidates.append(resourceURL.appendingPathComponent("public/index.html"))
        }

        candidates.append(URL(fileURLWithPath: fileManager.currentDirectoryPath)
            .appendingPathComponent("public/index.html"))

        let executableDir = Bundle.main.executableURL?.deletingLastPathComponent()
        if let executableDir {
            candidates.append(executableDir
                .deletingLastPathComponent()
                .appendingPathComponent("Resources/web/index.html"))
        }

        return candidates.first { fileManager.fileExists(atPath: $0.path) }
    }

    private static func locatePublicDirectory() -> URL? {
        let fileManager = FileManager.default
        var candidates: [URL] = []

        if let resourceURL = Bundle.main.resourceURL {
            candidates.append(resourceURL.appendingPathComponent("web"))
            candidates.append(resourceURL.appendingPathComponent("public"))
        }

        candidates.append(URL(fileURLWithPath: fileManager.currentDirectoryPath)
            .appendingPathComponent("public"))

        return candidates.first { isDirectory($0) }
    }

    private static func locateBackendServer() -> URL? {
        let fileManager = FileManager.default
        var candidates: [URL] = []

        if let resourceURL = Bundle.main.resourceURL {
            candidates.append(resourceURL.appendingPathComponent("backend/server.py"))
        }

        candidates.append(URL(fileURLWithPath: fileManager.currentDirectoryPath)
            .appendingPathComponent("backend/server.py"))

        return candidates.first { fileManager.fileExists(atPath: $0.path) }
    }

    private static func locatePython() -> String? {
        for path in [
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3"
        ] {
            if FileManager.default.fileExists(atPath: path) {
                return path
            }
        }
        return nil
    }

    private static func isDirectory(_ url: URL) -> Bool {
        var isDirectory: ObjCBool = false
        let exists = FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory)
        return exists && isDirectory.boolValue
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)
app.run()
