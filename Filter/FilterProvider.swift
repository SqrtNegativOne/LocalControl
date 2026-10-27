import Foundation
import NetworkExtension
import Security

/// Network content filter. While a rule is active, connections to hosts that don't fit it are dropped.
/// The app judges hosts the filter hasn't seen; if the app can't be reached, they're dropped (fail closed),
/// so killing the app mid-rule cuts the network down to what was already allowed.
final class FilterProvider: NEFilterDataProvider {
    override func startFilter(completionHandler: @escaping (Error?) -> Void) {
        apply(NEFilterSettings(rules: [], defaultAction: .filterData)) { completionHandler($0) }
    }

    override func stopFilter(with reason: NEProviderStopReason, completionHandler: @escaping () -> Void) {
        completionHandler()
    }

    override func handleNewFlow(_ flow: NEFilterFlow) -> NEFilterNewFlowVerdict {
        guard Policy.shared.ruleActive, let flow = flow as? NEFilterSocketFlow,
              let remote = flow.remoteEndpoint as? NWHostEndpoint else { return .allow() }
        let app = Self.signingID(flow.sourceAppAuditToken)
        if Policy.isExempt(app: app) || Policy.isLocal(remote.hostname) { return .allow() }
        if flow.socketProtocol == IPPROTO_UDP {
            // QUIC hides the host name; dropping it makes browsers fall back to TCP, where it's visible.
            return remote.port == "443" ? .drop() : .allow()
        }
        if let host = flow.remoteHostname ?? flow.url?.host {
            switch Policy.shared.decide(host.lowercased(), app: app, resume: { [weak self] allow in
                self?.resumeFlow(flow, with: allow ? NEFilterNewFlowVerdict.allow() : NEFilterNewFlowVerdict.drop())
            }) {
            case .allow: return .allow()
            case .drop: return .drop()
            case .pending: return .pause()
            }
        }
        guard remote.port == "443" || remote.port == "80" else { return .allow() }
        // Read the host name from the TLS ClientHello (or HTTP Host header).
        return .filterDataVerdict(withFilterInbound: false, peekInboundBytes: 0, filterOutbound: true, peekOutboundBytes: 16384)
    }

    /// The code-signing identifier of the program that opened a flow, e.g. "com.google.Chrome.helper".
    private static func signingID(_ auditToken: Data?) -> String {
        var code: SecCode?
        var info: CFDictionary?
        guard let auditToken,
              SecCodeCopyGuestWithAttributes(nil, [kSecGuestAttributeAudit: auditToken] as CFDictionary, [], &code) == errSecSuccess,
              let code
        else { return "" }
        var staticCode: SecStaticCode?
        guard SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let staticCode,
              SecCodeCopySigningInformation(staticCode, [], &info) == errSecSuccess
        else { return "" }
        return (info as? [String: Any])?[kSecCodeInfoIdentifier as String] as? String ?? ""
    }

    override func handleOutboundData(from flow: NEFilterFlow, readBytesStartOffset offset: Int, readBytes: Data) -> NEFilterDataVerdict {
        switch HostSniffer.parse(readBytes) {
        case .needMore:
            return NEFilterDataVerdict(passBytes: 0, peekBytes: 16384)
        case .none:
            return .allow()
        case .host(let host):
            switch Policy.shared.decide(host, app: Self.signingID(flow.sourceAppAuditToken), resume: { [weak self] allow in
                self?.resumeFlow(flow, with: allow ? NEFilterDataVerdict.allow() : NEFilterDataVerdict.drop())
            }) {
            case .allow: return .allow()
            case .drop: return .drop()
            case .pending: return .pause()
            }
        }
    }
}

/// The filter's view of the rule, persisted so it survives restarts even if the app never runs again.
final class Policy {
    static let shared = Policy()
    enum Decision { case allow, drop, pending }

