import Foundation

/// Keeps the active rule and prompt history in local defaults and iCloud key-value storage,
/// so a rule set on one device is locked in on the others too.
@MainActor
final class CloudSync: ObservableObject {
    enum SetError: LocalizedError {
        case locked(Rule)
        var errorDescription: String? {
            if case .locked(let r) = self {
                return "A rule is locked in until \(r.expiresAt.formatted(date: .omitted, time: .shortened)): \(r.rule)"
            }
            return nil
        }
    }

    @Published private(set) var rule: Rule?
    @Published private(set) var history = PromptHistory()

    private let cloud = NSUbiquitousKeyValueStore.default
    let local: UserDefaults

    init() {
        #if os(iOS)
        local = UserDefaults(suiteName: LC.appGroup) ?? .standard
        #else
        local = .standard
        #endif
        rule = Self.decode(Rule.self, local.data(forKey: LC.ruleKey))
        history = Self.decode(PromptHistory.self, local.data(forKey: LC.historyKey)) ?? PromptHistory()
        NotificationCenter.default.addObserver(
            forName: NSUbiquitousKeyValueStore.didChangeExternallyNotification, object: cloud, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.pull() }
        }
        cloud.synchronize()
        pull()
        expireIfNeeded()
    }

    /// The active rule, if any (clears an expired one).
    var activeRule: Rule? {
        expireIfNeeded()
        return rule
    }

    func setRule(_ new: Rule) throws {
        if let r = activeRule { throw SetError.locked(r) }
        rule = new
        history.record(new.command, minutes: new.expiresAt.timeIntervalSince(new.startedAt) / 60, at: new.startedAt)
        push()
    }

    func forgetPrompt(_ text: String) {
        history.forget(text)
        push()
    }

    @discardableResult
    func expireIfNeeded() -> Bool {
        guard let r = rule, !r.isActive else { return false }
        rule = nil
        push()
        return true
    }

    // MARK: storage

    private func push() {
        let ruleData = rule.flatMap { try? JSONEncoder().encode($0) }
        let historyData = try? JSONEncoder().encode(history)
        local.set(ruleData, forKey: LC.ruleKey)
        local.set(historyData, forKey: LC.historyKey)
        cloud.set(ruleData, forKey: LC.ruleKey)
        cloud.set(historyData, forKey: LC.historyKey)
        cloud.synchronize()
    }

    private func pull() {
        var changed = false
        if let remote = Self.decode(PromptHistory.self, cloud.data(forKey: LC.historyKey)) {
            let merged = history.merged(with: remote)
            if merged != history { history = merged; changed = true }
        }
        // Adopt a remote rule unless we already have a different active one (first come wins).
        if let remote = Self.decode(Rule.self, cloud.data(forKey: LC.ruleKey)), remote.isActive,
           rule?.isActive != true || rule?.id == remote.id, remote != rule {
            rule = remote
            changed = true
        }
        if changed { push() }
    }

    private static func decode<T: Decodable>(_ type: T.Type, _ data: Data?) -> T? {
        data.flatMap { try? JSONDecoder().decode(T.self, from: $0) }
    }
}
