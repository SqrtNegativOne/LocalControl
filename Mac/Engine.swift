import AppKit
import ScreenCaptureKit

struct Verdict: Codable, Equatable {
    enum Source: String, Codable { case llm, cache, builtin, appeal, error }
    var allowed: Bool
    var reason: String
    var scope: String = "page"  // "site" | "page"
    var source: Source = .llm
}

struct Decision: Identifiable, Codable {
    enum Kind: String, Codable { case judge, appeal, rule, ruleEnd }
    var id = UUID()
    var ts = Date()
    var kind: Kind
    var label: String
    var allowed: Bool = true
    var reason: String = ""
}

/// Seconds of active (non-idle) time per app and per website, per day.
struct Usage: Codable {
    var day: String
    var apps: [String: Double] = [:]
    var sites: [String: Double] = [:]

    func top(_ d: [String: Double], _ n: Int = 6) -> [(String, Double)] {
        d.sorted { $0.value > $1.value }.prefix(n).map { ($0.key, $0.value) }
    }
}

enum JudgePrompt {
    static let system = """
    You are the enforcement component of a focus tool. The user set a rule for how their computer may be used \
    right now. Given what is currently on their screen, decide whether it is consistent with the rule.

    Guidelines:
    - Judge by content and purpose, not just the site or app name. A search query, PDF, docs page, forum thread, \
    or video is allowed if its subject fits the rule.
    - Follow the spirit of the rule: if it permits a subject, content on that subject in any format (lecture \
    videos, talks, slides, blog posts, tutorials) is allowed unless the rule explicitly restricts the format.
    - Allow incidental infrastructure needed for allowed work: search engines when the query fits (or the search \
    page is empty), login/SSO pages, file pickers, PDF viewers, reference managers, note-taking, cloud storage \
    holding relevant documents.
    - Block activity that clearly falls outside the rule, e.g. social media feeds, entertainment, sports, \
    shopping, gossip, unrelated news, games, unless the rule permits them.
    - If it is genuinely ambiguous, allow it.
    - scope: "site" only if your verdict would hold for essentially every page of this website (or every window \
    of this app), e.g. arxiv.org under a research-only rule; otherwise "page". Sites that host mixed content \
    (YouTube, Reddit, Google, GitHub, Medium, X) are always "page".
    - reason: one short sentence addressed to the user explaining the verdict.
    """
    static let schema = #"""
    {"type":"object","properties":{"reason":{"type":"string"},"allowed":{"type":"boolean"},"scope":{"type":"string","enum":["site","page"]}},"required":["reason","allowed","scope"]}
    """#
    struct Reply: Decodable { var reason: String; var allowed: Bool; var scope: String }
}

@MainActor
final class Engine: ObservableObject {
    static let systemApps: Set<String> = [
        "com.apple.finder", "com.apple.loginwindow", "com.apple.dock", "com.apple.systemuiserver",
        "com.apple.systempreferences", "com.apple.Spotlight", "com.apple.controlcenter",
        "com.apple.notificationcenterui", "com.apple.ScreenSaver.Engine", "com.apple.SecurityAgent",
        "com.apple.UserNotificationCenter", "com.apple.coreautha",
    ]
    static let blankURLPrefixes = [
        "chrome://newtab", "about:blank", "about:newtab", "about:home", "edge://newtab", "brave://newtab",
        "favorites://", "arc://", "chrome-search://",
    ]

    let sync: CloudSync
    @Published var modelName: String { didSet { UserDefaults.standard.set(modelName, forKey: "model") } }
    /// off | auto | always
    @Published var vision: String { didSet { UserDefaults.standard.set(vision, forKey: "vision") } }
    @Published private(set) var current: Activity?
    @Published private(set) var currentVerdict: Verdict?
    @Published private(set) var decisions: [Decision] = []
    @Published private(set) var usage: Usage
    @Published private(set) var modelStatus: (ok: Bool, message: String) = (false, "Checking model…")
    @Published private(set) var lastError: String?

    private var cache: [String: Verdict] = [:]
    private var cacheRuleID: String?
    private var overrides: [String: [String]]  // rule id -> keys allowed by appeal
    private var inFlight: Set<String> = []
    private var notified: [String: Date] = [:]
    private(set) var blocks: [String: [String: String]] = [:]  // block id -> details for the block page
    private var lastTick = Date()
    private var lastSave = Date()
    private var lastRevive = Date.distantPast
    private var lastRuleID: String?
    private var timer: Timer?
    let port: UInt16 = 8765

    static let dataDir: URL = {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("LocalControl")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    var ollama: Ollama { Ollama(model: modelName) }

    init(sync: CloudSync) {
        self.sync = sync
        modelName = UserDefaults.standard.string(forKey: "model") ?? "gemma3:4b"
        vision = UserDefaults.standard.string(forKey: "vision") ?? "auto"
        overrides = (UserDefaults.standard.dictionary(forKey: "overrides") as? [String: [String]]) ?? [:]
        usage = Engine.loadUsage(day: Engine.today())
        lastRuleID = sync.activeRule?.id
    }

    func start() {
        timer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        Task { await refreshModelStatus() }
    }

    // MARK: rules

    func lockIn(command: String, minutes: Double?) async throws {
        if let r = sync.activeRule { throw CloudSync.SetError.locked(r) }
        var ruleText = command
        var parsedMinutes: Double = 0
        do {
            let p = try await ollama.chatJSON(ParsedCommand.self, system: PromptText.parseSystem,
                                              user: PromptText.parseUser(command), schema: PromptText.parseSchema)
            ruleText = p.rule.isEmpty ? command : p.rule
            parsedMinutes = p.duration_minutes
        } catch {
            lastError = error.localizedDescription
        }
        let m = Rule.clampMinutes(minutes ?? (parsedMinutes > 0 ? parsedMinutes : nil))
        let now = Date()
        let rule = Rule(command: command, rule: ruleText, startedAt: now, expiresAt: now.addingTimeInterval(m * 60))
        try sync.setRule(rule)
        log(Decision(kind: .rule, label: rule.rule, reason: "\(Int(m)) min"))
    }

    // MARK: loop

    private func tick() {
        let now = Date()
        let dt = min(now.timeIntervalSince(lastTick), 4.5)
        lastTick = now

        if let id = lastRuleID, sync.activeRule?.id != id {
            log(Decision(kind: .ruleEnd, label: "Rule ended"))
            notify("LocalControl", "Your rule has ended.")
        }
        lastRuleID = sync.activeRule?.id

        guard let act = Probe.observe() else { return }
        if Probe.idleSeconds < 120 { addUsage(act, dt) }
        if now.timeIntervalSince(lastSave) > 30 { saveUsage(); lastSave = now }

        current = act
        guard let rule = sync.activeRule else { currentVerdict = nil; return }
        if cacheRuleID != rule.id { cache = [:]; cacheRuleID = rule.id }

        if let v = immediateVerdict(act, rule: rule) {
            currentVerdict = v
            if !v.allowed { enforce(act, v, rule: rule) }
            return
        }
        currentVerdict = nil
        let key = act.cacheKeys.page
        guard !inFlight.contains(key) else { return }
        inFlight.insert(key)
        Task {
            let v = await judge(act, rule: rule)
            inFlight.remove(key)
            if current == act {
                currentVerdict = v
                if !v.allowed { enforce(act, v, rule: rule) }
            }
        }
    }

    /// Builtin, appeal or cached verdict; nil means the model must be asked.
    private func immediateVerdict(_ act: Activity, rule: Rule) -> Verdict? {
        if Self.systemApps.contains(act.bundleID) || act.bundleID == Bundle.main.bundleIdentifier {
            return Verdict(allowed: true, reason: "System app", source: .builtin)
        }
        if act.browser != nil {
            if act.url.hasPrefix("http://127.0.0.1:\(port)") {
                return Verdict(allowed: true, reason: "LocalControl", source: .builtin)
            }
            if Self.blankURLPrefixes.contains(where: act.url.hasPrefix) || (act.url.isEmpty && act.tabTitle.isEmpty) {
                return Verdict(allowed: true, reason: "Blank tab", source: .builtin)
            }
        }
        let (site, page) = act.cacheKeys
        let allowedByAppeal = overrides[rule.id] ?? []
        if allowedByAppeal.contains(page) || allowedByAppeal.contains(site) {
            return Verdict(allowed: true, reason: "Allowed by your appeal", source: .appeal)
        }
        if var v = cache[page] ?? cache[site] {
            v.source = .cache
            return v
        }
        return nil
    }

    private func judge(_ act: Activity, rule: Rule, justification: String = "") async -> Verdict {
        do {
            let v = try await askModel(act, rule: rule, justification: justification)
            lastError = nil
            if justification.isEmpty, cacheRuleID == rule.id {
                cache[v.scope == "site" ? act.cacheKeys.site : act.cacheKeys.page] = v
            }
            if justification.isEmpty {
                log(Decision(kind: .judge, label: act.label, allowed: v.allowed,
                             reason: v.reason + (v.scope == "site" ? " (whole site)" : "")))
            }
            return v
        } catch {
            // Fail closed, uncached: otherwise stopping Ollama would be a way out of a rule.
            lastError = error.localizedDescription
            reviveOllama()
            return Verdict(allowed: false, reason: "The local model is unavailable, so unjudged activity is blocked until it's back.",
                           source: .error)
        }
    }

    private func askModel(_ act: Activity, rule: Rule, justification: String) async throws -> Verdict {
        var lines = ["RULE: \(rule.rule)", "(Original instruction: \"\(rule.command)\")", "", "CURRENT ACTIVITY",
                     "Application: \(act.app)"]
        if !act.url.isEmpty { lines.append("URL: \(act.url)") }
        if !act.tabTitle.isEmpty { lines.append("Page title: \(act.tabTitle)") }
        if !act.windowTitle.isEmpty, act.windowTitle != act.tabTitle { lines.append("Window title: \(act.windowTitle)") }
        if !justification.isEmpty {
            lines += ["", "The user explains why they need this: \(justification)",
                      "Take the explanation into account if it is plausible and consistent with the rule."]
        }
        var images: [Data] = []
        let wantsShot = vision == "always" ||
            (vision == "auto" && ((act.browser != nil && act.url.isEmpty) || (act.browser == nil && act.windowTitle.isEmpty)))
        if wantsShot, let shot = await Self.screenshot() {
            images.append(shot)
            lines += ["", "A screenshot of the screen is attached."]
        }
        let r = try await ollama.chatJSON(JudgePrompt.Reply.self, system: JudgePrompt.system,
                                          user: lines.joined(separator: "\n"), schema: JudgePrompt.schema, images: images)
        return Verdict(allowed: r.allowed, reason: r.reason, scope: r.scope == "site" ? "site" : "page")
    }

    func appeal(url: String, title: String, justification: String) async -> Verdict {
        guard let rule = sync.activeRule else { return Verdict(allowed: true, reason: "No active rule", source: .builtin) }
        let act = Activity(app: "Browser", bundleID: "com.google.Chrome", url: url, tabTitle: title)
        var v = await judge(act, rule: rule, justification: justification)
        if v.source != .error { v.source = .appeal }
        if v.allowed {
            overrides[rule.id, default: []].append(act.cacheKeys.page)
            overrides = overrides.filter { $0.key == rule.id }  // drop old rules' appeals
            UserDefaults.standard.set(overrides, forKey: "overrides")
        }
        log(Decision(kind: .appeal, label: url, allowed: v.allowed, reason: v.reason))
        return v
    }

    // MARK: enforcement

    private func enforce(_ act: Activity, _ v: Verdict, rule: Rule) {
        if let browser = act.browser, !act.url.isEmpty {
            let id = String(UUID().uuidString.prefix(8))
            blocks[id] = ["url": act.url, "title": act.tabTitle, "reason": v.reason, "rule": rule.rule]
            if blocks.count > 200 { blocks.removeValue(forKey: blocks.keys.first!) }
            let target = "http://127.0.0.1:\(port)/blocked?id=\(id)"
            if !Enforcer.redirect(act, browser: browser, to: target) {
                lastError = Probe.lastScriptError.map { "Could not redirect tab: \($0)" }
            }
        } else {
            Enforcer.hide(bundleID: act.bundleID)
            let key = act.cacheKeys.page
            if Date().timeIntervalSince(notified[key] ?? .distantPast) > 30 {
                notified[key] = Date()
                notify("LocalControl hid \(act.app)", v.reason)
            }
        }
    }

    private func reviveOllama() {
        guard Date().timeIntervalSince(lastRevive) > 30 else { return }
        lastRevive = Date()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/brew")
        p.arguments = ["services", "start", "ollama"]
        try? p.run()
    }

    func reportError(_ message: String) {
        lastError = message
        print(message)
    }

    func notify(_ title: String, _ message: String) {
        _ = Probe.run("display notification \(Probe.quote(String(message.prefix(200)))) with title \(Probe.quote(title))")
    }

    func refreshModelStatus() async {
        let s = await ollama.status()
        modelStatus = (s.0, s.1)
    }

    /// Observes the screen once and judges it against a sample rule, without touching any state.
    func selfTest() async {
        print("model:", await ollama.status())
        print("accessibility:", Probe.accessibilityTrusted)
        guard let act = Probe.observe() else { return print("observe failed") }
        print("activity:", act.label, "| url:", act.url, "| script error:", Probe.lastScriptError ?? "none")
        let rule = Rule(command: "for the next hour, only papers or technical material",
                        rule: "You may only read papers, search for papers, or access technical material.",
                        startedAt: Date(), expiresAt: Date().addingTimeInterval(3600))
        let samples = [act,
                       Activity(app: "Firefox", bundleID: "org.mozilla.firefox", url: "https://arxiv.org/abs/1706.03762",
                                tabTitle: "[1706.03762] Attention Is All You Need"),
                       Activity(app: "Firefox", bundleID: "org.mozilla.firefox", url: "https://x.com/home", tabTitle: "Home / X")]
        for a in samples {
            do {
                let v = try await askModel(a, rule: rule, justification: "")
                print(v.allowed ? "ALLOW" : "BLOCK", v.scope, "|", a.label, "|", v.reason)
            } catch {
                print("judge error:", error.localizedDescription)
            }
        }
    }

    // MARK: screenshots

    private static func screenshot() async -> Data? {
        guard let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true),
              let display = content.displays.first else { return nil }
        let config = SCStreamConfiguration()
        let scale = min(1.0, 1280.0 / Double(display.width))
        config.width = Int(Double(display.width) * scale)
        config.height = Int(Double(display.height) * scale)
        let filter = SCContentFilter(display: display, excludingWindows: [])
        guard let image = try? await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
        else { return nil }
        return NSBitmapImageRep(cgImage: image).representation(using: .jpeg, properties: [.compressionFactor: 0.7])
    }

    // MARK: usage & log

    private static func today() -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date())
    }

    private static func usageURL(_ day: String) -> URL {
        dataDir.appendingPathComponent("usage-\(day).json")
    }

    private static func loadUsage(day: String) -> Usage {
        (try? Data(contentsOf: usageURL(day))).flatMap { try? JSONDecoder().decode(Usage.self, from: $0) } ?? Usage(day: day)
    }

    private func addUsage(_ act: Activity, _ dt: TimeInterval) {
        let day = Self.today()
        if day != usage.day { saveUsage(); usage = Usage(day: day) }
        usage.apps[act.app, default: 0] += dt
        if act.browser != nil, !act.domain.isEmpty { usage.sites[act.domain, default: 0] += dt }
    }

    func saveUsage() {
        try? JSONEncoder().encode(usage).write(to: Self.usageURL(usage.day))
    }

    private func log(_ d: Decision) {
        decisions.insert(d, at: 0)
        if decisions.count > 60 { decisions.removeLast() }
        guard var line = try? JSONEncoder().encode(d) else { return }
        line.append(0x0A)
        let url = Self.dataDir.appendingPathComponent("events.jsonl")
        if let h = try? FileHandle(forWritingTo: url) {
            h.seekToEndOfFile()
            h.write(line)
            try? h.close()
        } else {
            try? line.write(to: url)
        }
    }
}