    private let lock = NSLock()
    private var state: FilterState
    private var waiting: [String: [(Bool) -> Void]] = [:]
    private let url: URL = {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("filter-state.json")
    }()

    init() {
        state = (try? Data(contentsOf: url)).flatMap { try? JSONDecoder().decode(FilterState.self, from: $0) } ?? FilterState()
    }

    var ruleActive: Bool { lock.withLock { state.isActive } }

    func update(_ new: FilterState) {
        lock.withLock { state = new }
        try? JSONEncoder().encode(new).write(to: url)
    }

    /// This extension's bundle ID is the app's plus ".filter".
    static let appBundleID = (Bundle.main.bundleIdentifier ?? "").replacingOccurrences(of: ".filter", with: "")

    static func isExempt(app: String) -> Bool {
        // LocalControl itself (pings to the accountability service) and the local model server.
        app.hasSuffix(appBundleID) || app.hasSuffix("ollama")
    }

    static func isLocal(_ host: String) -> Bool {
        host == "localhost" || host.hasPrefix("127.") || host == "::1" || host.hasPrefix("10.") ||
            host.hasPrefix("192.168.") || host.hasPrefix("169.254.") || host.hasPrefix("fe80:") ||
            (host.hasPrefix("172.") && (16...31).contains(Int(host.split(separator: ".").dropFirst().first ?? "") ?? 0))
    }

    /// Decides now if it can; otherwise asks the app and calls `resume` with the answer later.
    func decide(_ host: String, app: String, resume: @escaping (Bool) -> Void) -> Decision {
        let host = host.hasSuffix(".") ? String(host.dropLast()) : host
        guard IPC.shared.appConnected else { return quickDecision(host) ?? .drop }
        lock.lock()
        if let d = quickDecisionLocked(host) { lock.unlock(); return d }
        let first = waiting[host] == nil
        waiting[host, default: []].append(resume)
        let ruleID = state.ruleID
        lock.unlock()
        if first {
            IPC.shared.judge(host: host, app: app) { [self] allowed in finish(host, ruleID: ruleID, allowed: allowed) }
            DispatchQueue.global().asyncAfter(deadline: .now() + 20) { [self] in finish(host, ruleID: ruleID, allowed: nil) }
        }
        return .pending
    }

    private func quickDecision(_ host: String) -> Decision? { lock.withLock { quickDecisionLocked(host) } }

    private func quickDecisionLocked(_ host: String) -> Decision? {
        guard state.isActive else { return .allow }
        if FilterHosts.matches(host, FilterHosts.infrastructure) { return .allow }
        if FilterHosts.matches(host, state.blocked) { return .drop }
        if FilterHosts.matches(host, state.allowed) { return .allow }
        return nil
    }

    /// `allowed` nil means no answer (timeout or app gone): drop without remembering.
    private func finish(_ host: String, ruleID: String?, allowed: Bool?) {
        lock.lock()
        let resumes = waiting.removeValue(forKey: host) ?? []
        if let allowed, state.ruleID == ruleID {
            let site = FilterHosts.site(host)
            if allowed { state.allowed.insert(site) } else { state.blocked.insert(site) }
        }
        lock.unlock()
        resumes.forEach { $0(allowed ?? false) }
    }
}

/// Pulls the destination host name out of the first bytes a client sends.
enum HostSniffer {
    enum Result: Equatable { case host(String), needMore, none }

    static func parse(_ data: Data) -> Result {
        let b = [UInt8](data)
        guard let first = b.first else { return .needMore }
        if first == 0x16 { return tls(b) }
        // Plain HTTP starts with an upper-case method ("GET ", "POST ", ...); anything else isn't ours to read.
        let method = b.prefix(while: { $0 != 0x20 })
        guard method.allSatisfy({ (0x41...0x5A).contains($0) }), method.count <= 10 else { return .none }
        if method.count == b.count { return .needMore }
        // The Host header.
        let text = String(decoding: b.prefix(8192), as: UTF8.self)
        for line in text.split(separator: "\r\n") where line.lowercased().hasPrefix("host:") {
            let value = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
            return .host(String(value.split(separator: ":").first ?? "").lowercased())
        }
        return text.contains("\r\n\r\n") || b.count >= 8192 ? .none : .needMore
    }

