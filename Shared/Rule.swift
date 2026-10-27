import Foundation

/// Identifiers shared by the Mac app, the iPhone app and its Screen Time extension.
enum LC {
    static let appGroup = "group.com.eugenevinitsky.localcontrol"
    /// Keys in iCloud key-value storage (mirrored into local defaults).
    static let ruleKey = "rule"
    static let historyKey = "prompts"

    static let defaultMinutes: Double = 60
    static let maxMinutes: Double = 12 * 60

    static var deviceName: String {
        #if os(macOS)
        return Host.current().localizedName ?? "Mac"
        #else
        return "iPhone"
        #endif
    }
}

/// A usage rule. Once set it is locked in: it can't be replaced or ended before `expiresAt`.
struct Rule: Codable, Equatable, Identifiable {
    var id: String = UUID().uuidString
    /// What the user typed.
    var command: String
    /// The model's restatement, used for judging.
    var rule: String
    var startedAt: Date
    var expiresAt: Date
    /// Device the rule was set on.
    var origin: String = LC.deviceName

    var isActive: Bool { expiresAt > Date() }
    var remaining: TimeInterval { max(0, expiresAt.timeIntervalSinceNow) }

    static func clampMinutes(_ m: Double?) -> Double {
        guard let m, m > 0 else { return LC.defaultMinutes }
        return min(m, LC.maxMinutes)
    }
}

/// A prompt the user has used before.
struct SavedPrompt: Codable, Equatable, Identifiable {
    var text: String
    var count: Int
    var lastUsed: Date
    var minutes: Double

    var id: String { PromptHistory.key(text) }
}

/// Prompt history, most used first. Synced across devices; forgetting leaves a tombstone
/// so another device's stale copy doesn't bring the prompt back.
struct PromptHistory: Codable, Equatable {
    var items: [SavedPrompt] = []
    var forgotten: [String: Date] = [:]

    static func key(_ text: String) -> String {
        text.lowercased().split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    mutating func record(_ text: String, minutes: Double, at date: Date = Date()) {
        let k = Self.key(text)
        forgotten[k] = nil
        if let i = items.firstIndex(where: { $0.id == k }) {
            items[i].count += 1
            items[i].lastUsed = date
            items[i].minutes = minutes
        } else {
            items.append(SavedPrompt(text: text.trimmingCharacters(in: .whitespacesAndNewlines),
                                     count: 1, lastUsed: date, minutes: minutes))
        }
    }

    mutating func forget(_ text: String) {
        let k = Self.key(text)
        items.removeAll { $0.id == k }
        forgotten[k] = Date()
    }

    func top(_ n: Int = 8) -> [SavedPrompt] {
        Array(items.sorted { ($0.count, $0.lastUsed) > ($1.count, $1.lastUsed) }.prefix(n))
    }

    /// Union of two histories: per prompt, the higher count and latest use win.
    func merged(with other: PromptHistory) -> PromptHistory {
        var out = PromptHistory()
        out.forgotten = forgotten.merging(other.forgotten) { max($0, $1) }
        var byKey: [String: SavedPrompt] = [:]
        for p in items + other.items {
            if let cur = byKey[p.id] {
                byKey[p.id] = SavedPrompt(text: cur.lastUsed >= p.lastUsed ? cur.text : p.text,
                                          count: max(cur.count, p.count),
                                          lastUsed: max(cur.lastUsed, p.lastUsed),
                                          minutes: cur.lastUsed >= p.lastUsed ? cur.minutes : p.minutes)
            } else {
                byKey[p.id] = p
            }
        }
        out.items = byKey.values.filter { p in (out.forgotten[p.id].map { p.lastUsed > $0 }) ?? true }
        return out
    }
}
