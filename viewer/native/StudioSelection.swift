/// Selection is independent of the camera used to observe the scene.
enum StudioSelection: Hashable {
  case document
  case object(String)
  case element(String, Int64)
  case camera(String, Int)

  var objectID: String? {
    switch self {
    case .document: return nil
    case .object(let id), .element(let id, _), .camera(let id, _): return id
    }
  }
}

enum StudioSelectionOperation { case replace, toggle }

/// Ordered mixed selection, with one primary inspection/authoring target.
/// Object membership does not erase individually selected identities or views.
struct StudioSelectionSet: Equatable {
  private(set) var items: [StudioSelection]
  private(set) var primary: StudioSelection

  init(items: [StudioSelection] = [], primary: StudioSelection? = nil) {
    var seen = Set<StudioSelection>()
    let uniqueItems = items.filter { $0 != .document && seen.insert($0).inserted }
    self.items = uniqueItems
    self.primary = primary.flatMap { uniqueItems.contains($0) ? $0 : nil }
      ?? uniqueItems.last ?? .document
  }

  func applying(_ item: StudioSelection, operation: StudioSelectionOperation) -> Self {
    if operation == .replace { return Self(items: [item], primary: item) }
    if item == .document { return self }
    if items.contains(item) {
      return Self(items: items.filter { $0 != item }, primary: primary == item ? nil : primary)
    }
    return Self(items: items + [item], primary: item)
  }
}