    /// The server_name extension of a TLS ClientHello.
    private static func tls(_ b: [UInt8]) -> Result {
        guard b.count >= 5 else { return .needMore }
        let recordEnd = 5 + (Int(b[3]) << 8 | Int(b[4]))
        guard b.count >= recordEnd else { return .needMore }
        func u16(_ i: Int) -> Int { Int(b[i]) << 8 | Int(b[i + 1]) }
        var i = 5
        guard b[i] == 0x01 else { return .none }  // ClientHello
        i += 4 + 2 + 32                          // handshake header, version, random
        guard i < recordEnd else { return .none }
        i += 1 + Int(b[i])                       // session id
        guard i + 2 <= recordEnd else { return .none }
        i += 2 + u16(i)                          // cipher suites
        guard i < recordEnd else { return .none }
        i += 1 + Int(b[i])                       // compression methods
        guard i + 2 <= recordEnd else { return .none }
        let extEnd = min(recordEnd, i + 2 + u16(i))
        i += 2
        while i + 4 <= extEnd {
            let (type, len) = (u16(i), u16(i + 2))
            i += 4
            if type == 0 {  // server_name: list length, name type, name length, name
                guard i + 5 <= extEnd else { return .none }
                let start = i + 5, end = i + 5 + u16(i + 3)
                guard end <= extEnd else { return .none }
                return .host(String(decoding: b[start..<end], as: UTF8.self).lowercased())
            }
            i += len
        }
        return .none
    }
}

/// XPC with the app: it pushes the rule state, and answers questions about new hosts.
final class IPC: NSObject, NSXPCListenerDelegate, FilterXPC {
    static let shared = IPC()
    private var listener: NSXPCListener?
    private let lock = NSLock()
    private var app: NSXPCConnection?

    var appConnected: Bool { lock.withLock { app != nil } }

    func start() {
        guard let ne = Bundle.main.object(forInfoDictionaryKey: "NetworkExtension") as? [String: Any],
              let name = ne["NEMachServiceName"] as? String else { fatalError("No NEMachServiceName in Info.plist") }
        let listener = NSXPCListener(machServiceName: name)
        listener.delegate = self
        listener.resume()
        self.listener = listener
    }

    func listener(_ listener: NSXPCListener, shouldAcceptNewConnection conn: NSXPCConnection) -> Bool {
        // Only LocalControl (same signing team) may change the rule.
        if let req = FilterHosts.sameTeamRequirement() { conn.setCodeSigningRequirement(req) }
        conn.exportedInterface = NSXPCInterface(with: FilterXPC.self)
        conn.exportedObject = self
        conn.remoteObjectInterface = NSXPCInterface(with: FilterAppXPC.self)
        conn.invalidationHandler = { [weak self, weak conn] in
            guard let self else { return }
            self.lock.withLock { if self.app === conn { self.app = nil } }
        }
        conn.resume()
        lock.withLock { app = conn }
        return true
    }

    func update(_ state: Data, reply: @escaping (Bool) -> Void) {
        guard let s = try? JSONDecoder().decode(FilterState.self, from: state) else { return reply(false) }
        Policy.shared.update(s)
        reply(true)
    }

    func judge(host: String, app appID: String, completion: @escaping (Bool?) -> Void) {
        guard let conn = lock.withLock({ app }),
              let proxy = conn.remoteObjectProxyWithErrorHandler({ _ in completion(nil) }) as? FilterAppXPC
        else { return completion(nil) }
        proxy.judge(host: host, app: appID) { completion($0) }
    }
}
