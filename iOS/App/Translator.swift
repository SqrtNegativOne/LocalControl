import Foundation
import HuggingFace
import MLXGuidedGeneration
import MLXLLM
import MLXLMCommon
import Tokenizers

/// What the on-device model returns for a command: the rule plus concrete Screen Time settings.
struct Translation: Decodable {
    var duration_minutes: Double
    var rule: String
    var mode: String
    var domains: [String]
}

/// Turns a plain-English command into a Screen Time plan with a small on-device model.
@MainActor
final class Translator: ObservableObject {
    enum State: Equatable {
        case notLoaded
        case downloading(Double)
        case loading
        case ready
        case failed(String)
    }

    @Published private(set) var state: State = .notLoaded
    @Published private(set) var busy = false

    static let model = LLMRegistry.qwen3_1_7b_4bit
    private var container: ModelContainer?

    static let system = PromptText.parseSystem + """

    You also configure Screen Time on an iPhone to enforce the policy:
    - mode: "allowlist" if the instruction says the device may ONLY be used for certain things (everything else is \
    blocked); "blocklist" if it forbids specific things (everything else is allowed).
    - domains: for an allowlist, the websites needed for the allowed activity; for a blocklist, the websites to block. \
    Use bare registrable domains like "arxiv.org" (no https://, no paths). Be thorough: 10 to 40 domains.
    Examples. Papers or technical material: arxiv.org, scholar.google.com, semanticscholar.org, openreview.net, \
    aclanthology.org, proceedings.neurips.cc, dl.acm.org, ieeexplore.ieee.org, nature.com, sciencedirect.com, \
    springer.com, google.com, wikipedia.org, github.com, stackoverflow.com, paperswithcode.com, huggingface.co. \
    No social media: x.com, twitter.com, instagram.com, tiktok.com, facebook.com, reddit.com, threads.net, \
    bsky.app, linkedin.com, snapchat.com, pinterest.com, tumblr.com, youtube.com.
    """

    static let schema = #"""
    {"type":"object","properties":{"duration_minutes":{"type":"number"},"rule":{"type":"string"},"mode":{"type":"string","enum":["allowlist","blocklist"]},"domains":{"type":"array","items":{"type":"string"},"maxItems":50}},"required":["duration_minutes","rule","mode","domains"]}
    """#

    func load() async throws -> ModelContainer {
        if let container { return container }
        state = .downloading(0)
        do {
            let c = try await loadModelContainer(
                from: HubDownloader(hub: .default), using: TransformersTokenizerLoader(), configuration: Self.model
            ) { [weak self] p in
                let f = p.fractionCompleted
                Task { @MainActor in
                    guard let self else { return }
                    self.state = f < 1 ? .downloading(f) : .loading
                }
            }
            container = c
            state = .ready
            return c
        } catch {
            state = .failed(error.localizedDescription)
            throw error
        }
    }

    func translate(_ command: String) async throws -> Translation {
        busy = true
        defer { busy = false }
        let c = try await load()
        let system = Self.system, schema = Self.schema, user = PromptText.parseUser(command)
        let json: String = try await c.perform { context in
            let tokenizer = context.tokenizer
            let vocab = TokenizerVocabExtractor.extractForGrammar(from: tokenizer)
            let grammarTokenizer = try GrammarTokenizer(
                vocab: vocab.vocab, vocabType: vocab.vocabType, eosTokenId: Int32(tokenizer.eosTokenId ?? 0))
            let constraint = try GrammarConstraint(
                tokenizer: grammarTokenizer, jsonSchema: schema, fastForward: true, hostTokenizer: tokenizer)
            let input = try await context.processor.prepare(
                input: UserInput(chat: [.system(system), .user(user)],
                                 additionalContext: ["enable_thinking": false]))
            var out = ""
            try GuidedGenerationLoop.run(
                input: input, context: context, constraint: constraint,
                maxTokens: 800, vocabSize: grammarTokenizer.vocabSize
            ) { delta in
                out += delta
                return true
            }
            return out
        }
        var t = try JSONDecoder().decode(Translation.self, from: Data(json.utf8))
        t.domains = Self.normalize(t.domains)
        return t
    }

    /// "https://www.arxiv.org/abs" -> "arxiv.org"; dedupes and caps at 50.
    static func normalize(_ domains: [String]) -> [String] {
        var seen = Set<String>()
        return domains.compactMap { raw in
            var d = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if let r = d.range(of: "://") { d = String(d[r.upperBound...]) }
            d = String(d.split(separator: "/").first ?? "")
            if d.hasPrefix("www.") { d.removeFirst(4) }
            guard d.contains("."), !d.contains(" "), seen.insert(d).inserted else { return nil }
            return d
        }.prefix(50).map { $0 }
    }
}
