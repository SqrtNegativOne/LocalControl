// Prints the URL of the page shown in the focused window of the app with the given pid,
// read from the Accessibility tree. Used for browsers without an AppleScript tab API (Firefox).
// Usage: axurl <pid>
import AppKit
import ApplicationServices

guard CommandLine.arguments.count > 1, let pid = Int32(CommandLine.arguments[1]) else { exit(2) }

func attr(_ e: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(e, name as CFString, &v) == .success ? v : nil
}

let app = AXUIElementCreateApplication(pid)
guard let w = attr(app, kAXFocusedWindowAttribute) else { exit(1) }

// Breadth-first so we hit the top-level web area (the page) before any nested iframes.
var queue: [AXUIElement] = [w as! AXUIElement]
var seen = 0
while !queue.isEmpty && seen < 4000 {
    let e = queue.removeFirst()
    seen += 1
    if (attr(e, kAXRoleAttribute) as? String) == "AXWebArea" {
        if let url = attr(e, "AXURL") {
            print((url as? URL)?.absoluteString ?? "\(url)")
            exit(0)
        }
        continue
    }
    if let kids = attr(e, kAXChildrenAttribute) as? [AXUIElement] { queue.append(contentsOf: kids) }
}
exit(1)
