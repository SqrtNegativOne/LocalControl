import Foundation
import Network

/// Tiny HTTP server on 127.0.0.1 serving the block page and handling appeals.
@MainActor
final class BlockServer {
    private var listener: NWListener?
    private let engine: Engine

    init(engine: Engine) { self.engine = engine }

    func start() {
        let params = NWParameters.tcp
        params.requiredInterfaceType = .loopback
        params.allowLocalEndpointReuse = true
        let listener: NWListener
        do {
            listener = try NWListener(using: params, on: NWEndpoint.Port(rawValue: engine.port)!)
        } catch {
            engine.reportError("Block page server failed: \(error.localizedDescription)")
            return
        }
        listener.stateUpdateHandler = { [weak self] state in
            if case .failed(let error) = state {
                MainActor.assumeIsolated { self?.engine.reportError("Block page server failed: \(error.localizedDescription)") }
            }
        }
        listener.newConnectionHandler = { [weak self] conn in
            conn.start(queue: .main)
            MainActor.assumeIsolated { self?.receive(conn, buffer: Data()) }
        }
        listener.start(queue: .main)
        self.listener = listener
    }

    private func receive(_ conn: NWConnection, buffer: Data) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, done, error in
            MainActor.assumeIsolated {
                var buf = buffer
                if let data { buf.append(data) }
                if let req = Request(buf) {
                    self?.handle(req, conn)
                } else if done || error != nil || buf.count > 1_000_000 {
                    conn.cancel()
                } else {
                    self?.receive(conn, buffer: buf)
                }
            }
        }
    }

    private func handle(_ req: Request, _ conn: NWConnection) {
        let origin = "http://127.0.0.1:\(engine.port)"
        switch (req.method, req.path) {
        case ("GET", "/blocked"):
            let page = Bundle.main.url(forResource: "blocked", withExtension: "html").flatMap { try? Data(contentsOf: $0) }
            respond(conn, 200, "text/html; charset=utf-8", page ?? Data("Blocked".utf8))
        case ("GET", "/api/block"):
            json(conn, engine.blocks[req.query["id"] ?? ""] ?? [:])
        case ("POST", "/api/appeal"):
            // Only our own page may appeal (no cross-site form posts).
            guard req.headers["content-type"]?.hasPrefix("application/json") == true,
                  req.headers["origin"].map({ $0 == origin }) ?? true,
                  let body = try? JSONSerialization.jsonObject(with: req.body) as? [String: String]
            else { return json(conn, ["error": "bad request"], status: 400) }
            Task {
                let v = await engine.appeal(url: body["url"] ?? "", title: body["title"] ?? "",
                                            justification: (body["justification"] ?? "").trimmingCharacters(in: .whitespaces))
                json(conn, ["allowed": v.allowed, "reason": v.reason])
            }
        default:
            json(conn, ["error": "not found"], status: 404)
        }
    }

    private func json(_ conn: NWConnection, _ obj: Any, status: Int = 200) {
        respond(conn, status, "application/json", (try? JSONSerialization.data(withJSONObject: obj)) ?? Data())
    }

    private func respond(_ conn: NWConnection, _ status: Int, _ type: String, _ body: Data) {
        var head = "HTTP/1.1 \(status) \(status == 200 ? "OK" : "Error")\r\n"
        head += "Content-Type: \(type)\r\nContent-Length: \(body.count)\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
        conn.send(content: Data(head.utf8) + body, completion: .contentProcessed { _ in conn.cancel() })
    }
}

private struct Request {
    var method: String
    var path: String
    var query: [String: String]
    var headers: [String: String]
    var body: Data

    /// Parses a complete request, or returns nil if more bytes are needed.
    init?(_ data: Data) {
        guard let end = data.range(of: Data("\r\n\r\n".utf8)) else { return nil }
        let lines = String(decoding: data[..<end.lowerBound], as: UTF8.self).components(separatedBy: "\r\n")
        let first = lines[0].split(separator: " ")
        guard first.count >= 2 else { return nil }
        method = String(first[0])
        let comps = URLComponents(string: String(first[1]))
        path = comps?.path ?? "/"
        query = Dictionary((comps?.queryItems ?? []).map { ($0.name, $0.value ?? "") }) { a, _ in a }
        headers = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            headers[line[..<colon].lowercased()] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        let length = Int(headers["content-length"] ?? "0") ?? 0
        let bodyStart = end.upperBound
        guard data.count - bodyStart >= length else { return nil }
        body = data[bodyStart..<(bodyStart + length)]
    }
}
