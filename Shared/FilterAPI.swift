#if os(macOS)
import Foundation
import Security

/// What the app tells the network filter: the active rule and the hosts already judged under it.
struct FilterState: Codable, Equatable {
    var ruleID: String?
    var expiresAt = Date.distantPast
    /// Domains allowed and blocked under the rule; each covers its subdomains. Blocking wins.
    var allowed: Set<String> = []
    var blocked: Set<String> = []

    var isActive: Bool { ruleID != nil && expiresAt > Date() }
}

/// Served by the filter extension.
@objc protocol FilterXPC {
    func update(_ state: Data, reply: @escaping (Bool) -> Void)
}

/// Served by the app, so the filter can ask about hosts it hasn't seen.
@objc protocol FilterAppXPC {
    /// `app` is the signing identifier of the program connecting.
    func judge(host: String, app: String, reply: @escaping (Bool) -> Void)
}

enum FilterHosts {
    /// Always allowed: infrastructure that allowed pages and the system depend on.
    static let infrastructure = [
        "apple.com", "icloud.com", "icloud-content.com", "mzstatic.com", "apple-cloudkit.com", "apple-dns.net",
        "cdn-apple.com", "aaplimg.com", "apple.news",
        "gstatic.com", "googleapis.com", "googleusercontent.com", "ggpht.com", "gvt1.com", "gvt2.com",
        "google-analytics.com", "googletagmanager.com", "doubleclick.net", "googlesyndication.com", "recaptcha.net",
        "cloudflare.com", "cloudflare-dns.com", "cloudflare-ech.com", "cloudflareinsights.com", "cloudfront.net",
        "akamai.net", "akamaihd.net", "akamaized.net", "akamaiedge.net", "edgekey.net", "edgesuite.net",
        "fastly.net", "fastly.com", "fastlylb.net", "jsdelivr.net", "unpkg.com", "bootstrapcdn.com", "jquery.com",
        "typekit.net", "fonts.net", "hcaptcha.com", "amazonaws.com", "azureedge.net", "msedge.net", "windows.net",
        "mozilla.org", "mozilla.net", "mozilla.com", "githubusercontent.com", "githubassets.com", "sentry.io",
        "digicert.com", "letsencrypt.org", "lencr.org", "sectigo.com", "usertrust.com", "globalsign.com",
    ]

    /// `host` or one of its parent domains is in `domains`.
    static func matches(_ host: String, _ domains: some Collection<String>) -> Bool {
        var h = Substring(host)
        while true {
            if domains.contains(String(h)) { return true }
            guard let dot = h.firstIndex(of: ".") else { return false }
            h = h[h.index(after: dot)...]
        }
    }

    /// The site a host belongs to, roughly: "mail.google.com" -> "google.com", "www.bbc.co.uk" -> "bbc.co.uk".
    static func site(_ host: String) -> String {
        let parts = host.split(separator: ".")
        guard parts.count > 2, Double(parts.last!) == nil else { return host }
        let n = parts[parts.count - 2].count <= 3 && parts.last!.count == 2 ? 3 : 2
        return parts.suffix(n).joined(separator: ".")
    }

    /// A code-signing requirement matching programs signed by the same team as this one.
    static func sameTeamRequirement() -> String? {
        var code: SecCode?
        var staticCode: SecStaticCode?
        var info: CFDictionary?
        guard SecCodeCopySelf([], &code) == errSecSuccess, let code,
              SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let staticCode,
              SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess,
              let team = (info as? [String: Any])?[kSecCodeInfoTeamIdentifier as String] as? String
        else { return nil }
        return "anchor apple generic and certificate leaf[subject.OU] = \"\(team)\""
    }
}
#endif
