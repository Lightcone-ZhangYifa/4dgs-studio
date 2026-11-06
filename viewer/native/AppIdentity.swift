import AppKit

/// Product identity belongs to the application bundle. About, Help and release
/// metadata read the same author record; build hosts and scene metadata never
/// supply an application author or a fallback account name.
struct AppIdentity {
  let name: String
  let version: String?
  let build: String?
  let copyright: String?
  let authorName: String?
  let authorNativeName: String?
  let authorProfileURL: URL?

  init(bundle: Bundle = .main) {
    var info = bundle.infoDictionary ?? [:]
    for key in ["CFBundleDisplayName", "CFBundleName", "NSHumanReadableCopyright"] {
      if let localized = bundle.object(forInfoDictionaryKey: key) { info[key] = localized }
    }
    self.init(info: info)
  }

  init(info: [String: Any]) {
    func text(_ value: Any?) -> String? {
      guard let value = value as? String else { return nil }
      let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
      return trimmed.isEmpty ? nil : trimmed
    }
    name = text(info["CFBundleDisplayName"]) ?? text(info["CFBundleName"]) ?? "4DGS Studio"
    version = text(info["CFBundleShortVersionString"])
    build = text(info["CFBundleVersion"])
    copyright = text(info["NSHumanReadableCopyright"])
    let author = info["GSVStudioAuthor"] as? [String: Any] ?? [:]
    authorName = text(author["Name"])
    authorNativeName = text(author["NativeName"])
    if let address = text(author["ProfileURL"]), let url = URL(string: address),
      url.scheme == "https", url.host?.isEmpty == false, url.user == nil, url.password == nil {
      authorProfileURL = url
    } else { authorProfileURL = nil }
  }

  var authorDisplayName: String? {
    var names = [String]()
    for name in [authorName, authorNativeName].compactMap({ $0 }) where !names.contains(name) {
      names.append(name)
    }
    return names.isEmpty ? nil : names.joined(separator: " · ")
  }

  /// AppKit owns and reuses its native About window. This value contains only
  /// local text, images and a user-activated link; it has no network requests,
  /// delegate captures, timers or workspace/document references.
  func aboutPanelOptions(icon: NSImage? = nil) -> [NSApplication.AboutPanelOptionKey: Any] {
    var options: [NSApplication.AboutPanelOptionKey: Any] = [
      .applicationName: name,
      .applicationVersion: version.map { L10n.format("about.version", "Version %@", $0) }
        ?? L10n.tr("about.versionUnavailable", "Version unavailable"),
      .version: build ?? "",
      NSApplication.AboutPanelOptionKey(rawValue: "Copyright"): copyright ?? "",
      .credits: aboutCredits(),
    ]
    if let icon { options[.applicationIcon] = icon }
    return options
  }

  private func aboutCredits() -> NSAttributedString {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    paragraph.lineSpacing = 3
    let attributes: [NSAttributedString.Key: Any] = [
      .font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.labelColor,
      .paragraphStyle: paragraph,
    ]
    let credits = NSMutableAttributedString(
      string: L10n.tr("about.description", "4D Gaussian Splatting"),
      attributes: attributes)
    if let author = authorDisplayName {
      credits.append(NSAttributedString(string: "\n\n" + author, attributes: attributes))
    }
    if let url = authorProfileURL {
      credits.append(NSAttributedString(string: "\n", attributes: attributes))
      var linkAttributes = attributes
      linkAttributes[.link] = url
      linkAttributes[.foregroundColor] = NSColor.linkColor
      linkAttributes[.underlineStyle] = NSUnderlineStyle.single.rawValue
      credits.append(NSAttributedString(
        string: L10n.tr("about.github", "GitHub"), attributes: linkAttributes))
    }
    return NSAttributedString(attributedString: credits)
  }
}
