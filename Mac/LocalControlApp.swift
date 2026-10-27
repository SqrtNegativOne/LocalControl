import ServiceManagement
import SwiftUI

@main
struct LocalControlApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            MenuView()
                .environmentObject(delegate.engine)
                .environmentObject(delegate.sync)
                .environmentObject(delegate.filter)
                .environmentObject(delegate.accountability)
        } label: {
            MenuLabel().environmentObject(delegate.sync)
        }
        .menuBarExtraStyle(.window)
    }
}

private struct MenuLabel: View {
    @EnvironmentObject var sync: CloudSync
    var body: some View {
        if let rule = sync.rule, rule.isActive {
            Image("MenuIcon")
            Text("until \(rule.expiresAt.formatted(date: .omitted, time: .shortened))")
        } else {
            Image("MenuIcon")
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    static let agentLabel = "com.eugenevinitsky.localcontrol.agent"
    /// Posted by a user-launched copy so the running agent copy shows its window.
    static let showNotification = Notification.Name("com.eugenevinitsky.localcontrol.show")

    let sync = CloudSync()
    lazy var engine = Engine(sync: sync)
    let filter = FilterClient()
    let accountability = Accountability()
    private var watchdog: Watchdog?
    private var termSignal: DispatchSourceSignal?
    private var server: BlockServer?
    private var window: NSWindow?

    /// Launched by launchd (the keep-alive agent) rather than by the user.
    private var isAgent: Bool { ProcessInfo.processInfo.environment["XPC_SERVICE_NAME"] == Self.agentLabel }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let args = ProcessInfo.processInfo.arguments
        if args.contains("--selftest") {
            Task { await engine.selfTest(); exit(0) }
            return
        }
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
            .filter { $0 != .current }
        var userJustOpened = !isAgent
        if isAgent {
            // The launchd copy is canonical: it gets restarted if killed. Another copy running
            // means the user just opened the app, so show the window in its place.
            others.forEach { $0.forceTerminate() }
            userJustOpened = !others.isEmpty
        } else if !args.contains("--no-agent"), handOffToAgent(hasOtherInstance: !others.isEmpty) {
            // If the agent copy is already running, ask it to show itself. (If it's just starting,
            // it terminates this copy and shows its window on its own.)
            DistributedNotificationCenter.default().postNotificationName(
                Self.showNotification, object: nil, userInfo: nil, deliverImmediately: true)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { NSApp.terminate(nil) }
            return
        }
        DistributedNotificationCenter.default().addObserver(
            forName: Self.showNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.showWindow() }
        }
        if userJustOpened { showWindow() }
        if !Probe.accessibilityTrusted { Probe.requestAccessibility() }
        let watchdog = Watchdog(sync: sync, filter: filter, accountability: accountability) { [weak self] message in
            self?.engine.recordAlert(message, partnerTold: self?.accountability.isConfigured == true)
        }
        watchdog.launched()
        self.watchdog = watchdog
        engine.watchdog = watchdog
        catchTermination()
        engine.filter = filter
        filter.judge = { [weak self] host, app in await self?.engine.judgeHost(host, app: app) ?? false }
        filter.start()
        engine.start()
        server = BlockServer(engine: engine)
        server?.start()
    }

    /// `kill` and `launchctl bootout` send SIGTERM, which would skip applicationShouldTerminate. During a
    /// rule, ignore it and tell the partner; launchd then force-kills the app after its exit timeout.
    private func catchTermination() {
        signal(SIGTERM, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        source.setEventHandler { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { exit(0) }
                guard self.sync.activeRule != nil else {
                    self.engine.saveUsage()
                    self.watchdog?.quitting()
                    exit(0)
                }
                self.watchdog?.report("Something tried to stop LocalControl from the command line during a rule.")
            }
        }
        source.resume()
        termSignal = source
    }

    /// Registers the keep-alive agent; returns true if this user-launched copy should exit.
    private func handOffToAgent(hasOtherInstance: Bool) -> Bool {
        let agent = SMAppService.agent(plistName: "\(Self.agentLabel).plist")
        if agent.status != .enabled { try? agent.register() }
        guard agent.status == .enabled else { return hasOtherInstance }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "gui/\(getuid())/\(Self.agentLabel)"]
        try? p.run()
        return true
    }

    /// Opening the app from Finder or Spotlight shows the same panel as the menu bar icon, in a window.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return false
    }

    func showWindow() {
        if window == nil {
            let host = NSHostingController(rootView: MenuView().environmentObject(engine).environmentObject(sync)
                .environmentObject(filter).environmentObject(accountability))
            let w = NSWindow(contentViewController: host)
            w.title = "LocalControl"
            w.styleMask = [.titled, .closable, .miniaturizable]
            w.isReleasedWhenClosed = false
            w.center()
            w.delegate = self
            window = w
        }
        // Menu-bar only by default; a Dock icon and ⌘Tab entry while the window is open.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if sync.activeRule != nil { return .terminateCancel }
        engine.saveUsage()
        watchdog?.quitting()
        return .terminateNow
    }

    static var agentStatus: SMAppService.Status {
        SMAppService.agent(plistName: "\(agentLabel).plist").status
    }
}
