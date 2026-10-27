import DeviceActivity
import Foundation
import ManagedSettings

/// Lifts the shields when a rule's interval ends, even if the app isn't running.
final class Monitor: DeviceActivityMonitor {
    override func intervalDidEnd(for activity: DeviceActivityName) {
        super.intervalDidEnd(for: activity)
        let defaults = UserDefaults(suiteName: LC.appGroup)
        // A newer rule may have been set since this interval was scheduled; leave it alone.
        if let data = defaults?.data(forKey: LC.ruleKey),
           let rule = try? JSONDecoder().decode(Rule.self, from: data),
           rule.expiresAt.timeIntervalSinceNow > 60 {
            return
        }
        ManagedSettingsStore(named: .init("localcontrol")).clearAllSettings()
        defaults?.removeObject(forKey: "appliedPlan")
    }
}
