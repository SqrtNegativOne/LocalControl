import AppKit

@MainActor
enum Enforcer {
    /// Sends the browser's current tab to `target` if it's still showing act.url.
    static func redirect(_ act: Activity, browser: Probe.Browser, to target: String) -> Bool {
        if browser == .firefox {
            // No tab API: drive the address bar (needs Accessibility).
            return Probe.run("""
            tell application \(Probe.quote(act.app)) to activate
            tell application "System Events"
                keystroke "l" using command down
                delay 0.1
                keystroke \(Probe.quote(target))
                key code 36
            end tell
            return "yes"
            """) == "yes"
        }
        return Probe.run("""
        tell application \(Probe.quote(act.app))
            if (count of windows) = 0 then return "no"
            set t to \(Probe.tabExpression(browser))
            if (URL of t) is \(Probe.quote(act.url)) then
                set URL of t to \(Probe.quote(target))
                return "yes"
            end if
            return "no"
        end tell
        """) == "yes"
    }

    static func hide(bundleID: String) {
        NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).forEach { $0.hide() }
    }
}
