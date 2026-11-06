import AppKit
import MetalKit
import simd

/// Native AppKit input surface. Events never run their own event loop or GPU work.
final class WorkspaceViewport: MTKView {
  var orbit: ((SIMD2<Float>) -> Void)?
  var pan: ((SIMD2<Float>) -> Void)?
  var dolly: ((Float) -> Void)?
  var dollyAt: ((Float, SIMD2<Float>) -> Void)?
  var roll: ((Float) -> Void)?
  var navigationBoundary: ((Bool) -> Void)?
  var pick: ((SIMD2<UInt32>, StudioSelectionOperation) -> Void)?
  var hover: ((SIMD2<UInt32>?) -> Void)?
  var openURLs: (([URL]) -> Void)?
  var keyAction: ((UInt16) -> Bool)?
  var onDrawableSizeChanged: ((CGSize) -> Void)?
  var tool: ViewportTool = .select { didSet { window?.invalidateCursorRects(for: self) } }
  var sensitivity: Float = 1
  private(set) var renderScale: Float = 1
  private var drag = NavigationDrag()
  private var dragOperation: NavigationOperation = .select
  private var selectionOperation: StudioSelectionOperation = .replace
  private var dragCancelled = false
  private var tracking: NSTrackingArea?
  private var navigationActive = false
  private var finishWork: DispatchWorkItem?
  private var scrollOperation: NavigationOperation?
  override var acceptsFirstResponder: Bool { true }

