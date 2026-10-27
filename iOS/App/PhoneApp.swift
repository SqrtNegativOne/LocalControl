import FamilyControls
import SwiftUI

@main
struct PhoneApp: App {
    @StateObject private var sync = CloudSync()
    @StateObject private var screenTime = ScreenTime()
    @StateObject private var translator = Translator()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(sync)
                .environmentObject(screenTime)
                .environmentObject(translator)
        }
    }
}

struct RootView: View {
    @EnvironmentObject var sync: CloudSync
    @EnvironmentObject var screenTime: ScreenTime
    @EnvironmentObject var translator: Translator
    @Environment(\.scenePhase) private var phase

    @State private var syncStatus: String?

    var body: some View {
        NavigationStack {
            Group {
                if !screenTime.authorized {
                    AuthorizeView()
                } else if let rule = sync.activeRule {
                    ActiveRuleView(rule: rule, status: syncStatus, retry: { Task { await reconcile() } })
                } else {
                    NewRuleView()
                }
            }
            .navigationTitle("LocalControl")
            .toolbar {
                if screenTime.authorized {
                    NavigationLink { SetupView() } label: { Image(systemName: "gearshape") }
                }
            }
        }
        .task { await reconcile() }
        .onChange(of: phase) { _, p in if p == .active { Task { await reconcile() } } }
        .onChange(of: sync.rule) { _, _ in Task { await reconcile() } }
    }

    /// Makes Screen Time match the synced rule: lifts expired shields, and enforces rules set on the Mac.
    private func reconcile() async {
        let rule = sync.activeRule
        screenTime.refresh(activeRule: rule)
        guard let rule, screenTime.authorized, screenTime.applied?.ruleID != rule.id, !translator.busy else { return }
        syncStatus = "Rule set on \(rule.origin) — translating it into Screen Time settings…"
        do {
            let t = try await translator.translate(rule.command)
            let plan = Plan(ruleID: rule.id, mode: Plan.Mode(rawValue: t.mode) ?? .blocklist, domains: t.domains)
            try screenTime.apply(plan, until: rule)
            syncStatus = nil
        } catch {
            syncStatus = "Couldn't apply the rule from \(rule.origin): \(error.localizedDescription)"
        }
    }
}

// MARK: - Authorization & setup

struct AuthorizeView: View {
    @EnvironmentObject var screenTime: ScreenTime

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "hourglass").font(.system(size: 44)).foregroundStyle(.tint)
            Text("LocalControl enforces your rules with Screen Time.")
                .font(.headline).multilineTextAlignment(.center)
            Text("Everything runs on this phone, including the language model.")
                .foregroundStyle(.secondary).multilineTextAlignment(.center)
            Button("Allow Screen Time access") { Task { await screenTime.requestAuthorization() } }
                .buttonStyle(.borderedProminent)
        }
        .padding(32)
    }
}

struct SetupView: View {
    @EnvironmentObject var screenTime: ScreenTime
    @EnvironmentObject var translator: Translator
    @State private var pickAllowed = false
    @State private var pickDistracting = false

    var body: some View {
        Form {
            Section {
                Button("Choose apps…") { pickAllowed = true }
                LabeledContent("Selected", value: "\(screenTime.alwaysAllowed.applicationTokens.count) apps")
            } header: {
                Text("Always allowed")
            } footer: {
                Text("For “only use it for …” rules, every other app is shielded. Pick the ones you always need (Phone, Messages, Maps, a PDF reader…).")
            }
            Section {
                Button("Choose apps & categories…") { pickDistracting = true }
                LabeledContent("Selected", value: "\(screenTime.distracting.applicationTokens.count) apps, \(screenTime.distracting.categoryTokens.count) categories")
            } header: {
                Text("Distracting")
            } footer: {
                Text("For “no …” rules, these apps are shielded and the model's website list is blocked.")
            }
            Section("On-device model") {
                LabeledContent("Model", value: Translator.model.name)
                ModelStatusRow()
                if translator.state == .notLoaded {
                    Button("Download now") { Task { try? await translator.load() } }
                }
            }
        }
        .navigationTitle("Setup")
        .familyActivityPicker(isPresented: $pickAllowed, selection: $screenTime.alwaysAllowed)
        .familyActivityPicker(isPresented: $pickDistracting, selection: $screenTime.distracting)
    }
}

struct ModelStatusRow: View {
    @EnvironmentObject var translator: Translator

    var body: some View {
        switch translator.state {
        case .notLoaded: Text("Downloads (~1 GB) the first time you set a rule").foregroundStyle(.secondary)
        case .downloading(let f): ProgressView("Downloading model", value: f)
        case .loading: ProgressView("Loading model")
        case .ready: Label("Ready", systemImage: "checkmark.circle").foregroundStyle(.green)
        case .failed(let e): Label(e, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
        }
    }
}

// MARK: - Active rule

struct ActiveRuleView: View {
    @EnvironmentObject var screenTime: ScreenTime
    let rule: Rule
    let status: String?
    let retry: () -> Void

