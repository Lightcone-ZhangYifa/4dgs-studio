import AppKit

/// A time viewport uses document times, never an inferred capture frame rate.
struct TimelineWindow {
  private(set) var lower: Double = 0
  private(set) var upper: Double = 1
  var span: Double { upper - lower }
  private(set) var rangeRejected = false

  @discardableResult mutating func fit(_ times: [Double]) -> Bool {
    guard times.allSatisfy({ $0.isFinite }) else {
      rangeRejected = true
      return false
    }
    var lo = times.min() ?? 0
    var hi = times.max() ?? 1
    if lo == hi {
      // A unit interval may be smaller than one ULP at a large clock origin.
      let width = max(1, lo.ulp * 8)
      let half = width * 0.5
      let a = lo - half
      let b = hi + half
      lo = a.isFinite ? a : lo
      hi = b.isFinite ? b : hi
      if lo == hi {
        lo = lo.nextDown
        hi = hi.nextUp
      }
    }
    let extent = hi - lo
    guard extent.isFinite, extent > 0 else {
      rangeRejected = true
      return false
    }
    let margin = extent * 0.025
    let paddedLo = lo - margin
    let paddedHi = hi + margin
    if paddedLo.isFinite, paddedHi.isFinite, (paddedHi - paddedLo).isFinite {
      lo = paddedLo
      hi = paddedHi
    }
    return assign(lo, hi)
  }
  @discardableResult mutating func zoom(_ factor: Double, anchor: Double) -> Bool {
    guard factor.isFinite, factor > 0, anchor.isFinite else { return false }
    let a = min(1, max(0, anchor))
    let point = time(at: a)
    let scaled = span * factor
    guard scaled.isFinite else {
      rangeRejected = true
      return false
    }
    // Keep enough representable doubles around the anchor for a real ruler.
    let newSpan = max(max(1e-12, point.ulp * 8), scaled)
    let lo = point - newSpan * a
    let hi = point + newSpan * (1 - a)
    return assign(lo, hi)
  }
  @discardableResult mutating func pan(_ fraction: Double) -> Bool {
    guard fraction.isFinite else { return false }
    let shift = span * fraction
    guard shift.isFinite else {
      rangeRejected = true
      return false
    }
    return assign(lower + shift, upper + shift)
  }
  private mutating func assign(_ lo: Double, _ hi: Double) -> Bool {
    guard lo.isFinite, hi.isFinite, hi > lo, (hi - lo).isFinite else {
      rangeRejected = true
      return false
    }
    lower = lo
    upper = hi
    rangeRejected = false
    return true
  }
  func time(at fraction: Double) -> Double {
    guard fraction.isFinite else { return lower }
    let value = lower + fraction * span
    return value.isFinite ? value : (fraction < 0 ? lower : upper)
  }
}

