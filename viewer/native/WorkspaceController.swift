import AppKit
import MetalKit
import simd

final class SceneOutlineNode {
  let id: String
  let title: String
  let symbol: String
  var object: WorkspaceObjectInfo?
  var cameraIndex: Int?
  var children: [SceneOutlineNode]
  init(
    id: String, title: String, symbol: String, object: WorkspaceObjectInfo? = nil,
    camera: Int? = nil, children: [SceneOutlineNode] = []
  ) {
    self.id = id
    self.title = title
    self.symbol = symbol
    self.object = object
    cameraIndex = camera
    self.children = children
  }
}
final class FlippedPanelView: NSView { override var isFlipped: Bool { true } }

/// Capture the selection gesture before AppKit synchronously notifies delegates.
/// The document never has to read a later event to interpret a selection change.
final class StudioOutlineView: NSOutlineView {
  private(set) var selectionOperation: StudioSelectionOperation = .replace
  private(set) var selectionGestureRow: Int?
  override func mouseDown(with event: NSEvent) {
    selectionOperation = event.modifierFlags.contains(.command) ? .toggle : .replace
    selectionGestureRow = row(at: convert(event.locationInWindow, from: nil))
    defer { selectionOperation = .replace; selectionGestureRow = nil }
    super.mouseDown(with: event)
  }
  override func keyDown(with event: NSEvent) {
    selectionOperation = event.modifierFlags.contains(.command) ? .toggle : .replace
    defer { selectionOperation = .replace }
    super.keyDown(with: event)
  }
}

