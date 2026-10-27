import Foundation

struct LLMError: LocalizedError {
    var message: String
    var errorDescription: String? { message }
}

private struct ChatReply: Decodable {
    struct Message: Decodable { var content: String }
    var message: Message
}

/// Minimal client for a local Ollama server.
struct Ollama {
    var baseURL = URL(string: "http://127.0.0.1:11434")!
    var model: String

    func chatJSON<T: Decodable>(_ type: T.Type, system: String, user: String, schema: String,
                                images: [Data] = []) async throws -> T {
        var msg: [String: Any] = ["role": "user", "content": user]
        if !images.isEmpty { msg["images"] = images.map { $0.base64EncodedString() } }
        let body: [String: Any] = [
            "model": model, "stream": false, "keep_alive": "30m",
            "format": try JSONSerialization.jsonObject(with: Data(schema.utf8)),
            "options": ["temperature": 0],
            "messages": [["role": "system", "content": system], msg],
        ]
        var req = URLRequest(url: baseURL.appendingPathComponent("api/chat"), timeoutInterval: 90)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let data: Data
        do {
            let (d, resp) = try await URLSession.shared.data(for: req)
            if let http = resp as? HTTPURLResponse, http.statusCode != 200 {
                throw LLMError(message: "Ollama HTTP \(http.statusCode): \(String(decoding: d.prefix(300), as: UTF8.self))")
            }
            data = d
        } catch let e as LLMError {
            throw e
        } catch {
            throw LLMError(message: "Cannot reach Ollama: \(error.localizedDescription)")
        }
        guard let content = try? JSONDecoder().decode(ChatReply.self, from: data).message.content,
              let out = try? JSONDecoder().decode(T.self, from: Data(content.utf8))
        else { throw LLMError(message: "Unparseable model output") }
        return out
    }

    /// (ok, message): whether Ollama is up and the model is pulled.
    func status() async -> (Bool, String) {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/tags"), timeoutInterval: 3)
        req.httpMethod = "GET"
        guard let (data, _) = try? await URLSession.shared.data(for: req) else {
            return (false, "Ollama isn't running")
        }
        struct Tags: Decodable { struct M: Decodable { var name: String }; var models: [M] }
        let names = Set((try? JSONDecoder().decode(Tags.self, from: data))?.models.map(\.name) ?? [])
        if names.contains(model) || names.contains(model + ":latest") { return (true, "\(model) ready") }
        return (false, "Run: ollama pull \(model)")
    }
}
