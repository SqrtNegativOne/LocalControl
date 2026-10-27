import Foundation

/// Model prompts shared by both platforms.
enum PromptText {
    static let parseSystem = """
    You turn a user's instruction about how their device may be used into a structured policy.
    - duration_minutes: how long the policy lasts, in minutes. Convert phrases like "for the next hour" (60), \
    "for 90 minutes", or "until 5pm" (minutes from now to then). Use 0 if no duration or end time is given.
    - rule: restate the instruction as a clear, self-contained policy describing what activity IS allowed and, \
    if implied, what is NOT. Write it in second person ("You may only ..."). Do not mention the duration.
    """

    static let parseSchema = #"""
    {"type":"object","properties":{"duration_minutes":{"type":"number"},"rule":{"type":"string"}},"required":["duration_minutes","rule"]}
    """#

    static func parseUser(_ command: String, now: Date = Date()) -> String {
        let f = DateFormatter()
        f.dateFormat = "EEEE HH:mm"
        return "Current time: \(f.string(from: now)).\nInstruction: \(command)"
    }
}

/// What the model returns when parsing a command.
struct ParsedCommand: Decodable {
    var duration_minutes: Double
    var rule: String
}