/// AppKit presentation only. Data ownership and authoring commands live in the
/// document controller; observing a camera never changes the selection model.
final class WorkspaceController: NSWindowController, NSToolbarDelegate, NSSplitViewDelegate,
  NSOutlineViewDataSource, NSOutlineViewDelegate, NSTextFieldDelegate, NSSearchFieldDelegate,
  NSWindowDelegate
{
  let viewport = WorkspaceViewport(frame: .zero, device: MTLCreateSystemDefaultDevice())
  var actions = WorkspaceActions()
  let comparisonView = StudioComparisonView(frame: .zero)
  let compareButton = NSButton()
  var comparisonEntries = [WorkspaceComparisonEntry]()
  var comparisonSelection: Int?
  var comparisonActive = false
  var comparisonMessage = ""
  var comparisonSummary = ""
  var comparisonDetailsExpanded = false
  var comparisonMetrics: RGBQualityMetrics?
  var navigationControls = [NSControl]()
  var scene: WorkspaceSceneInfo?
  var isPlaying = false
  var currentFrame = 0, currentTime = 0.0
  var selectionSet = StudioSelectionSet()
  var selection: StudioSelection { selectionSet.primary }
  var elementInfo: WorkspaceSelectionInfo?
  var cameraInspection: WorkspaceCameraInspection?
  var objectMetadata = ""
  var renderOptions = WorkspaceRenderOptions()
  var orbitCenter = SIMD3<Float>.zero
  let orbitStylePopup = NSPopUpButton()
  var trackballOrbit = false
  let pivotButton = NSButton(checkboxWithTitle: "", target: nil, action: nil)
  var lastPerformance: WorkspacePerformance?
  let split = NSSplitView(), leftPane = NSView(), rightPane = NSView(),
    centerPane = StudioDropZone()
  let outline = StudioOutlineView(), searchField = NSSearchField()
  var nodes = [SceneOutlineNode](), rebuildingTree = false
  let timeline = StudioTimelineView(), timelineContainer = NSView()
  let sceneTitle = NSTextField(labelWithString: "4DGS Studio")
  let objectCount = NSTextField(labelWithString: "")
  let viewPopup = NSPopUpButton(), toolPicker = NSSegmentedControl(), upPopup = NSPopUpButton()
  let coordinateCaption = NSTextField(labelWithString: "")
  let statusLabel = NSTextField(labelWithString: ""),
    performanceLabel = NSTextField(labelWithString: "")
  let frameLabel = NSTextField(labelWithString: ""), timeField = NSTextField(string: "0")
  let playButton = NSButton(),
    loopButton = NSButton(checkboxWithTitle: "", target: nil, action: nil)
  let speedPopup = NSPopUpButton(), previewRateField = NSTextField(string: "1"),
    previewRateLabel = NSTextField(labelWithString: "")
  let rangeStart = NSTextField(string: "0"), rangeEnd = NSTextField(string: "1")
  let rangeEnabled = NSButton(checkboxWithTitle: "", target: nil, action: nil)
  let snapButton = NSButton(checkboxWithTitle: "", target: nil, action: nil)
  let inspectorTabs = NSSegmentedControl(), inspectorScroll = NSScrollView(),
    inspectorBody = FlippedPanelView()
  let inspectorStack = NSStackView(), selectionTitle = NSTextField(labelWithString: ""),
    selectionSubtitle = NSTextField(wrappingLabelWithString: "")
  let infoLine = NSTextField(wrappingLabelWithString: "")
  var inspectorValues = [String: NSTextField]()
  var inspectorScrollPosition = NSPoint.zero
  var inspectorRestoringScroll = false
  var lastInspectorSelection: StudioSelectionSet?
  var lastInspectorTab: Int?
  var lastInspectorComparisonActive: Bool?
  let idField = NSTextField(string: ""), cameraNameField = NSTextField(string: ""),
    cameraFOVField = NSTextField(string: "")
  let cameraKeys = NSPopUpButton(), keyTimeField = NSTextField(string: "")
  let positionFields = (0..<3).map { _ in NSTextField(string: "0") }
  let rotationFields = (0..<4).map { _ in NSTextField(string: "0") }
  var editingCameraName: String?, editingCameraTimes = [Double](), editingFOV: Double?
  var canUndoCamera = false, canRedoCamera = false
  let focusButton = NSButton(), recordButton = NSButton()
  let resolutionPopup = NSPopUpButton(), backgroundPopup = NSPopUpButton()
  let gridButton = NSButton(), camerasButton = NSButton(), trajectoryButton = NSButton(),
    trailButton = NSButton()
  var trajectoryOptions = TrajectoryDisplayOptions()
  let pathOpacityField = NSTextField(string: "18"), trailFramesField = NSTextField(string: "8"),
    trajectoryDensityField = NSTextField(string: "100")
  let pathOpacityStepper = NSStepper(), trailFramesStepper = NSStepper(),
    trajectoryDensityStepper = NSStepper()
  let nearClipField = NSTextField(string: "0.01")
  let emptyPanel = NSStackView(), loadingPanel = NSVisualEffectView(),
    loadingLabel = NSTextField(wrappingLabelWithString: "")
  let loadingProgress = NSProgressIndicator(), loadingCancel = NSButton()
  var toolbarPlay: NSToolbarItem?
  var lastSceneURL: URL?
  var descriptorPanel: NSPanel?
  var cameraUndoManager: UndoManager?
  func windowWillReturnUndoManager(_ window: NSWindow) -> UndoManager? { cameraUndoManager }

  init() {
    let window = NSWindow(
      contentRect: NSRect(x: 0, y: 0, width: 1480, height: 960),
      styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false
    )
    window.title = AppIdentity().name
    window.minSize = NSSize(width: 1160, height: 760)
    window.appearance = NSAppearance(named: .darkAqua)
    window.toolbarStyle = .unifiedCompact
    window.isReleasedWhenClosed = false
    super.init(window: window)
    window.delegate = self
    buildWorkspace()
    wireViewport()
    let toolbar = NSToolbar(identifier: "4DGSStudioWorkspace")
    toolbar.delegate = self
    toolbar.displayMode = .iconOnly
    toolbar.allowsUserCustomization = true
    window.toolbar = toolbar
    window.setFrameAutosaveName("4DGSStudioProfessionalWorkspace")
    window.center()
  }
  required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
  func windowShouldClose(_ sender: NSWindow) -> Bool { actions.onCloseRequested?() ?? true }
  func label(
    _ text: String, size: CGFloat = 11, weight: NSFont.Weight = .regular, secondary: Bool = false
  ) -> NSTextField {
    let view = NSTextField(labelWithString: text)
    view.font = .systemFont(ofSize: size, weight: weight)
    if secondary { view.textColor = .secondaryLabelColor }
    return view
  }
  func stack(_ views: [NSView], vertical: Bool = true, spacing: CGFloat = 8) -> NSStackView {
    let s = NSStackView(views: views)
    s.orientation = vertical ? .vertical : .horizontal
    s.alignment = vertical ? .leading : .centerY
    s.spacing = spacing
    return s
  }
  func pin(_ view: NSView, in parent: NSView, inset: CGFloat = 0) {
    view.translatesAutoresizingMaskIntoConstraints = false
    parent.addSubview(view)
    NSLayoutConstraint.activate([
      view.leadingAnchor.constraint(equalTo: parent.leadingAnchor, constant: inset),
      view.trailingAnchor.constraint(equalTo: parent.trailingAnchor, constant: -inset),
      view.topAnchor.constraint(equalTo: parent.topAnchor, constant: inset),
      view.bottomAnchor.constraint(equalTo: parent.bottomAnchor, constant: -inset),
    ])
  }
  func separator() -> NSBox {
    let box = NSBox()
    box.boxType = .separator
    return box
  }
  func button(_ title: String, _ action: Selector, symbol: String? = nil) -> NSButton {
    let b = NSButton(title: title, target: self, action: action)
    b.bezelStyle = .rounded
    b.controlSize = .small
    if let symbol {
      b.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
      b.imagePosition = .imageLeading
    }
    return b
  }
  func toolButton(_ symbol: String, help: String, action: Selector) -> NSButton {
    let b = NSButton(
      image: NSImage(systemSymbolName: symbol, accessibilityDescription: help) ?? NSImage(),
      target: self, action: action)
    b.bezelStyle = .texturedRounded
    b.controlSize = .small
    b.toolTip = help
    b.setAccessibilityLabel(help)
    return b
  }
  func configureField(_ field: NSTextField, label: String, width: CGFloat? = nil) {
    field.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
    field.controlSize = .small
    // Commit with Return or the adjacent explicit action. Losing focus after a
    // committed edit must not record the same camera key a second time.
    field.cell?.sendsActionOnEndEditing = false
    field.setAccessibilityLabel(label)
    if let width { field.widthAnchor.constraint(equalToConstant: width).isActive = true }
  }
  func buildWorkspace() {
    guard let root = window?.contentView else { return }
    split.isVertical = true
    split.dividerStyle = .thin
    split.delegate = self
    [leftPane, centerPane, rightPane].forEach { split.addArrangedSubview($0) }
    split.setHoldingPriority(.defaultHigh, forSubviewAt: 0)
    split.setHoldingPriority(.defaultLow, forSubviewAt: 1)
    split.setHoldingPriority(.defaultHigh, forSubviewAt: 2)
    let status = makeStatus()
    makeTimeline()
    [split, timelineContainer, status].forEach {
      $0.translatesAutoresizingMaskIntoConstraints = false
      root.addSubview($0)
    }
    NSLayoutConstraint.activate([
      split.leadingAnchor.constraint(equalTo: root.leadingAnchor),
      split.trailingAnchor.constraint(equalTo: root.trailingAnchor),
      split.topAnchor.constraint(equalTo: root.topAnchor),
      split.bottomAnchor.constraint(equalTo: timelineContainer.topAnchor),
      timelineContainer.leadingAnchor.constraint(equalTo: root.leadingAnchor),
      timelineContainer.trailingAnchor.constraint(equalTo: root.trailingAnchor),
      timelineContainer.heightAnchor.constraint(equalToConstant: 170),
      timelineContainer.bottomAnchor.constraint(equalTo: status.topAnchor),
      status.leadingAnchor.constraint(equalTo: root.leadingAnchor),
      status.trailingAnchor.constraint(equalTo: root.trailingAnchor),
      status.bottomAnchor.constraint(equalTo: root.bottomAnchor),
      status.heightAnchor.constraint(equalToConstant: 27),
    ])
    makeScenePane()
    makeViewport()
    installComparison()
    makeInspector()
    DispatchQueue.main.async { [weak self] in
      guard let self else { return }
      self.split.setPosition(248, ofDividerAt: 0)
      self.split.setPosition(self.split.bounds.width - 330, ofDividerAt: 1)
    }
  }
  func makeScenePane() {
    sceneTitle.font = .systemFont(ofSize: 13, weight: .semibold)
    sceneTitle.lineBreakMode = .byTruncatingMiddle
    objectCount.font = .systemFont(ofSize: 10)
    objectCount.textColor = .secondaryLabelColor
    searchField.placeholderString = L10n.tr("outliner.search", "Find objects, types, encodings…")
    searchField.delegate = self
    searchField.setAccessibilityLabel(L10n.tr("outliner.searchLabel", "Search scene hierarchy"))
    let heading = stack(
      [
        label(
          L10n.tr("outliner.title", "Scene Outliner"), size: 10, weight: .semibold, secondary: true),
        sceneTitle, objectCount, searchField,
      ], spacing: 8)
    let column = NSTableColumn(identifier: .init("scene"))
    outline.addTableColumn(column)
    outline.outlineTableColumn = column
    outline.headerView = nil
    outline.rowHeight = 30
    outline.style = .sourceList
    outline.dataSource = self
    outline.delegate = self
    outline.target = self
    outline.doubleAction = #selector(outlineDoubleClick(_:))
    outline.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
    outline.allowsEmptySelection = true
    outline.allowsMultipleSelection = true
    outline.setAccessibilityLabel(L10n.tr("outliner.title", "Scene Outliner"))
    let scroll = NSScrollView()
    scroll.documentView = outline
    scroll.hasVerticalScroller = true
    scroll.drawsBackground = false
    let add = button(
      L10n.tr("camera.create", "Create camera"), #selector(createCamera(_:)),
      symbol: "camera.badge.ellipsis")
    let hint = NSTextField(wrappingLabelWithString:
      L10n.tr("outliner.hint", "Select to inspect · Double-click camera to look through"))
    hint.font = .systemFont(ofSize: 10)
    hint.textColor = .secondaryLabelColor
    let footer = stack([add, hint], spacing: 8)
    hint.widthAnchor.constraint(equalTo: footer.widthAnchor).isActive = true
    for v in [heading, scroll, footer] {
      v.translatesAutoresizingMaskIntoConstraints = false
      leftPane.addSubview(v)
    }
    searchField.widthAnchor.constraint(equalTo: heading.widthAnchor).isActive = true
    NSLayoutConstraint.activate([
      heading.leadingAnchor.constraint(equalTo: leftPane.leadingAnchor, constant: 14),
      heading.trailingAnchor.constraint(equalTo: leftPane.trailingAnchor, constant: -12),
      heading.topAnchor.constraint(equalTo: leftPane.topAnchor, constant: 14),
      scroll.leadingAnchor.constraint(equalTo: leftPane.leadingAnchor),
      scroll.trailingAnchor.constraint(equalTo: leftPane.trailingAnchor),
      scroll.topAnchor.constraint(equalTo: heading.bottomAnchor, constant: 12),
      scroll.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -12),
      footer.leadingAnchor.constraint(equalTo: leftPane.leadingAnchor, constant: 14),
      footer.trailingAnchor.constraint(equalTo: leftPane.trailingAnchor, constant: -10),
      footer.bottomAnchor.constraint(equalTo: leftPane.bottomAnchor, constant: -12),
    ])
  }
  func makeViewport() {
    viewport.translatesAutoresizingMaskIntoConstraints = false
    centerPane.addSubview(viewport)
    let header = NSView()
    header.translatesAutoresizingMaskIntoConstraints = false
    centerPane.addSubview(header)
    viewPopup.addItem(withTitle: L10n.tr("view.free", "Free perspective"))
    viewPopup.controlSize = .small
    viewPopup.target = self
    viewPopup.action = #selector(viewChanged(_:))
    viewPopup.setAccessibilityLabel(L10n.tr("view.lookThrough", "Viewport camera"))
    viewPopup.widthAnchor.constraint(lessThanOrEqualToConstant: 270).isActive = true
    let preset = NSPopUpButton(frame: .zero, pullsDown: true)
    preset.controlSize = .small
    preset.addItem(withTitle: L10n.tr("view.orient", "Orient"))
    for (key, english) in [
      ("isometric", "Isometric"), ("front", "Front"), ("back", "Back"), ("right", "Right"),
      ("left", "Left"), ("top", "Top"), ("bottom", "Bottom"),
    ] {
      let item = NSMenuItem(
        title: L10n.tr("view." + key, english), action: #selector(cameraPreset(_:)),
        keyEquivalent: "")
      item.target = self
      item.representedObject = key
      preset.menu?.addItem(item)
    }
    toolPicker.segmentCount = 4
    toolPicker.trackingMode = .selectOne
    toolPicker.selectedSegment = 0
    toolPicker.controlSize = .small
    toolPicker.target = self
    toolPicker.action = #selector(navigationToolChanged(_:))
    for (i, entry) in [
      ("cursorarrow", "Select"), ("rotate.3d", "Orbit"), ("hand.draw", "Pan"),
      ("arrow.up.left.and.arrow.down.right", "Dolly"),
    ].enumerated() {
      toolPicker.setImage(
        NSImage(
          systemSymbolName: entry.0,
          accessibilityDescription: L10n.tr("nav." + entry.1.lowercased(), entry.1)), forSegment: i)
      toolPicker.setWidth(30, forSegment: i)
      toolPicker.setToolTip(L10n.tr("nav." + entry.1.lowercased(), entry.1), forSegment: i)
    }
    upPopup.addItems(withTitles: ["Y ↑", "Z ↑", "X ↑", "−Y ↑", "−Z ↑", "−X ↑"])
    upPopup.controlSize = .small
    upPopup.target = self
    upPopup.action = #selector(upAxisChanged(_:))
    upPopup.toolTip = L10n.tr(
      "nav.upAxis", "Navigation up axis and grid plane. Source coordinates stay unchanged.")
    navigationControls = [toolPicker,viewPopup,preset,upPopup]
    let controls = stack(
      [
        toolPicker, viewPopup, preset, upPopup,
        toolButton(
          "questionmark.circle", help: L10n.tr("nav.help", "Navigation guide"),
          action: #selector(showNavigationHelp(_:))),
      ], vertical: false, spacing: 6)
    pin(controls, in: header, inset: 7)
    compareButton.title = L10n.tr("compare.button", "Render / GT")
    compareButton.target = self; compareButton.action = #selector(toggleComparison(_:)); compareButton.bezelStyle = .rounded
    compareButton.controlSize = .mini; compareButton.isEnabled = false
    let footer = makePreviewFooter()
    footer.translatesAutoresizingMaskIntoConstraints = false
    centerPane.addSubview(footer)
    NSLayoutConstraint.activate([
      header.leadingAnchor.constraint(equalTo: centerPane.leadingAnchor),
      header.trailingAnchor.constraint(equalTo: centerPane.trailingAnchor),
      header.topAnchor.constraint(equalTo: centerPane.topAnchor),
      header.heightAnchor.constraint(equalToConstant: 42),
      viewport.topAnchor.constraint(equalTo: header.bottomAnchor),
      viewport.leadingAnchor.constraint(equalTo: centerPane.leadingAnchor),
      viewport.trailingAnchor.constraint(equalTo: centerPane.trailingAnchor),
      viewport.bottomAnchor.constraint(equalTo: footer.topAnchor),
      footer.leadingAnchor.constraint(equalTo: centerPane.leadingAnchor),
      footer.trailingAnchor.constraint(equalTo: centerPane.trailingAnchor),
      footer.bottomAnchor.constraint(equalTo: centerPane.bottomAnchor),
      footer.heightAnchor.constraint(equalToConstant: 84),
    ])
    emptyPanel.orientation = .vertical
    emptyPanel.alignment = .centerX
    emptyPanel.spacing = 13
    [
      label(AppIdentity().name, size: 26, weight: .light),
      button(L10n.tr("file.open", "Open Scene…"), #selector(openDocument(_:)), symbol: "folder"),
      label(
        L10n.tr("welcome.drop", "Drop a GSV file into the viewport"), size: 11, secondary: true),
    ].forEach { emptyPanel.addArrangedSubview($0) }
    emptyPanel.translatesAutoresizingMaskIntoConstraints = false
    centerPane.addSubview(emptyPanel)
    NSLayoutConstraint.activate([
      emptyPanel.centerXAnchor.constraint(equalTo: viewport.centerXAnchor),
      emptyPanel.centerYAnchor.constraint(equalTo: viewport.centerYAnchor),
    ])
    loadingPanel.material = .hudWindow
    loadingPanel.blendingMode = .withinWindow
    loadingPanel.state = .active
    loadingPanel.wantsLayer = true
    loadingPanel.layer?.cornerRadius = 10
    loadingPanel.isHidden = true
    loadingProgress.style = .bar
    loadingProgress.isIndeterminate = true
    loadingProgress.minValue = 0
    loadingProgress.maxValue = 1
    loadingCancel.title = L10n.tr("common.cancel", "Cancel")
    loadingCancel.target = self
    loadingCancel.action = #selector(cancelLoading(_:))
    loadingCancel.bezelStyle = .rounded
    pin(
      stack([loadingLabel, loadingProgress, loadingCancel], spacing: 14), in: loadingPanel,
      inset: 22)
    loadingPanel.translatesAutoresizingMaskIntoConstraints = false
    centerPane.addSubview(loadingPanel)
    NSLayoutConstraint.activate([
      loadingPanel.widthAnchor.constraint(equalToConstant: 340),
      loadingPanel.centerXAnchor.constraint(equalTo: viewport.centerXAnchor),
      loadingPanel.centerYAnchor.constraint(equalTo: viewport.centerYAnchor),
    ])
  }
  func makeTimeline() {
    let first = toolButton(
      "backward.end", help: L10n.tr("timeline.first", "First sample"),
      action: #selector(firstFrame(_:)))
    let previous = toolButton(
      "backward.frame", help: L10n.tr("timeline.previous", "Previous sample  ←"),
      action: #selector(previousFrame(_:)))
    let next = toolButton(
      "forward.frame", help: L10n.tr("timeline.next", "Next sample  →"),
      action: #selector(nextFrame(_:)))
    let last = toolButton(
      "forward.end", help: L10n.tr("timeline.last", "Last sample"), action: #selector(lastFrame(_:))
    )
    playButton.target = self
    playButton.action = #selector(togglePlayback(_:))
    playButton.bezelStyle = .texturedRounded
    setPlayback(false)
    loopButton.title = L10n.tr("timeline.loop", "Loop")
    loopButton.state = .on
    loopButton.target = self
    loopButton.action = #selector(loopChanged(_:))
    loopButton.controlSize = .small
    speedPopup.addItems(withTitles: ["0.25×", "0.5×", "1×", "2×", "4×"])
    speedPopup.selectItem(at: 2)
    speedPopup.controlSize = .small
    speedPopup.target = self
    speedPopup.action = #selector(speedChanged(_:))
    timeField.target = self
    timeField.action = #selector(authorTimeChanged(_:))
    configureField(
      timeField, label: L10n.tr("timeline.current", "Current document time"), width: 82)
    configureField(
      previewRateField, label: L10n.tr("timeline.previewRate", "Preview units per second"),
      width: 45)
    previewRateField.target = self
    previewRateField.action = #selector(previewRateChanged(_:))
    previewRateLabel.font = .systemFont(ofSize: 10)
    previewRateLabel.textColor = .secondaryLabelColor
    frameLabel.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular)
    let transport = stack(
      [
        first, previous, playButton, next, last, loopButton, speedPopup, separator(),
        label(L10n.tr("timeline.currentShort", "Time")), timeField, frameLabel,
      ], vertical: false, spacing: 6)
    let speed = stack(
      [
        previewRateLabel, previewRateField,
        toolButton(
          "arrow.left.and.right", help: L10n.tr("timeline.fit", "Fit timeline  Home"),
          action: #selector(fitTimeline(_:))),
      ], vertical: false, spacing: 6)
    rangeEnabled.title = L10n.tr("timeline.workRange", "Preview range")
    rangeEnabled.target = self
    rangeEnabled.action = #selector(rangeChanged(_:))
    rangeEnabled.controlSize = .small
    for f in [rangeStart, rangeEnd] {
      configureField(
        f,
        label: f === rangeStart
          ? L10n.tr("timeline.in", "In time") : L10n.tr("timeline.out", "Out time"), width: 80)
      f.target = self
      f.action = #selector(rangeChanged(_:))
    }
    snapButton.title = L10n.tr("timeline.snap", "Snap to samples")
    snapButton.state = .on
    snapButton.target = self
    snapButton.action = #selector(snapChanged(_:))
    snapButton.controlSize = .small
    recordButton.title = L10n.tr("camera.record", "Record view at playhead")
    recordButton.target = self
    recordButton.action = #selector(recordCameraKeyframe(_:))
    recordButton.bezelStyle = .rounded
    recordButton.controlSize = .small
    recordButton.isEnabled = false
    let range = stack(
      [
        rangeEnabled, rangeStart, label("—"), rangeEnd, snapButton,
        label(
          L10n.tr("timeline.dragHint", "Scroll: zoom · Middle-drag: pan · Drag diamond: retime"),
          size: 10, secondary: true),
      ], vertical: false, spacing: 7)
    [transport, speed, timeline, range, recordButton].forEach {
      $0.translatesAutoresizingMaskIntoConstraints = false
      timelineContainer.addSubview($0)
    }
    NSLayoutConstraint.activate([
      transport.leadingAnchor.constraint(equalTo: timelineContainer.leadingAnchor, constant: 12),
      transport.topAnchor.constraint(equalTo: timelineContainer.topAnchor, constant: 8),
      speed.trailingAnchor.constraint(equalTo: timelineContainer.trailingAnchor, constant: -12),
      speed.centerYAnchor.constraint(equalTo: transport.centerYAnchor),
      timeline.leadingAnchor.constraint(equalTo: timelineContainer.leadingAnchor),
      timeline.trailingAnchor.constraint(equalTo: timelineContainer.trailingAnchor),
      timeline.topAnchor.constraint(equalTo: transport.bottomAnchor, constant: 6),
      timeline.heightAnchor.constraint(equalToConstant: 98),
      range.leadingAnchor.constraint(equalTo: timelineContainer.leadingAnchor, constant: 12),
      range.topAnchor.constraint(equalTo: timeline.bottomAnchor, constant: 5),
      recordButton.trailingAnchor.constraint(
        equalTo: timelineContainer.trailingAnchor, constant: -12),
      recordButton.centerYAnchor.constraint(equalTo: range.centerYAnchor),
    ])
    timeline.onRangeError = { [weak self] in
      self?.setStatus(
        L10n.tr(
          "timeline.rangeArithmetic",
          "This time range exceeds display arithmetic; the existing view is retained."))
    }
    timeline.onSeek = { [weak self] in
      self?.setPlayback(false)
      self?.actions.onPlaybackChanged?(false)
      self?.actions.onScrubTime?($0)
    }
    timeline.onKeySelect = { [weak self] in
      self?.cameraKeys.selectItem(at: $0)
      self?.actions.onJumpCameraKey?($0)
    }
    timeline.onKeyMove = { [weak self] in self?.actions.onRetimeCameraKey?($0, $1) }
  }
  func makeStatus() -> NSView {
    let v = NSView()
    statusLabel.font = .systemFont(ofSize: 10)
    statusLabel.textColor = .secondaryLabelColor
    statusLabel.lineBreakMode = .byTruncatingMiddle
    statusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    performanceLabel.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular)
    performanceLabel.textColor = .secondaryLabelColor
    [statusLabel, performanceLabel].forEach {
      $0.translatesAutoresizingMaskIntoConstraints = false
      v.addSubview($0)
    }
    NSLayoutConstraint.activate([
      statusLabel.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 12),
      statusLabel.centerYAnchor.constraint(equalTo: v.centerYAnchor),
      statusLabel.trailingAnchor.constraint(
        lessThanOrEqualTo: performanceLabel.leadingAnchor, constant: -12),
      performanceLabel.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -12),
      performanceLabel.centerYAnchor.constraint(equalTo: v.centerYAnchor),
    ])
    return v
  }
  func rebuildOutline() {
    let expanded = Set(
      (0..<outline.numberOfRows).compactMap { outline.item(atRow: $0) as? SceneOutlineNode }.filter
      { outline.isItemExpanded($0) }.map { $0.id })
    let first = nodes.isEmpty
    let query = searchField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    nodes = SceneCategory.allCases.compactMap { category in
      let objects = (scene?.objects ?? []).filter { SceneCategory.of($0.type) == category }.filter {
        query.isEmpty
          || [$0.label, $0.id, $0.type, $0.encoding].contains {
            $0.localizedCaseInsensitiveContains(query)
          }
      }
      guard !objects.isEmpty else { return nil }
      let title: SceneCategoryTitle = .init(category: category)
      let children = objects.map { object -> SceneOutlineNode in
        let children =
          object.cameraIndices.count > 1
          ? object.cameraIndices.map { index in
            SceneOutlineNode(
              id: object.id + "/view/\(index)", title: scene?.cameraNames[index] ?? "\(index+1)",
              symbol: "camera", object: object, camera: index)
          } : []
        return SceneOutlineNode(
          id: object.id, title: object.label,
          symbol: object.type == "camera" ? "camera" : category.symbol, object: object,
          camera: object.cameraIndices.count == 1 ? object.cameraIndices[0] : nil,
          children: children)
      }
      return SceneOutlineNode(
        id: "group/" + category.rawValue, title: title.value + "  \(objects.count)",
        symbol: category.symbol, children: children)
    }
    rebuildingTree = true
    outline.reloadData()
    for node in nodes {
      if first || !query.isEmpty || expanded.contains(node.id) { outline.expandItem(node) }
      for child in node.children where expanded.contains(child.id) { outline.expandItem(child) }
    }
    syncOutlineSelection()
    rebuildingTree = false
  }
  func outlineRow(for item: StudioSelection) -> Int? {
    guard let id = item.objectID else { return nil }
    var fallback: Int?
    for row in 0..<outline.numberOfRows {
      guard let node = outline.item(atRow: row) as? SceneOutlineNode,
        node.object?.id == id else { continue }
      if case .camera(_, let index) = item, node.cameraIndex == index { return row }
      if node.id == id { fallback = row }
    }
    return fallback
  }
  func syncOutlineSelection() {
    let rows = IndexSet(selectionSet.items.compactMap { outlineRow(for: $0) })
    outline.selectRowIndexes(rows, byExtendingSelection: false)
  }
  func outlineView(_ outlineView: NSOutlineView, numberOfChildrenOfItem item: Any?) -> Int {
    (item as? SceneOutlineNode)?.children.count ?? nodes.count
  }
  func outlineView(_ outlineView: NSOutlineView, child index: Int, ofItem item: Any?) -> Any {
    (item as? SceneOutlineNode)?.children[index] ?? nodes[index]
  }
  func outlineView(_ outlineView: NSOutlineView, isItemExpandable item: Any) -> Bool {
    !(item as! SceneOutlineNode).children.isEmpty
  }
  func outlineView(_ outlineView: NSOutlineView, shouldSelectItem item: Any) -> Bool {
    (item as? SceneOutlineNode)?.object != nil
  }
  func outlineView(_ outlineView: NSOutlineView, viewFor tableColumn: NSTableColumn?, item: Any)
    -> NSView?
  {
    let node = item as! SceneOutlineNode
    let icon = NSImageView(
      image: NSImage(systemSymbolName: node.symbol, accessibilityDescription: nil) ?? NSImage())
    icon.contentTintColor = node.object?.authored == true ? .systemOrange : .secondaryLabelColor
    icon.widthAnchor.constraint(equalToConstant: 16).isActive = true
    let text = label(node.title, size: 11, weight: node.object == nil ? .semibold : .regular)
    text.lineBreakMode = .byTruncatingMiddle
    text.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    text.setContentHuggingPriority(.init(1), for: .horizontal)
    text.toolTip = node.object.map { $0.label + "\n" + $0.id + "\n" + $0.encoding }
    let row = stack([icon, text], vertical: false, spacing: 6)
    row.distribution = .fill
    if let object = node.object, let visible = object.visible, node.id == object.id {
      let eye = NSButton(
        image: NSImage(
          systemSymbolName: visible ? "eye" : "eye.slash",
          accessibilityDescription: L10n.tr("object.visibility", "Toggle object visibility"))!,
        target: self, action: #selector(objectVisibilityChanged(_:)))
      eye.isBordered = false
      eye.identifier = .init(object.id)
      eye.state = visible ? .on : .off
      eye.toolTip = L10n.tr("object.visibility", "Toggle object visibility")
      eye.widthAnchor.constraint(equalToConstant: 22).isActive = true
      row.addArrangedSubview(eye)
    } else if node.object != nil, node.cameraIndex == nil {
      let lock = NSImageView(
        image: NSImage(
          systemSymbolName: "lock",
          accessibilityDescription: L10n.tr("object.readonly", "Read-only source object"))!)
      lock.contentTintColor = .tertiaryLabelColor
      lock.widthAnchor.constraint(equalToConstant: 12).isActive = true
      row.addArrangedSubview(lock)
    }
    let cell = NSTableCellView()
    cell.textField = text
    cell.imageView = icon
    pin(row, in: cell, inset: 2)
    cell.setAccessibilityLabel(node.title)
    return cell
  }
  func outlineViewSelectionDidChange(_ notification: Notification) {
    guard !rebuildingTree else { return }
    let toggling = outline.selectionOperation == .toggle
    let rows = outline.selectedRowIndexes
    var items = toggling
      ? selectionSet.items.filter { outlineRow(for: $0) == nil } : []
    var clicked: StudioSelection?
    for row in rows {
      guard let node = outline.item(atRow: row) as? SceneOutlineNode,
        let object = node.object else { continue }
      let previous = toggling ? selectionSet.items.filter { outlineRow(for: $0) == row } : []
      let represented = node.cameraIndex.map { StudioSelection.camera(object.id, $0) }
        ?? .object(object.id)
      items.append(contentsOf: previous.isEmpty ? [represented] : previous)
      if row == outline.selectionGestureRow { clicked = previous.last ?? represented }
    }
    let primary = clicked ?? (items.contains(selection) ? selection : items.last)
    actions.onSelectSelections?(StudioSelectionSet(items: items, primary: primary))
  }
  @objc func outlineDoubleClick(_ sender: Any?) {
    guard let node = outline.item(atRow: outline.clickedRow) as? SceneOutlineNode,
      let index = node.cameraIndex
    else { return }
    actions.onSelectCamera?(index)
  }
  @objc func objectVisibilityChanged(_ sender: NSButton) {
    guard let id = sender.identifier?.rawValue else { return }
    if let visible = scene?.objects.first(where: { $0.id == id })?.visible {
      actions.onSetObjectVisible?(id, !visible)
    }
  }
  func controlTextDidChange(_ notification: Notification) {
    if notification.object as? NSSearchField === searchField { rebuildOutline() }
  }
  func splitView(_ splitView: NSSplitView, canCollapseSubview subview: NSView) -> Bool {
    subview !== centerPane
  }
  func splitView(_ splitView: NSSplitView, constrainMinCoordinate p: CGFloat, ofSubviewAt i: Int)
    -> CGFloat
  { i == 0 ? 190 : max(580, splitView.bounds.width - 440) }
  func splitView(_ splitView: NSSplitView, constrainMaxCoordinate p: CGFloat, ofSubviewAt i: Int)
    -> CGFloat
  { i == 0 ? 350 : splitView.bounds.width - 290 }
}

private struct SceneCategoryTitle {
  let value: String
  init(category: SceneCategory) {
    switch category {
    case .representations: value = L10n.tr("category.representations", "4D representations")
    case .cameras: value = L10n.tr("category.cameras", "Cameras")
    case .motion: value = L10n.tr("category.motion", "Motion & structure")
    case .observations: value = L10n.tr("category.observations", "Observations")
    case .appearance: value = L10n.tr("category.appearance", "Appearance")
    case .rendering: value = L10n.tr("category.rendering", "Rendering")
    case .data: value = L10n.tr("category.data", "Supporting data")
    }
  }
}