    var body: some View {
        List {
            Section {
                Text(rule.rule).font(.title3)
                Text("You said: “\(rule.command)”").font(.footnote).foregroundStyle(.secondary)
                TimelineView(.periodic(from: .now, by: 1)) { _ in
                    Text(Self.clock(rule.remaining) + " left")
                        .font(.system(size: 34, weight: .semibold)).monospacedDigit()
                }
                Label("Locked in until \(rule.expiresAt.formatted(date: .omitted, time: .shortened))",
                      systemImage: "lock.fill")
                    .foregroundStyle(.secondary)
            }
            if let plan = screenTime.applied, plan.ruleID == rule.id {
                Section(plan.mode == .allowlist ? "Only these websites" : "Blocked websites") {
                    ForEach(plan.domains, id: \.self) { Text($0) }
                    if plan.domains.isEmpty { Text("None").foregroundStyle(.secondary) }
                }
            } else {
                Section("This phone") {
                    if let status { Text(status) }
                    ModelStatusRow()
                    if status?.hasPrefix("Couldn't") == true { Button("Retry", action: retry) }
                }
            }
        }
    }

    static func clock(_ s: TimeInterval) -> String {
        let s = Int(s.rounded())
        let (h, m, x) = (s / 3600, s % 3600 / 60, s % 60)
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, x) : String(format: "%d:%02d", m, x)
    }
}

// MARK: - New rule

struct NewRuleView: View {
    @EnvironmentObject var sync: CloudSync
    @EnvironmentObject var translator: Translator
    @State private var text = ""
    @State private var minutes = ""
    @State private var draft: Draft?

    var body: some View {
        Form {
            Section {
                TextField("For the next hour, I can only use this phone to read papers or technical material.",
                          text: $text, axis: .vertical)
                    .lineLimit(3...6)
                TextField("Minutes (optional)", text: $minutes).keyboardType(.numberPad)
                Button {
                    Task { await translate() }
                } label: {
                    if translator.busy { ProgressView() } else { Text("Next") }
                }
                .disabled(text.trimmingCharacters(in: .whitespaces).isEmpty || translator.busy)
            } header: {
                Text("How should this phone be used?")
            }
            if translator.busy || translator.state != .ready {
                Section { ModelStatusRow() }
            }
            let saved = sync.history.top()
            if !saved.isEmpty {
                Section("Saved prompts") {
                    ForEach(saved) { p in
                        Button {
                            text = p.text
                            minutes = String(Int(p.minutes.rounded()))
                        } label: {
                            HStack {
                                Text(p.text).foregroundStyle(.primary).lineLimit(2)
                                Spacer()
                                Text("\(p.count)×").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                    .onDelete { idx in idx.map { saved[$0].text }.forEach(sync.forgetPrompt) }
                }
            }
        }
        .sheet(item: $draft) { d in ReviewView(draft: d) }
    }

    private func translate() async {
        let command = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let override = Double(minutes)
        var d = Draft(command: command, rule: command, minutes: Rule.clampMinutes(override), mode: .blocklist, domains: [])
        do {
            let t = try await translator.translate(command)
            d.rule = t.rule.isEmpty ? command : t.rule
            d.minutes = Rule.clampMinutes(override ?? t.duration_minutes)
            d.mode = Plan.Mode(rawValue: t.mode) ?? .blocklist
            d.domains = t.domains
        } catch {
            d.note = "The model couldn't translate this (\(error.localizedDescription)). Fill in the details yourself."
        }
        draft = d
    }
}

struct Draft: Identifiable {
    let id = UUID()
    var command: String
    var rule: String
    var minutes: Double
    var mode: Plan.Mode
    var domains: [String]
    var note: String?
}

struct ReviewView: View {
    @EnvironmentObject var sync: CloudSync
    @EnvironmentObject var screenTime: ScreenTime
    @Environment(\.dismiss) private var dismiss
    @State var draft: Draft
    @State private var newDomain = ""
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                if let note = draft.note { Section { Text(note).foregroundStyle(.orange) } }
                Section("Rule") {
                    TextField("Rule", text: $draft.rule, axis: .vertical)
                    Stepper("\(Int(draft.minutes)) minutes", value: $draft.minutes, in: 5...LC.maxMinutes, step: 5)
                }
                Section {
                    Picker("Mode", selection: $draft.mode) {
                        Text("Only allow").tag(Plan.Mode.allowlist)
                        Text("Block").tag(Plan.Mode.blocklist)
                    }
                    .pickerStyle(.segmented)
                } footer: {
                    Text(draft.mode == .allowlist
                         ? "All apps except your “always allowed” ones are shielded, and only the websites below open."
                         : "Your “distracting” apps and the websites below are blocked.")
                }
                Section(draft.mode == .allowlist ? "Allowed websites" : "Blocked websites") {
                    ForEach(draft.domains, id: \.self) { Text($0) }
                        .onDelete { draft.domains.remove(atOffsets: $0) }
                    HStack {
                        TextField("Add a domain", text: $newDomain)
                            .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                            .onSubmit(addDomain)
                        Button("Add", action: addDomain).disabled(newDomain.isEmpty)
                    }
                }
                if let error { Section { Text(error).foregroundStyle(.red) } }
            }
            .navigationTitle("Review")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Lock in", action: lockIn).bold() }
            }
        }
    }

    private func addDomain() {
        draft.domains = Translator.normalize(draft.domains + [newDomain])
        newDomain = ""
    }

    private func lockIn() {
        let now = Date()
        let rule = Rule(command: draft.command, rule: draft.rule, startedAt: now,
                        expiresAt: now.addingTimeInterval(Rule.clampMinutes(draft.minutes) * 60))
        do {
            // Apply first so a Screen Time failure doesn't leave a locked rule with nothing enforcing it.
            try screenTime.apply(Plan(ruleID: rule.id, mode: draft.mode, domains: draft.domains), until: rule)
            try sync.setRule(rule)
            dismiss()
        } catch {
            screenTime.clear()
            self.error = error.localizedDescription
        }
    }
}
