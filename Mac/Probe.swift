import AppKit

/// What's in front of the user right now.
struct Activity: Equatable, Codable {
    var app: String
    var bundleID: String = ""
    var windowTitle: String = ""
    var url: String = ""
    var tabTitle: String = ""

    var browser: Probe.Browser? { Probe.browser(for: bundleID) }

    var domain: String {
        let host = URLComponents(string: url)?.host ?? ""
        return host.hasPrefix("www.") ? String(host.dropFirst(4)) : host
    }

    /// URL without fragment, used as a cache key.
    var page: String {
        guard var c = URLComponents(string: url) else { return url }
        c.fragment = nil
        return c.string ?? url
    }

    var label: String {
        if browser != nil, !url.isEmpty { return "\(domain) — \(tabTitle.isEmpty ? url : tabTitle)" }
        return windowTitle.isEmpty ? app : "\(app) — \(windowTitle)"
    }

    /// (site-level key, page-level key)
    var cacheKeys: (site: String, page: String) {
        if browser != nil, !url.isEmpty { return ("site:\(domain)", "page:\(page)") }
        return ("app:\(app)", "win:\(app)|\(windowTitle)")
    }
}

@MainActor
enum Probe {
    enum Browser { case chromium, safari, firefox }

    nonisolated static func browser(for bundleID: String) -> Browser? {
        if chromium.contains(bundleID) { return .chromium }
        if safari.contains(bundleID) { return .safari }
        if firefox.contains(bundleID) { return .firefox }
        return nil
    }

    nonisolated static let chromium: Set<String> = [
        "com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.canary", "com.brave.Browser",
        "com.microsoft.edgemac", "company.thebrowser.Browser", "com.vivaldi.Vivaldi", "org.chromium.Chromium",
        "com.operasoftware.Opera",
    ]
    nonisolated static let safari: Set<String> = ["com.apple.Safari", "com.apple.SafariTechnologyPreview"]
    nonisolated static let firefox: Set<String> = [
        "org.mozilla.firefox", "org.mozilla.firefoxdeveloperedition", "org.mozilla.nightly",
        "app.zen-browser.zen", "net.waterfox.waterfox", "io.gitlab.librewolf-community",
    ]

    static var accessibilityTrusted: Bool { AXIsProcessTrusted() }

    static func requestAccessibility() {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)
    }

    /// Set when a browser refused Apple Events (Automation permission denied).
    static var lastScriptError: String?

    static func observe() -> Activity? {
        guard let front = NSWorkspace.shared.frontmostApplication else { return nil }
        let bundleID = front.bundleIdentifier ?? ""
        var act = Activity(app: front.localizedName ?? bundleID, bundleID: bundleID)
        let ax = AXUIElementCreateApplication(front.processIdentifier)
        AXUIElementSetMessagingTimeout(ax, 0.25)
        let window: AXUIElement? = axAttr(ax, kAXFocusedWindowAttribute)
        if let window { act.windowTitle = axAttr(window, kAXTitleAttribute) ?? "" }

        switch act.browser {
        case .chromium, .safari:
            if let (url, title) = activeTab(app: act.app, browser: act.browser!) {
                act.url = url
                act.tabTitle = title
            }
        case .firefox:
            act.tabTitle = act.windowTitle.replacingOccurrences(
                of: #"\s+[—-]\s+Mozilla Firefox.*$"#, with: "", options: .regularExpression)
            if let window { act.url = webAreaURL(window) ?? "" }
        case nil:
            break
        }
        return act
    }

    // MARK: AppleScript (Chromium, Safari)

    private static var scripts: [String: NSAppleScript] = [:]

    static func run(_ source: String) -> String? {
        let script = scripts[source] ?? NSAppleScript(source: source)
        scripts[source] = script
        var err: NSDictionary?
        let out = script?.executeAndReturnError(&err)
        if let err {
            lastScriptError = (err[NSAppleScript.errorMessage] as? String) ?? "\(err)"
            return nil
        }
        lastScriptError = nil
        return out?.stringValue
    }

    static func quote(_ s: String) -> String {
        "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }

    static func tabExpression(_ browser: Browser) -> String {
        browser == .safari ? "current tab of front window" : "active tab of front window"
    }

    private static func activeTab(app: String, browser: Browser) -> (String, String)? {
        let titleProp = browser == .safari ? "name" : "title"
        let out = run("""
        tell application \(quote(app))
            if (count of windows) = 0 then return ""
            try
                set t to \(tabExpression(browser))
                return (URL of t) & linefeed & (\(titleProp) of t)
            on error
                return ""
            end try
        end tell
        """)
        guard let out, !out.isEmpty else { return nil }
        let parts = out.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
        return (String(parts[0]), parts.count > 1 ? String(parts[1]) : "")
    }

    // MARK: Accessibility (Firefox and friends)

    /// The URL of the top-level web area in the window, found breadth-first.
    private static func webAreaURL(_ window: AXUIElement) -> String? {
        var queue = [window]
        var visited = 0
        while !queue.isEmpty, visited < 4000 {
            let el = queue.removeFirst()
            visited += 1
            if (axAttr(el, kAXRoleAttribute) as String?) == "AXWebArea" {
                if let url: URL = axAttr(el, "AXURL") { return url.absoluteString }
                if let s: String = axAttr(el, "AXURL") { return s }
                continue
            }
            if let kids: [AXUIElement] = axAttr(el, kAXChildrenAttribute) { queue.append(contentsOf: kids) }
        }
        return nil
    }

    private static func axAttr<T>(_ el: AXUIElement, _ name: String) -> T? {
        var v: AnyObject?
        guard AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success else { return nil }
        return v as? T
    }

    // MARK: Idle

    static var idleSeconds: TimeInterval {
        CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: ~0)!)
    }
}
