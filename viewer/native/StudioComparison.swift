import AppKit
import CoreFoundation

struct WorkspaceComparisonEntry {
  let observationID: String
  let row: Int
  let cameraID: String
  let cameraView: Int
  let time: Double
  let label: String
  var renderViewID: String = ""
  var renderViewLabel: String = ""
}

/// Two color-managed image panes share one 2D inspection transform. Camera
/// orbit is deliberately unavailable here: a changed ray is no longer GT-matched.
final class ComparisonImagePane: NSView {
  var image: CGImage? { didSet { needsDisplay = true } }
  var zoom: CGFloat = 1 { didSet { needsDisplay = true } }
  var pan = NSPoint.zero { didSet { needsDisplay = true } }
  var transform: ((CGFloat, NSPoint) -> Void)?
  private var dragPoint = NSPoint.zero
  override var isFlipped: Bool { true }
  override var acceptsFirstResponder: Bool { true }
  override func draw(_ dirtyRect: NSRect) {
    // AppKit may draw a non-layer-backed view without clipping it to its own
    // bounds. Linked pan/zoom must never paint over the surrounding controls.
    NSGraphicsContext.saveGraphicsState()
    defer { NSGraphicsContext.restoreGraphicsState() }
    NSBezierPath(rect: bounds).addClip()
    NSColor(calibratedWhite:0.055,alpha:1).setFill(); bounds.fill()
    guard let image else { return }
    let fit = min(bounds.width/CGFloat(image.width),bounds.height/CGFloat(image.height))
    let width = CGFloat(image.width)*fit*zoom, height = CGFloat(image.height)*fit*zoom
    let rect = NSRect(x:(bounds.width-width)/2+pan.x*bounds.width,y:(bounds.height-height)/2+pan.y*bounds.height,width:width,height:height)
    NSGraphicsContext.current?.imageInterpolation = zoom >= 1.5 ? .none : .high
    NSImage(cgImage:image,size:NSSize(width:image.width,height:image.height)).draw(in:rect,from:.zero,operation:.copy,fraction:1,respectFlipped:true,hints:nil)
  }
  override func mouseDown(with event: NSEvent) { window?.makeFirstResponder(self); dragPoint = convert(event.locationInWindow,from:nil) }
  override func mouseDragged(with event: NSEvent) {
    let point = convert(event.locationInWindow,from:nil)
    transform?(zoom,NSPoint(x:pan.x+(point.x-dragPoint.x)/max(1,bounds.width),y:pan.y+(point.y-dragPoint.y)/max(1,bounds.height)))
    dragPoint = point
  }
  override func scrollWheel(with event: NSEvent) {
    if event.modifierFlags.contains(.option) {
      changeZoom(exp(event.scrollingDeltaY*0.025),at:convert(event.locationInWindow,from:nil))
    } else {
      transform?(zoom,NSPoint(x:pan.x+event.scrollingDeltaX/max(1,bounds.width),y:pan.y-event.scrollingDeltaY/max(1,bounds.height)))
    }
  }
  override func magnify(with event: NSEvent) { changeZoom(1+event.magnification,at:convert(event.locationInWindow,from:nil)) }
  private func changeZoom(_ factor: CGFloat, at point: NSPoint) {
    let next = min(32,max(0.25,zoom*factor)), ratio = next/zoom
    let anchor = NSPoint(x:point.x/max(1,bounds.width)-0.5,y:point.y/max(1,bounds.height)-0.5)
    transform?(next,NSPoint(x:anchor.x-(anchor.x-pan.x)*ratio,y:anchor.y-(anchor.y-pan.y)*ratio))
  }
}

