import Foundation
import NetworkExtension
import SystemExtensions

/// Installs the network filter system extension, keeps it enabled, and talks to it over XPC.
@MainActor
final class FilterClient: NSObject, ObservableObject {
    enum Status: Equatable { case checking, needsApproval, off, on, failed(String) }

    @Published private(set) var status = Status.checking
    /// Last time the extension answered; used to notice it has stopped running.
    private(set) var lastReply = Date()
    /// Asked for hosts the filter hasn't seen; returns whether to allow.
    var judge: ((String, String) async -> Bool)?

    private var connection: NSXPCConnection?
    private lazy var appSide = AppSide(client: self)
    private static var extensionID: String { (Bundle.main.bundleIdentifier ?? "") + ".filter" }

    func start() {
        NotificationCenter.default.addObserver(forName: .NEFilterConfigurationDidChange, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.refresh() }
        }
        let request = OSSystemExtensionRequest.activationRequest(forExtensionWithIdentifier: Self.extensionID, queue: .main)
        request.delegate = self
        OSSystemExtensionManager.shared.submitRequest(request)
    }

    /// Turns the filter on (macOS asks the first time).
    func enable() async {
        let mgr = NEFilterManager.shared()
        do {
            try await mgr.loadFromPreferences()
            if mgr.providerConfiguration == nil {
                let config = NEFilterProviderConfiguration()
                config.filterSockets = true
                config.filterPackets = false
                mgr.providerConfiguration = config
                mgr.localizedDescription = "LocalControl"
            }
            if !mgr.isEnabled {
                mgr.isEnabled = true
                try await mgr.saveToPreferences()
            }
            status = .on
        } catch {
            status = .failed(error.localizedDescription)
        }
    }

    func refresh() async {
        let mgr = NEFilterManager.shared()
        try? await mgr.loadFromPreferences()
        if status != .needsApproval { status = mgr.isEnabled ? .on : .off }
    }

    func push(_ state: FilterState) {
        guard status == .on, let data = try? JSONEncoder().encode(state) else { return }
        let proxy = connect().remoteObjectProxyWithErrorHandler { [weak self] _ in
            DispatchQueue.main.async { self?.connection = nil }
        } as? FilterXPC
        proxy?.update(data) { [weak self] ok in
            DispatchQueue.main.async { if ok { self?.lastReply = Date() } }
        }
    }

    private func connect() -> NSXPCConnection {
        if let connection { return connection }
        let conn = NSXPCConnection(machServiceName: Self.machServiceName, options: [])
        if let req = FilterHosts.sameTeamRequirement() { conn.setCodeSigningRequirement(req) }
        conn.remoteObjectInterface = NSXPCInterface(with: FilterXPC.self)
        conn.exportedInterface = NSXPCInterface(with: FilterAppXPC.self)
        conn.exportedObject = appSide
        conn.invalidationHandler = { [weak self] in DispatchQueue.main.async { self?.connection = nil } }
        conn.resume()
        connection = conn
        return conn
    }

    private static let machServiceName: String = {
        let url = Bundle.main.bundleURL.appendingPathComponent("Contents/Library/SystemExtensions/\(extensionID).systemextension")
        let ne = Bundle(url: url)?.object(forInfoDictionaryKey: "NetworkExtension") as? [String: Any]
        return ne?["NEMachServiceName"] as? String ?? ""
    }()

    /// Receives the filter's questions on an XPC queue and answers on the main actor.
    private final class AppSide: NSObject, FilterAppXPC {
        weak var client: FilterClient?
        init(client: FilterClient) { self.client = client }

        func judge(host: String, app: String, reply: @escaping (Bool) -> Void) {
            Task { @MainActor [weak client] in
                guard let judge = client?.judge else { return reply(false) }
                reply(await judge(host, app))
            }
        }
    }
}

extension FilterClient: OSSystemExtensionRequestDelegate {
    nonisolated func request(_ request: OSSystemExtensionRequest, actionForReplacingExtension existing: OSSystemExtensionProperties,
                             withExtension ext: OSSystemExtensionProperties) -> OSSystemExtensionRequest.ReplacementAction {
        .replace
    }

    nonisolated func requestNeedsUserApproval(_ request: OSSystemExtensionRequest) {
        MainActor.assumeIsolated { status = .needsApproval }
    }

    nonisolated func request(_ request: OSSystemExtensionRequest, didFinishWithResult result: OSSystemExtensionRequest.Result) {
        MainActor.assumeIsolated {
            status = .checking
            Task { await enable() }
        }
    }

    nonisolated func request(_ request: OSSystemExtensionRequest, didFailWithError error: Error) {
        MainActor.assumeIsolated { status = .failed(error.localizedDescription) }
    }
}
