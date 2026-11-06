import AppKit

extension WorkspaceController {
  /// Only a visible Metal view needs a separately captured GPU image. A hidden
  /// viewport must never overwrite the visible comparison or preview panes.
  var workspaceNeedsViewportCapture: Bool { !viewport.isHiddenOrHasHiddenAncestor }

  /// Prepare immutable PNG bytes before a save sheet can dim its parent window.
  /// The caller and the viewport provider's completion both run on the main thread.
  func prepareWorkspaceSnapshot(
    captureViewport: (@escaping (Result<CGImage, Error>) -> Void) -> Void,
    completion: @escaping (Result<Data, Error>) -> Void
  ) {
    let finish: (Result<CGImage?, Error>) -> Void = { result in
      completion(Result { try self.workspaceSnapshotPNG(viewportImage: result.get()) })
    }
    if workspaceNeedsViewportCapture {
      captureViewport { finish($0.map { Optional($0) }) }
    } else { finish(.success(nil)) }
  }

  func workspaceSnapshotPNG(viewportImage: CGImage? = nil) throws -> Data {
    guard let window, let root = window.contentView?.superview else {
      throw SceneError.invalid(L10n.tr("error.workspaceCapture", "Cannot capture workspace controls."))
    }
    root.layoutSubtreeIfNeeded()
    guard let bitmap = root.bitmapImageRepForCachingDisplay(in: root.bounds) else {
      throw SceneError.invalid(L10n.tr("error.workspaceCapture", "Cannot capture workspace controls."))
    }
    root.cacheDisplay(in: root.bounds, to: bitmap)
    let snapshot = NSImage(size: root.bounds.size)
    snapshot.lockFocus()
    bitmap.draw(in: root.bounds)
    if workspaceNeedsViewportCapture {
      guard let viewportImage else {
        snapshot.unlockFocus()
        throw SceneError.invalid(L10n.tr("error.workspaceCapture", "Cannot capture workspace controls."))
      }
      NSImage(cgImage: viewportImage, size: viewport.bounds.size).draw(
        in: root.convert(viewport.bounds, from: viewport))
    }
    snapshot.unlockFocus()
    guard let data = snapshot.tiffRepresentation,
      let original = NSBitmapImageRep(data: data)?.cgImage
    else {
      throw SceneError.invalid(L10n.tr("error.workspaceEncoding", "Workspace PNG encoding failed."))
    }
    // Export content rather than native window chrome, whose toolbar vibrancy
    // cannot be cached faithfully by AppKit.
    let layout = window.contentLayoutRect
    let scale = CGFloat(original.width) / root.bounds.width
    let crop = CGRect(
      x: layout.minX * scale, y: (root.bounds.maxY - layout.maxY) * scale,
      width: layout.width * scale, height: layout.height * scale)
    guard let content = original.cropping(to: crop),
      let png = NSBitmapImageRep(cgImage: content).representation(using: .png, properties: [:])
    else {
      throw SceneError.invalid(L10n.tr("error.workspaceContentEncoding", "Workspace content PNG encoding failed."))
    }
    return png
  }
}