final class StudioComparisonView: NSView {
  let selection = NSPopUpButton(), renderViewSelection = NSPopUpButton()
  private var allEntries = [WorkspaceComparisonEntry]()
  private var renderViewIDs = [String]()
  private var activeEntry: Int?
  let rendered = ComparisonImagePane(), reference = ComparisonImagePane()
  let status = NSTextField(wrappingLabelWithString:"")
  private let detail = NSTextField(labelWithString:"")
  private let referenceTitle = NSTextField(labelWithString:"")
  var onSelect: ((Int)->Void)?
  var onSelectPreviewView: ((String)->Void)?
  var previewOnly = false
  var onClose: (()->Void)?
  override init(frame frameRect: NSRect) {
    super.init(frame:frameRect)
    wantsLayer = true
    refreshBackgroundAppearance()
    renderViewSelection.target = self;renderViewSelection.action = #selector(selectRenderView(_:));renderViewSelection.controlSize = .small
    renderViewSelection.setAccessibilityLabel(L10n.tr("render.viewSelector","Render view and source profile"))
    renderViewSelection.setAccessibilityIdentifier("comparison.renderView")
    selection.target = self; selection.action = #selector(selectEntry(_:)); selection.controlSize = .small
    selection.setAccessibilityLabel(L10n.tr("compare.observation","Camera observation"))
    let fit = NSButton(title:L10n.tr("compare.fit","Fit both"),target:self,action:#selector(fitBoth(_:)))
    let actual = NSButton(title:"1:1",target:self,action:#selector(actualPixels(_:)))
    actual.toolTip = L10n.tr("compare.actualPixels","One image pixel per display backing pixel")
    let close = NSButton(title:L10n.tr("compare.close","Return to 3D"),target:self,action:#selector(closeComparison(_:)))
    [fit,actual,close].forEach {$0.bezelStyle = .rounded; $0.controlSize = .small}
    let controls = NSStackView(views:[selection,fit,actual,close]); controls.orientation = .horizontal; controls.spacing = 6
    let leftTitle = NSTextField(labelWithString:L10n.tr("compare.render","Render"))
    referenceTitle.stringValue = L10n.tr("compare.reference","Ground truth / reference")
    [leftTitle,referenceTitle].forEach {$0.font = .systemFont(ofSize:12,weight:.semibold)}
    func pane(_ image: ComparisonImagePane,_ title: NSTextField)->NSStackView {
      let box = NSStackView(views:[title,image]); box.orientation = .vertical; box.spacing = 7; box.alignment = .leading
      image.translatesAutoresizingMaskIntoConstraints = false
      image.widthAnchor.constraint(equalTo:box.widthAnchor).isActive = true
      image.heightAnchor.constraint(greaterThanOrEqualToConstant:120).isActive = true
      return box
    }
    rendered.setAccessibilityIdentifier("comparison.render"); reference.setAccessibilityIdentifier("comparison.reference")
    let panes = NSStackView(views:[pane(rendered,leftTitle),pane(reference,referenceTitle)])
    panes.orientation = .horizontal; panes.distribution = .fillEqually; panes.spacing = 10
    panes.arrangedSubviews.forEach {$0.heightAnchor.constraint(equalTo:panes.heightAnchor).isActive = true}
    status.font = .systemFont(ofSize:11); status.textColor = .secondaryLabelColor; status.isSelectable = true
    status.maximumNumberOfLines = 3
    detail.font = .monospacedDigitSystemFont(ofSize:10,weight:.regular); detail.textColor = .secondaryLabelColor
    let hint = NSTextField(labelWithString:L10n.tr("compare.navigation","Linked inspection: drag / scroll to pan · pinch or Option-scroll to zoom · camera locked"))
    hint.font = .systemFont(ofSize:10); hint.textColor = .secondaryLabelColor; hint.lineBreakMode = .byTruncatingTail
    [renderViewSelection,controls,status,panes,detail,hint].forEach {$0.translatesAutoresizingMaskIntoConstraints = false; addSubview($0)}
    NSLayoutConstraint.activate([
      renderViewSelection.leadingAnchor.constraint(equalTo:leadingAnchor,constant:10),renderViewSelection.trailingAnchor.constraint(equalTo:trailingAnchor,constant:-10),renderViewSelection.topAnchor.constraint(equalTo:topAnchor,constant:8),
      controls.leadingAnchor.constraint(equalTo:leadingAnchor,constant:10),controls.trailingAnchor.constraint(equalTo:trailingAnchor,constant:-10),controls.topAnchor.constraint(equalTo:renderViewSelection.bottomAnchor,constant:6),
      selection.widthAnchor.constraint(greaterThanOrEqualToConstant:150),
      status.leadingAnchor.constraint(equalTo:controls.leadingAnchor),status.trailingAnchor.constraint(equalTo:controls.trailingAnchor),status.topAnchor.constraint(equalTo:controls.bottomAnchor,constant:8),
      panes.leadingAnchor.constraint(equalTo:controls.leadingAnchor),panes.trailingAnchor.constraint(equalTo:controls.trailingAnchor),panes.topAnchor.constraint(equalTo:status.bottomAnchor,constant:8),
      detail.leadingAnchor.constraint(equalTo:controls.leadingAnchor),detail.trailingAnchor.constraint(equalTo:controls.trailingAnchor),detail.topAnchor.constraint(equalTo:panes.bottomAnchor,constant:8),
      hint.leadingAnchor.constraint(equalTo:controls.leadingAnchor),hint.trailingAnchor.constraint(equalTo:controls.trailingAnchor),hint.topAnchor.constraint(equalTo:detail.bottomAnchor,constant:5),hint.bottomAnchor.constraint(equalTo:bottomAnchor,constant:-8)
    ])
    let update: (CGFloat,NSPoint)->Void = {[weak self] zoom,pan in
      self?.rendered.zoom = zoom; self?.reference.zoom = zoom; self?.rendered.pan = pan; self?.reference.pan = pan
    }
    rendered.transform = update; reference.transform = update
  }
  required init?(coder:NSCoder) { fatalError() }
  override func viewDidChangeEffectiveAppearance() {
    super.viewDidChangeEffectiveAppearance()
    refreshBackgroundAppearance()
  }
  private func refreshBackgroundAppearance() {
    // A CGColor is resolved once; resolve the semantic AppKit color again when
    // this view inherits its window's appearance or the appearance changes.
    effectiveAppearance.performAsCurrentDrawingAppearance {
      layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
    }
  }
  func entries(_ items:[WorkspaceComparisonEntry], selected:Int?) {
    previewOnly = false;reference.superview?.isHidden = false;selection.isHidden = false
    allEntries = items;activeEntry = selected
    renderViewIDs = [];renderViewSelection.removeAllItems()
    for item in items where !renderViewIDs.contains(item.renderViewID) {
      renderViewIDs.append(item.renderViewID);renderViewSelection.addItem(withTitle:item.renderViewLabel.isEmpty ? item.renderViewID:item.renderViewLabel)
    }
    let entry = selected.flatMap{items.indices.contains($0) ? items[$0]:nil} ?? items.first
    if let id = entry?.renderViewID,let index = renderViewIDs.firstIndex(of:id) {renderViewSelection.selectItem(at:index)}
    selection.removeAllItems()
    for (index,item) in items.enumerated() where item.renderViewID == entry?.renderViewID {
      selection.addItem(withTitle:item.label);selection.lastItem?.tag = index
      if index == selected {selection.selectItem(at:selection.numberOfItems-1)}
    }
  }
  func exportImage() throws -> CGImage {
    if previewOnly {
      guard let image = rendered.image else {throw SceneError.invalid(L10n.tr("render.previewExportPending","Wait for the selected render-view preview before exporting."))}
      return image
    }
    return try pairedImage()
  }
  func pairedImage() throws -> CGImage {
    guard let left = rendered.image, let right = reference.image,
      left.width == right.width, left.height == right.height,
      let color = left.colorSpace,
      let context = CGContext(data:nil,width:left.width*2,height:left.height,bitsPerComponent:8,bytesPerRow:left.width*8,space:color,bitmapInfo:CGImageAlphaInfo.noneSkipLast.rawValue)
    else {throw SceneError.invalid(L10n.tr("compare.snapshotPending", "Wait for a matched comparison before exporting its image."))}
    context.draw(left,in:CGRect(x:0,y:0,width:left.width,height:left.height))
    context.draw(right,in:CGRect(x:left.width,y:0,width:right.width,height:right.height))
    guard let result = context.makeImage() else {throw SceneError.invalid("Comparison image allocation failed.")}
    return result
  }
  func clear(_ message:String) {rendered.image = nil;reference.image = nil;status.stringValue = message;detail.stringValue = ""}
  func show(render:CGImage,gt:CGImage,color:String,role:String) {
    rendered.image = render; reference.image = gt
    referenceTitle.stringValue = L10n.tr("role."+role,role)
    detail.stringValue = "\(render.width) × \(render.height) · \(color)"
  }
  @objc private func selectEntry(_ sender:Any?) {if let tag = selection.selectedItem?.tag {onSelect?(tag)}}
  func previewViews(_ views:[(String,String)],selected:String) {
    previewOnly = true;rendered.superview?.isHidden = false;reference.superview?.isHidden = true;selection.isHidden = true
    renderViewIDs = views.map{$0.0};renderViewSelection.removeAllItems();renderViewSelection.addItems(withTitles:views.map{$0.1})
    if let index = renderViewIDs.firstIndex(of:selected) {renderViewSelection.selectItem(at:index)}
  }
  func showPreview(_ image:CGImage,color:String) {
    rendered.image = image;reference.image = nil
    detail.stringValue = "\(image.width) × \(image.height) · \(color)"
    status.stringValue = L10n.tr("render.previewReady","Explicit render-view preview · no reference image or metrics")
  }
  @objc private func selectRenderView(_ sender:Any?) {
    guard renderViewIDs.indices.contains(renderViewSelection.indexOfSelectedItem) else {return}
    let id = renderViewIDs[renderViewSelection.indexOfSelectedItem]
    if previewOnly {onSelectPreviewView?(id);return}
    let time = activeEntry.flatMap{allEntries.indices.contains($0) ? allEntries[$0].time:nil}
    if let index = allEntries.firstIndex(where:{$0.renderViewID == id && $0.time == time}) ?? allEntries.firstIndex(where:{$0.renderViewID == id}) {onSelect?(index)}
  }
  @objc private func closeComparison(_ sender:Any?) {onClose?()}
  func resetInspectionTransform() { rendered.transform?(1,.zero) }
  @objc private func fitBoth(_ sender:Any?) {resetInspectionTransform()}
  @objc private func actualPixels(_ sender:Any?) {
    guard let image = rendered.image else {return}
    let fit = min(rendered.bounds.width/CGFloat(image.width),rendered.bounds.height/CGFloat(image.height))
    rendered.transform?(1/max(0.001,fit*(window?.backingScaleFactor ?? 1)),.zero)
  }
}

extension WorkspaceController {
  /// A successful scene replacement starts a fresh image inspection session.
  /// Frame/time updates within that scene preserve the linked pan/zoom.
  func resetComparisonDocument() {
    comparisonMessage = ""; comparisonSummary = ""; comparisonMetrics = nil
    comparisonDetailsExpanded = false
    comparisonView.clear("")
    comparisonView.resetInspectionTransform()
  }
  func installComparison() {
    comparisonView.isHidden = true
    comparisonView.translatesAutoresizingMaskIntoConstraints = false
    centerPane.addSubview(comparisonView)
    NSLayoutConstraint.activate([
      comparisonView.leadingAnchor.constraint(equalTo:viewport.leadingAnchor),comparisonView.trailingAnchor.constraint(equalTo:viewport.trailingAnchor),
      comparisonView.topAnchor.constraint(equalTo:viewport.topAnchor),comparisonView.bottomAnchor.constraint(equalTo:viewport.bottomAnchor)
    ])
    comparisonView.onSelect = {[weak self] index in self?.actions.onSelectComparison?(index)}
    comparisonView.onSelectPreviewView = {[weak self] id in self?.actions.onActivateRenderView?(id)}
    comparisonView.onClose = {[weak self] in self?.actions.onToggleComparison?(false)}
  }
  func updateComparisons(_ entries:[WorkspaceComparisonEntry]) {
    comparisonEntries = entries
    compareButton.isEnabled = !entries.isEmpty
    compareButton.toolTip = entries.isEmpty ? L10n.tr("compare.absent","No calibrated RGB observations in this document. Normal 3D inspection remains available.") : L10n.tr("compare.open","Compare render with camera observation")
    comparisonView.entries(entries,selected:comparisonSelection)
  }
  func setComparisonActive(_ value:Bool,selected:Int? = nil) {
    comparisonActive = value; comparisonSelection = selected
    refreshPerformanceLabel()
    comparisonView.isHidden = !value; viewport.isHidden = value
    compareButton.state = value ? .on:.off
    navigationControls.forEach {$0.isEnabled = !value}
    window?.makeFirstResponder(value ? comparisonView.rendered : viewport)
    comparisonView.entries(comparisonEntries,selected:selected)
    if value {inspectorTabs.selectedSegment = 0}
    rebuildInspector()
  }
  func comparisonPending(_ message:String) {
    guard comparisonMessage != message || comparisonMetrics != nil else {return}
    comparisonMessage = message; comparisonSummary = message
    comparisonMetrics = nil; comparisonView.clear(message)
    if comparisonActive {rebuildInspector()}
  }
  func comparisonFinished(render:CGImage,gt:CGImage,color:String,role:String,metrics:RGBQualityMetrics,context:String,summary:String) {
    comparisonMessage = context; comparisonSummary = summary; comparisonMetrics = metrics
    comparisonView.show(render:render,gt:gt,color:color,role:role)
    comparisonView.status.stringValue = L10n.tr("compare.ready","Matched camera capture · full-resolution metrics ready")
    if comparisonActive {rebuildInspector()}
  }
  func buildComparisonInspector() {
    if comparisonView.previewOnly {
      section(L10n.tr("render.previewAction","Preview render view"),[inspectorNote(comparisonSummary)])
      buildComparisonDetails(includeMetricPolicy:false)
      return
    }
    section(L10n.tr("compare.title","Image comparison"),[
      inspectorNote(comparisonSummary),
      property("comparison.psnr","PSNR",value:comparisonMetrics.flatMap {$0.psnr}.map {$0.isInfinite ? "∞ dB" : L10n.number($0,digits:4)+" dB"} ?? "—"),
      property("comparison.ssim","SSIM",value:comparisonMetrics?.ssim.map{L10n.number($0,digits:6)} ?? "—"),
      property("comparison.mse","MSE",value:comparisonMetrics.flatMap{$0.mse}.map{String(format:"%.8g",$0)} ?? "—"),
      property("comparison.mae","MAE",value:comparisonMetrics.flatMap{$0.mae}.map{String(format:"%.8g",$0)} ?? "—"),
      property("comparison.pixels",L10n.tr("compare.pixels","Valid pixels"),value:comparisonMetrics.map{String($0.pixelCount)} ?? "—"),
      property("comparison.windows",L10n.tr("compare.windows","SSIM windows"),value:comparisonMetrics.map{String($0.ssimWindowCount)} ?? "—"),
    ])
    buildComparisonDetails(includeMetricPolicy:true)
  }
  private func buildComparisonDetails(includeMetricPolicy:Bool) {
    let title = L10n.tr("compare.details", "Rendering and metric details")
    let toggle = button(title,#selector(toggleComparisonDetails(_:)),
      symbol:comparisonDetailsExpanded ? "chevron.down":"chevron.right")
    toggle.isBordered = false
    toggle.setAccessibilityIdentifier("comparison.details")
    toggle.setAccessibilityValue(comparisonDetailsExpanded ? 1:0)
    inspectorStack.addArrangedSubview(toggle)
    guard comparisonDetailsExpanded else {return}
    var details:[NSView] = [inspectorNote(comparisonMessage)]
    if includeMetricPolicy {
      details.append(inspectorNote(L10n.tr("compare.policy","RGB [0,1] in the declared color space; no gamma conversion. Float32 render, optional declared camera RGB affine, then final clipping. PSNR range 1. SSIM: 11×11 Gaussian σ=1.5, population covariance, valid interior; mask windows require all 121 pixels. SSIM is unavailable below 11×11 or without valid windows.")))
      details.append(inspectorNote(L10n.tr("compare.renderPolicy","Explicit render-view background, center clipping and appearance; neutral viewport exposure, scale and opacity. No grid, scaffold, selection or camera overlays. Effective profile differences remain visible; metrics do not establish source-renderer parity.")))
    }
    section(L10n.tr("compare.contract", "Evaluation contract"),details)
  }
  @objc func toggleComparisonDetails(_ sender:Any?) {
    comparisonDetailsExpanded.toggle()
    rebuildInspector()
  }
  @objc func toggleComparison(_ sender:Any?) {actions.onToggleComparison?(!comparisonActive)}
  @objc func previewSelectedRenderView(_ sender:Any?) {if let id=selection.objectID{actions.onActivateRenderView?(id)}}
  @objc func compareSelectedObservation(_ sender:Any?) {if let id=selection.objectID{actions.onCompareObservation?(id)}}
}
