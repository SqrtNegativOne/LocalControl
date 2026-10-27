import Foundation

/// Notices ways of weakening LocalControl during a rule and reports each to the accountability partner.
@MainActor
final class Watchdog {
    private let sync: CloudSync
    private let filter: FilterClient
    private let accountability: Accountability
    private let onReport: (String) -> Void
    private var reported: Set<String> = []
    private var ruleID: String?
    private var lastWall = Date()
    private var lastMono = Watchdog.monotonic()
    private let marker = Engine.dataDir.appendingPathComponent("running")

    init(sync: CloudSync, filter: FilterClient, accountability: Accountability, onReport: @escaping (String) -> Void) {
        self.sync = sync
        self.filter = filter
        self.accountability = accountability
        self.onReport = onReport
    }

    /// Call at launch: a leftover marker means the last run didn't quit normally.
    func launched() {
        if FileManager.default.fileExists(atPath: marker.path), sync.activeRule != nil {
            report("LocalControl stopped unexpectedly during a rule (force-quit, crash or restart) and was relaunched.")
        }
        try? Data().write(to: marker)
    }

    func quitting() {
        try? FileManager.default.removeItem(at: marker)
    }

    func check() {
        let (wall, mono) = (Date(), Self.monotonic())
        let drift = wall.timeIntervalSince(lastWall) - (mono - lastMono)
        (lastWall, lastMono) = (wall, mono)
        // A rule that was active at the last check may have just been ended by moving the clock forward.
        if ruleID != nil, abs(drift) > 120 {
            report("The Mac's clock was changed by \(Int(drift / 60)) minutes during a rule.")
        }
        guard let rule = sync.activeRule else { ruleID = nil; return }
        if rule.id != ruleID { ruleID = rule.id; reported = [] }

        condition("accessibility", !Probe.accessibilityTrusted,
                  "Accessibility permission was turned off, so LocalControl can't see window titles or Firefox URLs.")
        condition("login", AppDelegate.agentStatus != .enabled,
                  "LocalControl was removed from Login Items, so it won't restart if it's quit.")
        condition("filter", filter.status == .off,
                  "The network filter was turned off.")
        condition("filter-dead", filter.status == .on && Date().timeIntervalSince(filter.lastReply) > 90,
                  "The network filter stopped responding.")
    }

    /// Reports once when `bad` becomes true, again only after it has cleared.
    private func condition(_ key: String, _ bad: Bool, _ message: String) {
        if bad, !reported.contains(key) {
            reported.insert(key)
            report(message)
        } else if !bad {
            reported.remove(key)
        }
    }

    func report(_ message: String) {
        accountability.report(message)
        onReport(message)
    }

    /// Seconds on a clock that keeps counting during sleep and can't be set by the user.
    private static func monotonic() -> Double {
        Double(clock_gettime_nsec_np(CLOCK_MONOTONIC)) / 1e9
    }
}