  override init(frame frameRect: NSRect, device: MTLDevice?) {
    super.init(frame: frameRect, device: device)
    registerForDraggedTypes([.fileURL])
    autoresizingMask = [.width, .height]
    autoResizeDrawable = false
    setAccessibilityRole(.group)
    setAccessibilityIdentifier("studio.viewport")
  }
  required init(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
  override func resetCursorRects() {
    addCursorRect(
      bounds, cursor: tool == .select ? .arrow : (tool == .pan ? .openHand : .crosshair))
  }
  override func updateTrackingAreas() {
    super.updateTrackingAreas()
    if let tracking { removeTrackingArea(tracking) }
    tracking = NSTrackingArea(
      rect: .zero,
      options: [.mouseMoved, .mouseEnteredAndExited, .activeInKeyWindow, .inVisibleRect],
      owner: self)
    addTrackingArea(tracking!)
  }
  private func point(_ event: NSEvent) -> SIMD2<Float> {
    let p = convert(event.locationInWindow, from: nil)
    return SIMD2(Float(p.x - bounds.minX), Float(bounds.maxY - p.y))
  }
  private func modifiers(_ event: NSEvent) -> NavigationModifiers {
    var flags: NavigationModifiers = []
    if event.modifierFlags.contains(.option) { flags.insert(.option) }
    if event.modifierFlags.contains(.shift) { flags.insert(.shift) }
    if event.modifierFlags.contains(.control) { flags.insert(.control) }
    return flags
  }
  private func beginNavigation() {
    finishWork?.cancel()
    guard !navigationActive else { return }
    navigationActive = true
    navigationBoundary?(true)
  }
  private func endNavigation() {
    finishWork?.cancel()
    finishWork = nil
    guard navigationActive else { return }
    navigationActive = false
    navigationBoundary?(false)
  }
  private func finishSoon() {
    finishWork?.cancel()
    let work = DispatchWorkItem { [weak self] in self?.endNavigation() }
    finishWork = work
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.22, execute: work)
  }
  private func beginDrag(_ event: NSEvent) {
    endNavigation()
    scrollOperation = nil
    window?.makeFirstResponder(self)
    dragCancelled = false
    drag.begin(at: point(event))
    selectionOperation = event.modifierFlags.contains(.command) ? .toggle : .replace
    dragOperation = NavigationInputPolicy.operation(
      button: event.buttonNumber, modifiers: modifiers(event), tool: tool)
  }
  private func moveDrag(_ event: NSEvent) {
    guard !dragCancelled, let movement = drag.move(to: point(event)), dragOperation != .select
    else { return }
    beginNavigation()
    let delta = movement * max(0.05, min(5, sensitivity))
    switch dragOperation {
    case .orbit: orbit?(delta)
    case .pan: pan?(delta)
    case .dolly: performDolly((delta.x + delta.y) * 0.24, anchor: .init(repeating: 0.5))
    case .select: break
    }
  }
  private func endDrag(_ event: NSEvent) {
    defer { endNavigation() }
    guard !dragCancelled, event.buttonNumber == 0, !drag.crossedThreshold, dragOperation == .select,
      let pixel = pickPixel(event)
    else { return }
    pick?(pixel, selectionOperation)
  }
  override func mouseDown(with event: NSEvent) { beginDrag(event) }
  override func mouseDragged(with event: NSEvent) { moveDrag(event) }
  override func mouseUp(with event: NSEvent) { endDrag(event) }
  override func rightMouseDown(with event: NSEvent) { beginDrag(event) }
  override func rightMouseDragged(with event: NSEvent) { moveDrag(event) }
  override func rightMouseUp(with event: NSEvent) { endDrag(event) }
  override func otherMouseDown(with event: NSEvent) { beginDrag(event) }
  override func otherMouseDragged(with event: NSEvent) { moveDrag(event) }
  override func otherMouseUp(with event: NSEvent) { endDrag(event) }
  private func normalizedPoint(_ event: NSEvent) -> SIMD2<Float> {
    let p = point(event)
    return SIMD2(
      min(1, max(0, p.x / Float(max(1, bounds.width)))),
      min(1, max(0, p.y / Float(max(1, bounds.height)))))
  }
  private func performDolly(_ delta: Float, anchor: SIMD2<Float>) {
    if let dollyAt { dollyAt(delta, anchor) } else { dolly?(delta) }
  }
  override func scrollWheel(with event: NSEvent) {
    // Do not let releasing Option during momentum unexpectedly turn orbit into pan.
    let proposed = NavigationInputPolicy.scrollOperation(
      precise: event.hasPreciseScrollingDeltas, modifiers: modifiers(event))
    if event.phase.contains(.began) || scrollOperation == nil
      || (event.phase.isEmpty && event.momentumPhase.isEmpty)
    {
      scrollOperation = proposed
    }
    let operation = scrollOperation ?? proposed
    let dx = Float(event.scrollingDeltaX)
    let dy = Float(event.scrollingDeltaY)
    if dx != 0 || dy != 0 {
      beginNavigation()
      let gain = max(0.05, min(5, sensitivity))
      switch operation {
      case .orbit: orbit?(SIMD2(dx, dy) * gain)
      case .pan: pan?(SIMD2(dx, dy) * gain)
      case .dolly:
        performDolly(
          -dy * (event.hasPreciseScrollingDeltas ? 0.18 : 2) * gain, anchor: normalizedPoint(event))
      case .select: break
      }
    }
    // Delay only the history boundary, never input. Native momentum remains smooth.
    finishSoon()
    if event.phase.contains(.cancelled) || event.momentumPhase.contains(.ended)
      || event.momentumPhase.contains(.cancelled)
    {
      scrollOperation = nil
      endNavigation()
    }
  }
  override func magnify(with event: NSEvent) {
    guard event.magnification.isFinite else { return }
    if event.magnification != 0 {
      beginNavigation()
      performDolly(
        -log(max(0.05, Float(1 + event.magnification))) / 0.025, anchor: normalizedPoint(event))
    }
    finishSoon()
    if event.phase.contains(.cancelled) { endNavigation() }
  }
  override func rotate(with event: NSEvent) {
    guard event.rotation.isFinite else { return }
    if event.rotation != 0 {
      beginNavigation()
      roll?(Float(event.rotation) * Float.pi / 180)
    }
    finishSoon()
    if event.phase.contains(.cancelled) { endNavigation() }
  }
  private func pickPixel(_ event: NSEvent) -> SIMD2<UInt32>? {
    let p = point(event)
    guard p.x >= 0, p.y >= 0, p.x < Float(bounds.width), p.y < Float(bounds.height),
      drawableSize.width > 0, drawableSize.height > 0
    else { return nil }
    return SIMD2(
      UInt32(
        min(Float(drawableSize.width - 1), p.x / Float(bounds.width) * Float(drawableSize.width))),
      UInt32(
        min(Float(drawableSize.height - 1), p.y / Float(bounds.height) * Float(drawableSize.height))
      ))
  }
  override func mouseMoved(with event: NSEvent) { hover?(pickPixel(event)) }
  override func mouseExited(with event: NSEvent) { hover?(nil) }
  override func resignFirstResponder() -> Bool {
    dragCancelled = true
    endNavigation()
    return super.resignFirstResponder()
  }
  override func cancelOperation(_ sender: Any?) {
    dragCancelled = true
    endNavigation()
  }
  func setRenderScale(_ value: Float) {
    guard value.isFinite, value > 0, value <= 1 else { return }
    renderScale = value
    autoResizeDrawable = false
    updateDrawableDimensions()
  }
  private func updateDrawableDimensions() {
    let backing = convertToBacking(bounds).size
    guard backing.width > 0, backing.height > 0 else { return }
    let size = CGSize(
      width: max(1, (backing.width * CGFloat(renderScale)).rounded(.down)),
      height: max(1, (backing.height * CGFloat(renderScale)).rounded(.down)))
    if drawableSize != size {
      drawableSize = size
      onDrawableSizeChanged?(size)
    }
  }
  override func layout() {
    super.layout()
    updateDrawableDimensions()
  }
  override func viewDidChangeBackingProperties() {
    super.viewDidChangeBackingProperties()
    updateDrawableDimensions()
  }
  override func viewDidMoveToWindow() {
    super.viewDidMoveToWindow()
    updateDrawableDimensions()
  }
  override func keyDown(with event: NSEvent) {
    if keyAction?(event.keyCode) == true { return }
    super.keyDown(with: event)
  }
  override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
    sender.draggingPasteboard.canReadObject(
      forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) ? .copy : []
  }
  override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool { true }
  override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
    guard
      let urls = sender.draggingPasteboard.readObjects(
        forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty
    else { return false }
    openURLs?(urls)
    return true
  }
}

/// Accept file drops over the empty-document overlay as well as the viewport.
final class StudioDropZone: NSView {
  var openURLs: (([URL]) -> Void)?
  override init(frame frameRect: NSRect) {
    super.init(frame: frameRect)
    registerForDraggedTypes([.fileURL])
  }
  required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
  override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
    sender.draggingPasteboard.canReadObject(
      forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) ? .copy : []
  }
  override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
    guard
      let urls = sender.draggingPasteboard.readObjects(
        forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty
    else { return false }
    openURLs?(urls)
    return true
  }
}
