import AppKit

/// Texts an accountability partner over iMessage (from your own Messages account) when LocalControl is
/// stopped or tampered with during a rule.
@MainActor
final class Accountability: ObservableObject {
    /// Phone number or email address the partner uses for iMessage.
    @Published private(set) var partner: String?
    @Published private(set) var lastError: String?
    /// The text sent on an alert; "{event}" is replaced by what happened.
    @Published var message: String { didSet { UserDefaults.standard.set(message, forKey: "partnerMessage") } }
    static let defaultMessage = "LocalControl: {event}"

    var isConfigured: Bool { partner != nil }

    init() {
        partner = UserDefaults.standard.string(forKey: "partner")
        message = UserDefaults.standard.string(forKey: "partnerMessage") ?? Self.defaultMessage
    }

    /// Saves the partner and texts them an introduction (macOS asks to let LocalControl control Messages).
    func connect(partner handle: String) throws {
        try send("Hi! I've made you my accountability partner in LocalControl, a focus app. If I stop it or work "
                 + "around it while a focus rule is locked in, it will text you from my account.", to: handle)
        partner = handle
        UserDefaults.standard.set(handle, forKey: "partner")
        lastError = nil
    }

    func disconnect() {
        partner = nil
        UserDefaults.standard.removeObject(forKey: "partner")
    }

    /// Something weakened the protection during a rule: tell the partner now.
    func report(_ event: String) {
        guard let partner else { return }
        do {
            let template = message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Self.defaultMessage : message
            try send(template.replacingOccurrences(of: "{event}", with: event), to: partner)
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    func sendTest() {
        report("This is a test alert, nothing is wrong.")
    }

    private func send(_ text: String, to handle: String) throws {
        let script = """
        tell application "Messages"
            set svc to 1st account whose service type = iMessage
            send \(Probe.quote(text)) to participant \(Probe.quote(handle)) of svc
        end tell
        """
        _ = Probe.run(script)
        if let err = Probe.lastScriptError { throw LLMError(message: "Couldn't send an iMessage: \(err)") }
    }
}
