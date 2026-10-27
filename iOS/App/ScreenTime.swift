import DeviceActivity
import FamilyControls
import Foundation
import ManagedSettings

/// The concrete Screen Time settings enforcing one rule.
struct Plan: Codable, Equatable {
    enum Mode: String, Codable, CaseIterable {
        case allowlist, blocklist
    }

    var ruleID: String
    var mode: Mode
    var domains: [String]

    static let key = "appliedPlan"  // also cleared by the Monitor extension
}

extension DeviceActivityName {
    static let localControl = Self("localcontrol.rule")
}

extension ManagedSettingsStore.Name {
    static let localControl = Self("localcontrol")
}

/// Applies plans through ManagedSettings and schedules their removal when the rule ends.
@MainActor
final class ScreenTime: ObservableObject {
    @Published private(set) var authorized = AuthorizationCenter.shared.authorizationStatus == .approved
    @Published private(set) var applied: Plan?
    @Published var alwaysAllowed: FamilyActivitySelection { didSet { save(alwaysAllowed, "alwaysAllowed") } }
    @Published var distracting: FamilyActivitySelection { didSet { save(distracting, "distracting") } }

    private let store = ManagedSettingsStore(named: .localControl)
    private let defaults = UserDefaults(suiteName: LC.appGroup) ?? .standard

    init() {
        alwaysAllowed = Self.load(defaults, "alwaysAllowed") ?? FamilyActivitySelection()
        distracting = Self.load(defaults, "distracting") ?? FamilyActivitySelection()
        applied = Self.load(defaults, Plan.key)
    }

    func requestAuthorization() async {
        try? await AuthorizationCenter.shared.requestAuthorization(for: .individual)
        authorized = AuthorizationCenter.shared.authorizationStatus == .approved
    }

    func apply(_ plan: Plan, until rule: Rule) throws {
        store.clearAllSettings()
        let domains = Set(plan.domains.map { WebDomain(domain: $0) })
        switch plan.mode {
        case .allowlist:
            store.shield.applicationCategories = .all(except: alwaysAllowed.applicationTokens)
            store.webContent.blockedByFilter = .all(except: domains)
        case .blocklist:
            store.shield.applications = distracting.applicationTokens.isEmpty ? nil : distracting.applicationTokens
            store.shield.applicationCategories = .specific(distracting.categoryTokens)
            store.shield.webDomains = distracting.webDomainTokens.isEmpty ? nil : distracting.webDomainTokens
            store.shield.webDomainCategories = .specific(distracting.categoryTokens)
            store.webContent.blockedByFilter = .specific(domains)
        }
        // Deleting the app would lift the shields, so don't allow it while a rule runs.
        store.application.denyAppRemoval = true
        try scheduleEnd(rule.expiresAt)
        applied = plan
        save(plan, Plan.key)
    }

    func clear() {
        store.clearAllSettings()
        DeviceActivityCenter().stopMonitoring([.localControl])
        applied = nil
        defaults.removeObject(forKey: Plan.key)
    }

    /// The Monitor extension clears the store when the interval ends; this covers the app's side.
    func refresh(activeRule: Rule?) {
        applied = Self.load(defaults, Plan.key)
        if activeRule == nil, applied != nil { clear() }
    }

    private func scheduleEnd(_ end: Date) throws {
        // DeviceActivity needs an interval of at least 15 minutes, so start it in the past if needed.
        let start = min(Date(), end.addingTimeInterval(-16 * 60))
        let parts: Set<Calendar.Component> = [.year, .month, .day, .hour, .minute, .second]
        let cal = Calendar.current
        let schedule = DeviceActivitySchedule(
            intervalStart: cal.dateComponents(parts, from: start),
            intervalEnd: cal.dateComponents(parts, from: end),
            repeats: false)
        let center = DeviceActivityCenter()
        center.stopMonitoring([.localControl])
        try center.startMonitoring(.localControl, during: schedule)
    }

    private func save<T: Encodable>(_ value: T, _ key: String) {
        defaults.set(try? PropertyListEncoder().encode(value), forKey: key)
    }

    private static func load<T: Decodable>(_ defaults: UserDefaults, _ key: String) -> T? {
        defaults.data(forKey: key).flatMap { try? PropertyListDecoder().decode(T.self, from: $0) }
    }
}
