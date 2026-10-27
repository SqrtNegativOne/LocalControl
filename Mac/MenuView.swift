import SwiftUI

struct MenuView: View {
    @EnvironmentObject var engine: Engine
    @EnvironmentObject var sync: CloudSync

    @State private var command = ""
    @State private var minutes = ""
    @State private var busy = false
    @State private var error: String?
    @State private var showSettings = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            if let rule = sync.rule, rule.isActive {
                ActiveRuleView(rule: rule)
            } else {
                newRule
            }
            Divider()
            now
            if !engine.decisions.isEmpty { Divider(); decisions }
            Divider()
            today
            permissions
            footer
        }
        .padding(16)
        .frame(width: 380)
        .task { await engine.refreshModelStatus() }
    }

    private var header: some View {
        HStack {
            Text("LocalControl").font(.headline)
            Spacer()
            Label(engine.modelStatus.message, systemImage: "circle.fill")
                .labelStyle(StatusLabelStyle(ok: engine.modelStatus.ok))
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    // MARK: new rule

    private var newRule: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("How should this computer be used?").font(.subheadline.weight(.semibold))
            TextField("For the next hour, only papers or technical material.", text: $command, axis: .vertical)
                .lineLimit(2...4)
                .textFieldStyle(.roundedBorder)
                .onSubmit(lockIn)
            let saved = sync.history.top(6)
            if !saved.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(saved) { p in
                        HStack(spacing: 6) {
                            Button {
                                command = p.text
                                minutes = String(Int(p.minutes))
                            } label: {
                                Text(p.text).lineLimit(1).truncationMode(.tail)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .buttonStyle(.plain)
                            Text("\(p.count)× · \(Int(p.minutes))m").font(.caption).foregroundStyle(.secondary)
                            Button { sync.forgetPrompt(p.text) } label: { Image(systemName: "xmark") }
                                .buttonStyle(.plain).foregroundStyle(.tertiary).help("Forget")
                        }
                        .font(.callout)
                        .padding(.vertical, 3)
                    }
                }
            }
            HStack {
                TextField("min", text: $minutes).frame(width: 56).textFieldStyle(.roundedBorder)
                Text("minutes (blank: from your words, else 60)").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button(busy ? "Reading…" : "Lock in", action: lockIn)
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || command.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            if let error { Text(error).font(.caption).foregroundStyle(.red) }
        }
    }

    private func lockIn() {
        let text = command.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        busy = true
        error = nil
        Task {
            do {
                try await engine.lockIn(command: text, minutes: Double(minutes))
                command = ""
                minutes = ""
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }

    // MARK: status sections

    private var now: some View {
        VStack(alignment: .leading, spacing: 4) {
            SectionTitle("Right now")
            Text(engine.current?.label ?? "—").lineLimit(1).truncationMode(.middle)
            if sync.rule?.isActive == true {
                if let v = engine.currentVerdict {
                    HStack(alignment: .top, spacing: 6) {
                        Tag(allowed: v.allowed)
                        Text(v.reason).font(.caption).foregroundStyle(.secondary)
                    }
                } else {
                    Text("Judging…").font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Text("No rule active — just tracking usage.").font(.caption).foregroundStyle(.secondary)
            }
            if let e = engine.lastError {
                Text(e).font(.caption).foregroundStyle(.orange).lineLimit(3)
            }
        }
    }

    private var decisions: some View {
        VStack(alignment: .leading, spacing: 4) {
            SectionTitle("Decisions")
            ForEach(engine.decisions.prefix(5)) { d in
                HStack(alignment: .top, spacing: 6) {
                    switch d.kind {
                    case .judge, .appeal: Tag(allowed: d.allowed, appeal: d.kind == .appeal)
                    case .rule: Text("RULE").tagStyle(.blue)
                    case .ruleEnd: Text("ENDED").tagStyle(.gray)
                    }
                    VStack(alignment: .leading, spacing: 1) {
                        Text(d.label).lineLimit(1).truncationMode(.middle)
                        if !d.reason.isEmpty {
                            Text(d.reason).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        }
                    }
                }
                .font(.callout)
            }
        }
    }

    private var today: some View {
        HStack(alignment: .top, spacing: 16) {
            UsageList(title: "Sites today", rows: engine.usage.top(engine.usage.sites, 5))
            UsageList(title: "Apps today", rows: engine.usage.top(engine.usage.apps, 5))
        }
    }

    @ViewBuilder private var permissions: some View {
        if !Probe.accessibilityTrusted {
            Button("Grant Accessibility (needed for window titles and Firefox)") {
                NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
            }
            .font(.caption)
        }
        if AppDelegate.agentStatus == .requiresApproval {
            Button("Allow LocalControl in Login Items so it restarts if closed") {
                NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.LoginItems-Settings.extension")!)
            }
            .font(.caption)
        }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if showSettings {
                HStack {
                    Text("Model").font(.caption)
                    TextField("gemma3:4b", text: $engine.modelName).textFieldStyle(.roundedBorder)
                        .onSubmit { Task { await engine.refreshModelStatus() } }
                }
                Picker("Screenshots", selection: $engine.vision) {
                    Text("Off").tag("off")
                    Text("When needed").tag("auto")
                    Text("Always").tag("always")
                }
                .pickerStyle(.segmented)
                .font(.caption)
            }
            HStack {
                Button(showSettings ? "Done" : "Settings") { showSettings.toggle() }
                Spacer()
                if sync.rule?.isActive == true {
                    Text("Can't quit while a rule is locked in").font(.caption).foregroundStyle(.secondary)
                } else {
                    Button("Quit") { NSApp.terminate(nil) }
                }
            }
            .buttonStyle(.borderless)
            .font(.caption)
        }
    }
}

private struct ActiveRuleView: View {
    let rule: Rule

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionTitle("Locked in")
            Text(rule.rule).font(.body.weight(.medium)).fixedSize(horizontal: false, vertical: true)
            Text("You said: “\(rule.command)”" + (rule.origin != LC.deviceName ? " (from \(rule.origin))" : ""))
                .font(.caption).foregroundStyle(.secondary)
            TimelineView(.periodic(from: .now, by: 1)) { _ in
                HStack(alignment: .firstTextBaseline) {
                    Text(Self.clock(rule.remaining)).font(.system(size: 26, weight: .semibold).monospacedDigit())
                    Text("left · until \(rule.expiresAt.formatted(date: .omitted, time: .shortened))")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    static func clock(_ s: TimeInterval) -> String {
        let t = Int(s.rounded())
        let (h, m, sec) = (t / 3600, t % 3600 / 60, t % 60)
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%d:%02d", m, sec)
    }
}

private struct UsageList: View {
    let title: String
    let rows: [(String, Double)]

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            SectionTitle(title)
            if rows.isEmpty { Text("—").font(.caption).foregroundStyle(.secondary) }
            ForEach(rows, id: \.0) { name, secs in
                HStack {
                    Text(name).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Text(Self.duration(secs)).foregroundStyle(.secondary).monospacedDigit()
                }
                .font(.caption)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    static func duration(_ s: Double) -> String {
        s >= 3600 ? "\(Int(s) / 3600)h \(Int(s) % 3600 / 60)m" : "\(max(1, Int(s) / 60))m"
    }
}

private struct SectionTitle: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary).tracking(0.6)
    }
}

private struct Tag: View {
    let allowed: Bool
    var appeal = false
    var body: some View {
        Text(appeal ? "APPEAL" : allowed ? "ALLOW" : "BLOCK").tagStyle(allowed ? .green : .red)
    }
}

private extension Text {
    func tagStyle(_ color: Color) -> some View {
        self.font(.system(size: 9, weight: .bold))
            .padding(.horizontal, 5).padding(.vertical, 2)
            .foregroundStyle(color)
            .background(color.opacity(0.15), in: RoundedRectangle(cornerRadius: 3))
    }
}

private struct StatusLabelStyle: LabelStyle {
    let ok: Bool
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 4) {
            configuration.icon.font(.system(size: 6)).foregroundStyle(ok ? .green : .red)
            configuration.title
        }
    }
}