final class StudioTimelineView: NSView {
  var times = [0.0] {
    didSet {
      if oldValue != times {
        if !userRange { fit() }
        needsDisplay = true
      }
    }
  }
  var currentTime = 0.0 { didSet { needsDisplay = true } }
  var unit = "second" { didSet { needsDisplay = true } }
  var keyTimes = [Double]() {
    didSet {
      if oldValue != keyTimes { cancelKeyGesture() }
      needsDisplay = true
    }
  }
  var keyName = "" { didSet { needsDisplay = true } }
  var selectedKey: Int? { didSet { needsDisplay = true } }
  var previewRange: ClosedRange<Double>? { didSet { needsDisplay = true } }
  var onSeek: ((Double) -> Void)?
  var onKeySelect: ((Int) -> Void)?
  var onKeyMove: ((Int, Double) -> Void)?
  var onRangeError: (() -> Void)?
  var snap = true
  private var windowRange = TimelineWindow()
  private var userRange = false
  private var movingKey: Int?
  private var ghostTime: Double?
  private var gestureCancelled = false
  private var downX: CGFloat = 0
  private var lastX: CGFloat = 0
  private let gutter: CGFloat = 138
  override var isFlipped: Bool { true }
  override var acceptsFirstResponder: Bool { true }
  override init(frame frameRect: NSRect) {
    super.init(frame: frameRect)
    setAccessibilityElement(true)
    setAccessibilityRole(.slider)
    setAccessibilityLabel(
      L10n.tr("timeline.canvas", "Timeline. Click to scrub; drag a camera keyframe to retime."))
  }
  required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
  func fit() {
    userRange = false
    if !windowRange.fit(times) { onRangeError?() }
    needsDisplay = true
  }
  func zoom(_ factor: Double) {
    if !windowRange.zoom(factor, anchor: 0.5), windowRange.rangeRejected { onRangeError?() }
    userRange = true
    needsDisplay = true
  }
  private var contentWidth: CGFloat { max(1, bounds.width - gutter - 16) }
  private func x(_ time: Double) -> CGFloat {
    let fraction = (time - windowRange.lower) / windowRange.span
    // Keep drawing paths finite for source points outside a restricted view.
    guard fraction.isFinite else { return time < windowRange.lower ? -1e6 : 1e6 }
    return gutter + CGFloat(max(-1e6, min(1e6, fraction))) * contentWidth
  }
  private func time(_ x: CGFloat, snapEnabled: Bool = true) -> Double {
    let value = windowRange.time(at: Double((x - gutter) / contentWidth))
    guard snap, snapEnabled, !times.isEmpty else { return value }
    // Snap only when close on screen, preserving fractional document times.
    var low = 0
    var high = times.count
    while low < high {
      let m = (low + high) / 2
      if times[m] < value { low = m + 1 } else { high = m }
    }
    let candidates = [max(0, low - 1), min(times.count - 1, low)]
    let nearest = candidates.map { times[$0] }.min { abs($0 - value) < abs($1 - value) }!
    return abs(self.x(nearest) - x) <= 7 ? nearest : value
  }
  override func draw(_ dirtyRect: NSRect) {
    NSColor.windowBackgroundColor.setFill()
    bounds.fill()
    NSColor(calibratedWhite: 0.12, alpha: 1).setFill()
    NSRect(x: gutter, y: 0, width: contentWidth + 16, height: bounds.height).fill()
    let font = NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .regular)
    func text(_ value: String, _ point: NSPoint, _ color: NSColor = .secondaryLabelColor) {
      NSString(string: value).draw(
        at: point, withAttributes: [.font: font, .foregroundColor: color])
    }
    text(
      L10n.tr("timeline.time", "TIME") + " · " + L10n.tr("unit." + unit, unit), NSPoint(x: 12, y: 8)
    )
    text(L10n.tr("timeline.source", "Source samples"), NSPoint(x: 12, y: 40))
    let title = keyName.isEmpty ? L10n.tr("timeline.cameraKeys", "Camera keys") : keyName
    let paragraph = NSMutableParagraphStyle()
    paragraph.lineBreakMode = .byTruncatingTail
    NSString(string: title).draw(
      in: NSRect(x: 12, y: 72, width: gutter - 22, height: 18),
      withAttributes: [
        .font: font, .foregroundColor: NSColor.labelColor, .paragraphStyle: paragraph,
      ])
    if let range = previewRange {
      NSColor.controlAccentColor.withAlphaComponent(0.07).setFill()
      let a = max(gutter, x(range.lowerBound))
      let b = min(bounds.maxX, x(range.upperBound))
      if b > a { NSRect(x: a, y: 0, width: b - a, height: bounds.height).fill() }
    }
    NSGraphicsContext.saveGraphicsState()
    NSBezierPath(rect: NSRect(x: gutter, y: 0, width: contentWidth + 16, height: bounds.height))
      .addClip()
    let raw = windowRange.span / max(2, Double(contentWidth / 90))
    let power = pow(10, floor(log10(max(1e-12, raw))))
    let scale = raw / power
    let step = (scale > 5 ? 10 : scale > 2 ? 5 : scale > 1 ? 2 : 1) * power
    let estimatedFirst = ceil(windowRange.lower / step) * step
    let first = estimatedFirst.isFinite ? estimatedFirst : windowRange.lower
    let estimatedCount = ceil(windowRange.span / step) + 2
    let tickCount = estimatedCount.isFinite ? Int(max(0, min(1000, estimatedCount))) : 0
    for i in 0..<tickCount {
      let t = first + Double(i) * step
      guard t.isFinite else { break }
      let px = x(t)
      if px > bounds.maxX { break }
      NSColor.separatorColor.withAlphaComponent(0.5).setFill()
      NSRect(x: px, y: 24, width: 1, height: bounds.height - 24).fill()
      text(L10n.number(t, digits: 6), NSPoint(x: px + 3, y: 8))
    }
    if let a = times.first, let b = times.last {
      NSColor.systemTeal.withAlphaComponent(0.20).setFill()
      NSRect(
        x: max(gutter, x(a)), y: 39, width: max(0, min(bounds.maxX, x(b)) - max(gutter, x(a))),
        height: 14
      ).fill()
      // Subpixel ticks coalesce visually; no source samples are discarded.
      var previous: CGFloat = -.infinity
      NSColor.systemTeal.withAlphaComponent(0.8).setFill()
      for t in times {
        let px = x(t)
        if px >= gutter && px <= bounds.width && px - previous >= 2 {
          NSRect(x: px, y: 40, width: 1, height: 12).fill()
          previous = px
        }
      }
    }
    for (index, t) in keyTimes.enumerated() {
      let px = x(movingKey == index ? ghostTime ?? t : t)
      guard px >= gutter - 5, px <= bounds.maxX + 5 else { continue }
      let path = NSBezierPath()
      path.move(to: NSPoint(x: px, y: 70))
      path.line(to: NSPoint(x: px + 6, y: 76))
      path.line(to: NSPoint(x: px, y: 82))
      path.line(to: NSPoint(x: px - 6, y: 76))
      path.close()
      (selectedKey == index ? NSColor.systemOrange : NSColor.controlAccentColor).setFill()
      path.fill()
    }
    let px = x(currentTime)
    NSColor.controlAccentColor.setFill()
    NSRect(x: px - 0.7, y: 23, width: 1.4, height: bounds.height - 23).fill()
    let triangle = NSBezierPath()
    triangle.move(to: NSPoint(x: px - 5, y: 20))
    triangle.line(to: NSPoint(x: px + 5, y: 20))
    triangle.line(to: NSPoint(x: px, y: 27))
    triangle.close()
    triangle.fill()
    NSGraphicsContext.restoreGraphicsState()
    setAccessibilityValue(L10n.number(currentTime, digits: 6))
  }
  override func mouseDown(with event: NSEvent) {
    window?.makeFirstResponder(self)
    cancelKeyGesture()
    gestureCancelled = false
    let p = convert(event.locationInWindow, from: nil)
    downX = p.x
    guard p.x >= gutter else { return }
    if p.y > 61,
      let index = keyTimes.indices.min(by: {
        abs(x(keyTimes[$0]) - p.x) < abs(x(keyTimes[$1]) - p.x)
      }), abs(x(keyTimes[index]) - p.x) < 9
    {
      movingKey = index
      selectedKey = index
      ghostTime = keyTimes[index]
      onKeySelect?(index)
    } else {
      onSeek?(time(p.x, snapEnabled: !event.modifierFlags.contains(.option)))
    }
  }
  override func mouseDragged(with event: NSEvent) {
    guard !gestureCancelled else { return }
    let p = convert(event.locationInWindow, from: nil)
    if movingKey != nil {
      ghostTime = time(p.x, snapEnabled: !event.modifierFlags.contains(.option))
      needsDisplay = true
    } else if p.x >= gutter {
      onSeek?(time(p.x, snapEnabled: !event.modifierFlags.contains(.option)))
    }
  }
  override func mouseUp(with event: NSEvent) {
    if !gestureCancelled, let key = movingKey, let time = ghostTime,
      abs(convert(event.locationInWindow, from: nil).x - downX) > 3
    {
      onKeyMove?(key, time)
    }
    movingKey = nil
    ghostTime = nil
    needsDisplay = true
  }
  private func cancelKeyGesture() {
    movingKey = nil
    ghostTime = nil
    gestureCancelled = true
    needsDisplay = true
  }
  override func cancelOperation(_ sender: Any?) { cancelKeyGesture() }
  override func resignFirstResponder() -> Bool {
    cancelKeyGesture()
    return super.resignFirstResponder()
  }
  override func otherMouseDown(with event: NSEvent) {
    lastX = convert(event.locationInWindow, from: nil).x
  }
  override func otherMouseDragged(with event: NSEvent) {
    let p = convert(event.locationInWindow, from: nil)
    if !windowRange.pan(Double((lastX - p.x) / contentWidth)), windowRange.rangeRejected {
      onRangeError?()
    }
    lastX = p.x
    userRange = true
    needsDisplay = true
  }
  override func scrollWheel(with event: NSEvent) {
    if event.modifierFlags.contains(.shift)
      || abs(event.scrollingDeltaX) > abs(event.scrollingDeltaY)
    {
      windowRange.pan(
        Double(
          (event.scrollingDeltaX != 0 ? event.scrollingDeltaX : event.scrollingDeltaY)
            / contentWidth))
    } else {
      let p = convert(event.locationInWindow, from: nil)
      windowRange.zoom(
        exp(Double(event.scrollingDeltaY) * 0.015), anchor: Double((p.x - gutter) / contentWidth))
    }
    if windowRange.rangeRejected { onRangeError?() }
    userRange = true
    needsDisplay = true
  }
  override func magnify(with event: NSEvent) {
    windowRange.zoom(exp(-Double(event.magnification)), anchor: 0.5)
    if windowRange.rangeRejected { onRangeError?() }
    userRange = true
    needsDisplay = true
  }
  override func keyDown(with event: NSEvent) {
    switch event.keyCode {
    case 115: fit()
    case 123: if let t = times.last(where: { $0 < currentTime }) { onSeek?(t) }
    case 124: if let t = times.first(where: { $0 > currentTime }) { onSeek?(t) }
    default: super.keyDown(with: event)
    }
  }
}
