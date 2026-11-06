import Foundation

/// Product strings use stable semantic keys; GSV identifiers and user-authored
/// labels remain data. The language is fixed for a session, so changing the
/// preference never leaves an existing workspace half translated.
enum L10n {
  static let supportedLanguages = ["en", "zh-Hans"]
  private static let preferenceKey = "StudioLanguage"
  static let language: String = validated(UserDefaults.standard.string(forKey: preferenceKey))
  static var preferredLanguage: String {
    validated(UserDefaults.standard.string(forKey: preferenceKey))
  }
  static var locale: Locale { .current }  // Region and UI language are independent preferences.
  static let resolvedLanguage: String = {
    if language != "system" { return language }
    return Bundle.preferredLocalizations(
      from: supportedLanguages, forPreferences: Locale.preferredLanguages
    ).first ?? "en"
  }()
  private static let localizedBundle: Bundle = {
    guard let path = Bundle.main.path(forResource: resolvedLanguage, ofType: "lproj"),
      let bundle = Bundle(path: path)
    else { return Bundle.main }
    return bundle
  }()
  private static func validated(_ value: String?) -> String {
    guard let value, value == "system" || supportedLanguages.contains(value) else {
      return "system"
    }
    return value
  }
  static func setPreferredLanguage(_ value: String) {
    guard value == "system" || supportedLanguages.contains(value) else { return }
    UserDefaults.standard.set(value, forKey: preferenceKey)
  }
  static func tr(_ key: String, _ fallback: String) -> String {
    localizedBundle.localizedString(forKey: key, value: fallback, table: "Localizable")
  }
  static func format(_ key: String, _ fallback: String, _ arguments: CVarArg...) -> String {
    String(format: tr(key, fallback), locale: locale, arguments: arguments)
  }
  static func number(_ value: Double, digits: Int = 4) -> String {
    let formatter = NumberFormatter()
    formatter.locale = locale
    formatter.maximumFractionDigits = max(0, digits)
    formatter.minimumFractionDigits = 0
    formatter.usesGroupingSeparator = false
    return formatter.string(from: NSNumber(value: value)) ?? String(value)
  }
  /// Editable timestamps must round-trip all finite f64 values without rounding
  /// distinct samples onto one another. Region controls the decimal separator.
  static func time(_ value: Double) -> String {
    String(value).replacingOccurrences(of: ".", with: locale.decimalSeparator ?? ".")
  }
  static func parse(_ value: String) -> Double? {
    let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !text.isEmpty else { return nil }
    // Accept portable scientific notation as well as the displayed regional
    // decimal separator, but reject non-finite values and trailing garbage.
    if let number = Double(text), number.isFinite { return number }
    let scanner = Scanner(string: text)
    scanner.locale = locale
    guard let number = scanner.scanDouble(), scanner.isAtEnd, number.isFinite else { return nil }
    return number
  }
}
