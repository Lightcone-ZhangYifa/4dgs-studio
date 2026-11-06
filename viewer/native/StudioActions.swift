import AppKit
import simd

extension WorkspaceController {
  func wireViewport() {
    centerPane.openURLs = { [weak self] urls in self?.actions.onOpenURLs?(urls) }
    viewport.orbit = { [weak self] in
      self?.markFreeCamera()
      self?.actions.onOrbit?($0)
    }
    viewport.pan = { [weak self] in
      self?.markFreeCamera()
      self?.actions.onPan?($0)
    }
    viewport.dolly = { [weak self] in
      self?.markFreeCamera()
      self?.actions.onDolly?($0)
    }
    viewport.dollyAt = { [weak self] in
      self?.markFreeCamera()
      self?.actions.onDollyAt?($0, $1)
    }
    viewport.roll = { [weak self] in
      self?.markFreeCamera()
      self?.actions.onRoll?($0)
    }
    viewport.navigationBoundary = { [weak self] in self?.actions.onNavigationBoundary?($0) }
    viewport.pick = { [weak self] in self?.actions.onPick?($0, $1) }
    viewport.openURLs = { [weak self] in self?.actions.onOpenURLs?($0) }
    viewport.onDrawableSizeChanged = { [weak self] _ in
      guard let self else { return }
      self.updatePerformance(self.lastPerformance ?? WorkspacePerformance())
    }
    viewport.keyAction = { [weak self] key in
      guard let self else { return false }
      switch key {
      case 49: self.togglePlayback(nil)
      case 123: self.previousFrame(nil)
      case 124: self.nextFrame(nil)
      case 3: self.focusSelection(nil)
      case 115: self.resetCamera(nil)
      case 53:
        self.setPlayback(false)
        self.actions.onPlaybackChanged?(false)
        self.actions.onClearSelection?()
      case 18: self.applyPreset("front")
      case 20: self.applyPreset("right")
      case 26: self.applyPreset("top")
      default: return false
      }
      return true
    }
    camerasButton.state = .on
    trajectoryButton.state = .on
    trailButton.state = .on
  }
  func updateScene(_ info: WorkspaceSceneInfo) {
    let replacement = lastSceneURL != info.url
    let previousClock = scene?.activeClock
    scene = info
    lastSceneURL = info.url
    sceneTitle.stringValue = info.name
    sceneTitle.toolTip = info.url.path
    objectCount.stringValue = L10n.format(
      "scene.counts", "%d objects · %d time samples", info.objects.count, info.frameCount)
    window?.title = info.name + " — 4DGS Studio"
    if replacement {
      selectionSet = StudioSelectionSet(items: info.activeObjectID.map { [.object($0)] } ?? [])
      elementInfo = nil
      cameraInspection = nil
      objectMetadata = ""
      nodes = []
      rangeEnabled.state = .off
      timeline.previewRange = nil
      searchField.stringValue = ""
      rangeStart.stringValue = L10n.time(info.startTime)
      rangeEnd.stringValue = L10n.time(info.endTime)
    }
    let retained = selectionSet.items.filter { item in
      item.objectID.map { id in info.objects.contains(where: { $0.id == id }) } ?? false
    }
    if retained != selectionSet.items {
      selectionSet = StudioSelectionSet(items: retained, primary: selection)
      elementInfo = nil
      cameraInspection = nil
    }
    let selectedView = viewPopup.indexOfSelectedItem
    viewPopup.removeAllItems()
    viewPopup.addItem(withTitle: L10n.tr("view.free", "Free perspective"))
    viewPopup.addItems(withTitles: info.cameraNames)
    if selectedView >= 0, selectedView < viewPopup.numberOfItems {
      viewPopup.selectItem(at: selectedView)
    }
    timeline.times = info.times.isEmpty ? [info.startTime, info.endTime] : info.times
    timeline.unit = info.timeUnit
    if replacement || previousClock != info.activeClock { timeline.fit() }
    previewRateLabel.stringValue =
      L10n.tr("timeline.previewLabel", "Preview") + " "
      + L10n.tr("unit." + info.timeUnit, info.timeUnit) + "/s"
    if previewRateField.currentEditor() == nil {
      previewRateField.stringValue = L10n.number(info.previewRate, digits: 3)
    }
    coordinateCaption.stringValue = L10n.format(
      "viewport.domain", "Space: %@   ·   Clock: %@   ·   %@", info.activeSpace, info.activeClock,
      L10n.tr("unit." + info.timeUnit, info.timeUnit))
    coordinateCaption.toolTip = info.spaceDescription + "\n" + info.clockDescription
    emptyPanel.isHidden = true
    loadingPanel.isHidden = true
    loadingProgress.stopAnimation(nil)
    rebuildOutline()
    rebuildInspector()
  }
  func updateFrame(index: Int, time: Double) {
    currentFrame = max(0, min(index, max(0, (scene?.frameCount ?? 1) - 1)))
    currentTime = time
    timeline.currentTime = time
    frameLabel.stringValue = L10n.format(
      "timeline.sampleLabel", "Sample %d / %d", currentFrame + 1, scene?.frameCount ?? 0)
    if timeField.currentEditor() == nil { timeField.stringValue = L10n.time(time) }
    refreshSelectionHeading()
  }
  func setPlayback(_ playing: Bool) {
    isPlaying = playing
    let title = playing ? L10n.tr("timeline.pause", "Pause") : L10n.tr("timeline.play", "Play")
    playButton.image = NSImage(
      systemSymbolName: playing ? "pause.fill" : "play.fill", accessibilityDescription: title)
    playButton.toolTip = title + "  ␣"
    playButton.setAccessibilityLabel(title)
    toolbarPlay?.image = playButton.image
    toolbarPlay?.label = title
  }
  /// All selection membership comes from the document controller. Attribute
  /// refreshes below only describe the primary item, including during playback.
  func setSelectionPresentation(_ set: StudioSelectionSet) {
    guard selectionSet != set else { return }
    let changedPrimary = selection != set.primary
    selectionSet = set
    if changedPrimary { elementInfo = nil; cameraInspection = nil; objectMetadata = "" }
    rebuildingTree = true
    syncOutlineSelection()
    rebuildingTree = false
    inspectorTabs.selectedSegment = 0
    rebuildInspector()
  }
  func updateSelection(_ info: WorkspaceSelectionInfo?) {
    elementInfo = info
    if info != nil { cameraInspection = nil }
    if let info, idField.currentEditor() == nil { idField.stringValue = String(info.id) }
    if info != nil && inspectorValues["identity"] == nil {
      rebuildInspector()
    } else {
      refreshSelectionHeading()
      updateElementValues()
    }
  }
  func selectObjectPresentation(_ id: String) {
    elementInfo = nil
    cameraInspection = nil
    rebuildingTree = true
    syncOutlineSelection()
    rebuildingTree = false
    rebuildInspector()
  }
  func clearSelectionPresentation() {
    elementInfo = nil
    cameraInspection = nil
    rebuildingTree = true
    syncOutlineSelection()
    rebuildingTree = false
    rebuildInspector()
  }
  func inspectObject(_ text: String) { objectMetadata = text }
  func inspectCamera(_ info: WorkspaceCameraInspection) {
    elementInfo = nil
    cameraInspection = info
    if inspectorValues["cameraK"] == nil {
      rebuildInspector()
    } else {
      refreshSelectionHeading()
      updateCameraValues(info)
    }
    rebuildingTree = true
    syncOutlineSelection()
    rebuildingTree = false
  }
  func updateCameraAuthoring(
    name: String?, times: [Double], fov: Double?, canUndo: Bool, canRedo: Bool,
    timelineCompatible: Bool = true
  ) {
    let stateChanged = (editingCameraName == nil) != (name == nil)
    let timesChanged = times != editingCameraTimes
    editingCameraName = name
    editingCameraTimes = times
    editingFOV = fov
    canUndoCamera = canUndo
    canRedoCamera = canRedo
    if cameraNameField.currentEditor() == nil { cameraNameField.stringValue = name ?? "" }
    if cameraFOVField.currentEditor() == nil {
      cameraFOVField.stringValue = fov.map { L10n.number($0, digits: 3) } ?? ""
    }
    timeline.keyName = name ?? ""
    timeline.keyTimes = timelineCompatible ? times : []
    recordButton.isEnabled = name != nil && timelineCompatible
    if timesChanged { refreshKeyList() }
    if stateChanged { rebuildInspector() }
  }
  func setCameraCaption(_ name: String) {
    if let index = scene?.cameraNames.firstIndex(of: name) {
      viewPopup.selectItem(at: index + 1)
    } else {
      viewPopup.selectItem(at: 0)
    }
  }
  func updatePerformance(_ info: WorkspacePerformance) {
    lastPerformance = info
    refreshPerformanceLabel()
  }
  func refreshPerformanceLabel() {
    if comparisonActive {
      performanceLabel.stringValue = L10n.tr(
        "status.comparisonPerformance", "3D viewport hidden · comparison timing not measured")
      performanceLabel.toolTip = L10n.tr(
        "status.comparisonPerformanceDetail",
        "3D viewport GPU times and visible counts do not describe this independent camera capture. No comparison timing is reported.")
      return
    }
    let info = lastPerformance ?? WorkspacePerformance()
    let gpu = info.gpuMilliseconds.map { L10n.number($0, digits: 1) + " ms" } ?? "—"
    performanceLabel.stringValue = L10n.format(
      "status.performance", "GPU %@  ·  %@ / %@ IDs  ·  %d%%", gpu, info.visibleCount.formatted(),
      info.totalCount.formatted(), Int(viewport.renderScale * 100))
    performanceLabel.toolTip = L10n.format(
      "status.performanceDetail",
      "Completed renders/s: %@. GPU buffers: %@. This is not display FPS or total process memory.",
      info.fps.map { L10n.number($0, digits: 1) } ?? "—",
      ByteCountFormatter.string(fromByteCount: Int64(info.residentBytes), countStyle: .memory))
  }
  func setStatus(_ message: String) {
    statusLabel.stringValue = message
    statusLabel.toolTip = message
  }
  func setLoading(message: String?, progress: Double? = nil, cancellable: Bool = true) {
    guard let message else {
      loadingPanel.isHidden = true
      loadingProgress.stopAnimation(nil)
      emptyPanel.isHidden = scene != nil
      return
    }
    loadingPanel.isHidden = false
    emptyPanel.isHidden = true
    loadingLabel.stringValue = message
    setStatus(message)
    loadingCancel.isHidden = !cancellable
    loadingCancel.isEnabled = true
    loadingProgress.isIndeterminate = progress == nil
    if let progress {
      loadingProgress.stopAnimation(nil)
      loadingProgress.doubleValue = max(0, min(1, progress))
    } else {
      loadingProgress.startAnimation(nil)
    }
  }
  func presentError(_ error: Error) { presentError(message: error.localizedDescription) }
  func presentError(message: String) {
    setLoading(message: nil)
    setStatus(L10n.tr("error.operation", "Unable to complete operation"))
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = L10n.tr("error.title", "Scene operation failed")
    alert.informativeText = L10n.tr("error.technicalDetail", "Technical detail") + "\n" + message
    alert.addButton(withTitle: L10n.tr("common.ok", "OK"))
    if let window { alert.beginSheetModal(for: window) } else { alert.runModal() }
  }
  @objc func openDocument(_ sender: Any?) {
    let panel = NSOpenPanel()
    panel.title = L10n.tr("file.openGSV", "Open GSV Scene")
    panel.canChooseDirectories = false
    panel.allowsMultipleSelection = false
    guard let window else { return }
    panel.beginSheetModal(for: window) { [weak self] response in
      if response == .OK { self?.actions.onOpenURLs?(panel.urls) }
    }
  }
  @objc func togglePlayback(_ sender: Any?) {
    guard (scene?.frameCount ?? 0) > 1 else { return }
    setPlayback(!isPlaying)
    actions.onPlaybackChanged?(isPlaying)
  }
  func seek(_ index: Int) {
    guard let scene else { return }
    setPlayback(false)
    actions.onPlaybackChanged?(false)
    actions.onSeekFrame?(max(0, min(index, scene.frameCount - 1)))
  }
  @objc func firstFrame(_ sender: Any?) { seek(0) }
  @objc func previousFrame(_ sender: Any?) { seek(currentFrame - 1) }
  @objc func nextFrame(_ sender: Any?) { seek(currentFrame + 1) }
  @objc func lastFrame(_ sender: Any?) { seek((scene?.frameCount ?? 1) - 1) }
  func markFreeCamera() { viewPopup.selectItem(at: 0) }
  @objc func resetCamera(_ sender: Any?) {
    markFreeCamera()
    actions.onResetCamera?()
    window?.makeFirstResponder(viewport)
  }
  @objc func cameraPreset(_ sender: Any?) {
    if let item = sender as? NSMenuItem, let name = item.representedObject as? String {
      applyPreset(name)
    }
  }
  func applyPreset(_ name: String) {
    markFreeCamera()
    actions.onCameraPreset?(name)
    window?.makeFirstResponder(viewport)
  }
  @objc func cameraTop(_ sender: Any?) { applyPreset("top") }
  @objc func cameraFront(_ sender: Any?) { applyPreset("front") }
  @objc func cameraRight(_ sender: Any?) { applyPreset("right") }
  @objc func cameraIsometric(_ sender: Any?) { applyPreset("isometric") }
  @objc func focusSelection(_ sender: Any?) {
    actions.onFocusSelection?()
    window?.makeFirstResponder(viewport)
  }
  @objc func previousView(_ sender: Any?) {
    markFreeCamera()
    actions.onPreviousView?()
  }
  @objc func nextView(_ sender: Any?) {
    markFreeCamera()
    actions.onNextView?()
  }
  @objc func viewChanged(_ sender: Any?) {
    let index = viewPopup.indexOfSelectedItem
    actions.onSelectCamera?(index > 0 ? index - 1 : nil)
    window?.makeFirstResponder(viewport)
  }
  @objc func lookThroughSelection(_ sender: Any?) {
    if let camera = cameraInspection { actions.onSelectCamera?(camera.entry) }
  }
  @objc func navigationToolChanged(_ sender: Any?) {
    viewport.tool = [.select, .orbit, .pan, .dolly][toolPicker.selectedSegment]
    window?.makeFirstResponder(viewport)
  }
  @objc func upAxisChanged(_ sender: Any?) {
    actions.onUpAxis?(["y", "z", "x", "-y", "-z", "-x"][upPopup.indexOfSelectedItem])
  }
  @objc func showNavigationHelp(_ sender: Any?) {
    let alert = NSAlert()
    alert.messageText = L10n.tr("nav.help", "Navigation guide")
    alert.informativeText = L10n.tr(
      "nav.guide",
      "SELECT\nClick a primitive. Command-click toggles mixed selection. F frames selection; Home frames the scene.\n\nMOUSE\nMiddle-drag or Option-left-drag: orbit\nShift-middle-drag / right-drag: pan\nOption-right-drag: dolly\nWheel: dolly around pointer\n\nTRACKPAD\nTwo fingers: pan · Option-two fingers: orbit\nPinch: dolly · Rotate: roll\n\nUse the toolbar tools for one-button navigation. Escape cancels a drag. View history is separate from camera edit Undo. Standard views remain perspective."
    )
    alert.addButton(withTitle: L10n.tr("common.ok", "OK"))
    if let window { alert.beginSheetModal(for: window) }
  }
  @objc func findID(_ sender: Any?) {
    guard let id = Int64(idField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)) else {
      setStatus(L10n.tr("selection.invalidID", "Enter an exact signed 64-bit integer ID."))
      NSSound.beep()
      return
    }
    actions.onSelectID?(id)
  }
  @objc func speedChanged(_ sender: Any?) {
    actions.onPlaybackSpeedChanged?([0.25, 0.5, 1, 2, 4][speedPopup.indexOfSelectedItem])
  }
  @objc func loopChanged(_ sender: Any?) { actions.onLoopChanged?(loopButton.state == .on) }
  @objc func authorTimeChanged(_ sender: Any?) {
    guard let time = L10n.parse(timeField.stringValue) else {
      setStatus(
        L10n.tr("timeline.invalidTime", "Enter a finite document time."))
      return
    }
    window?.makeFirstResponder(viewport)
    actions.onSeekSeconds?(time)
  }
  @objc func previewRateChanged(_ sender: Any?) {
    guard let value = L10n.parse(previewRateField.stringValue), value > 0, value <= 10000 else {
      setStatus(
        L10n.tr(
          "timeline.invalidRate",
          "Preview rate must be greater than zero and at most 10000 units/s."))
      return
    }
    actions.onPreviewRateChanged?(value)
  }
  @objc func rangeChanged(_ sender: Any?) {
    guard rangeEnabled.state == .on else {
      timeline.previewRange = nil
      actions.onSetPreviewRange?(nil)
      return
    }
    guard let a = L10n.parse(rangeStart.stringValue), let b = L10n.parse(rangeEnd.stringValue),
      a < b, let scene, a >= scene.startTime, b <= scene.endTime
    else {
      setStatus(
        L10n.tr(
          "timeline.invalidRange",
          "Preview in/out must be ordered and within the document timeline."))
      rangeEnabled.state = .off
      timeline.previewRange = nil
      actions.onSetPreviewRange?(nil)
      return
    }
    timeline.previewRange = a...b
    actions.onSetPreviewRange?(a...b)
  }
  @objc func snapChanged(_ sender: Any?) { timeline.snap = snapButton.state == .on }
  @objc func fitTimeline(_ sender: Any?) { timeline.fit() }
  @objc func createCamera(_ sender: Any?) { actions.onCreateCamera?() }
  @objc func recordCameraKeyframe(_ sender: Any?) { actions.onRecordCameraKeyframe?() }
  @objc func saveCameras(_ sender: Any?) { actions.onSaveCameras?() }
  @objc func importCameras(_ sender: Any?) { actions.onImportCameras?() }
  @objc func renameCamera(_ sender: Any?) { actions.onRenameCamera?(cameraNameField.stringValue) }
  @objc func deleteCamera(_ sender: Any?) { actions.onDeleteCamera?() }
  @objc func jumpCameraKey(_ sender: Any?) {
    let index = cameraKeys.indexOfSelectedItem
    timeline.selectedKey = index
    if index >= 0, index < editingCameraTimes.count {
      keyTimeField.stringValue = L10n.time(editingCameraTimes[index])
    }
    actions.onJumpCameraKey?(index)
  }
  @objc func deleteCameraKey(_ sender: Any?) {
    actions.onDeleteCameraKey?(cameraKeys.indexOfSelectedItem)
  }
  @objc func retimeCameraKey(_ sender: Any?) {
    if let time = L10n.parse(keyTimeField.stringValue) {
      actions.onRetimeCameraKey?(cameraKeys.indexOfSelectedItem, time)
    }
  }
  @objc func cameraFOVChanged(_ sender: Any?) {
    if let degrees = L10n.parse(cameraFOVField.stringValue) { actions.onCameraFOV?(degrees) }
  }
  @objc func cameraTransformChanged(_ sender: Any?) {
    let p = positionFields.compactMap { L10n.parse($0.stringValue) }
    let q = rotationFields.compactMap { L10n.parse($0.stringValue) }
    guard p.count == 3, q.count == 4, (p + q).allSatisfy({ abs($0) < 1e12 }) else {
      setStatus(L10n.tr("camera.invalidTransform", "Enter finite XYZ and WXYZ components."))
      return
    }
    actions.onCameraTransform?(
      SIMD3(Float(p[0]), Float(p[1]), Float(p[2])),
      SIMD4(Float(q[0]), Float(q[1]), Float(q[2]), Float(q[3])))
  }
  func updateOrbitCenter(_ value: SIMD3<Float>) {
    orbitCenter = value
    let text = [value.x, value.y, value.z].map { L10n.number(Double($0), digits: 5) }.joined(
      separator: "  ")
    inspectorValues["orbitCenter"]?.stringValue = text
    inspectorValues["orbitCenter"]?.toolTip = text
  }
  @objc func orbitStyleChanged(_ sender: Any?) {
    trackballOrbit = orbitStylePopup.indexOfSelectedItem == 1
    actions.onTrackballOrbit?(trackballOrbit)
  }
  @objc func pivotSelection(_ sender: Any?) { actions.onOrbitCenterSelection?() }
  @objc func pivotView(_ sender: Any?) { actions.onOrbitCenterView?() }
  @objc func pivotVisibilityChanged(_ sender: Any?) {
    renderOptions.showOrbitCenter = pivotButton.state == .on
    actions.onRenderOptionsChanged?(renderOptions)
  }
  @objc func undoCamera(_ sender: Any?) { actions.onUndoCamera?() }
  @objc func redoCamera(_ sender: Any?) { actions.onRedoCamera?() }
  @objc func renderSliderChanged(_ sender: NSSlider) {
    guard let key = sender.identifier?.rawValue else { return }
    let v = sender.floatValue
    inspectorValues[key]?.stringValue = L10n.number(Double(v), digits: 2) + "×"
    switch key {
    case "scale": renderOptions.gaussianScale = v
    case "opacity": renderOptions.opacityScale = v
    case "exposure": renderOptions.exposure = v
    default: return
    }
    actions.onRenderOptionsChanged?(renderOptions)
  }
  @objc func backgroundChanged(_ sender: Any?) {
    renderOptions.background =
      [
        SIMD4<Float>(0.055, 0.061, 0.071, 1), SIMD4(0, 0, 0, 1), SIMD4(0.18, 0.18, 0.18, 1),
        SIMD4(1, 1, 1, 1),
      ][backgroundPopup.indexOfSelectedItem]
    actions.onRenderOptionsChanged?(renderOptions)
  }
  @objc func gridChanged(_ sender: Any?) {
    renderOptions.showGrid = gridButton.state == .on
    actions.onRenderOptionsChanged?(renderOptions)
  }
  @objc func camerasChanged(_ sender: Any?) { actions.onShowCameras?(camerasButton.state == .on) }
  @objc func trajectoryChanged(_ sender: Any?) {
    trajectoryOptions.showPaths = trajectoryButton.state == .on
    publishTrajectoryOptions()
  }
  @objc func resolutionChanged(_ sender: Any?) {
    renderOptions.resolutionScale = [1, 0.75, 0.5][resolutionPopup.indexOfSelectedItem]
    actions.onRenderOptionsChanged?(renderOptions)
  }
  @objc func nearClipFieldChanged(_ sender: Any?) {
    guard let value = L10n.parse(nearClipField.stringValue), value >= 0.0001, value <= 100 else {
      setStatus(
        L10n.tr("display.invalidClip", "Near clip must be between 0.0001 and 100 scene units."))
      return
    }
    renderOptions.nearClip = Float(value)
    actions.onRenderOptionsChanged?(renderOptions)
  }
  @objc func cancelLoading(_ sender: Any?) {
    actions.onCancelLoading?()
    loadingCancel.isEnabled = false
  }
  @objc func toggleScenePane(_ sender: Any?) {
    leftPane.isHidden.toggle()
    split.adjustSubviews()
  }
  @objc func toggleInspector(_ sender: Any?) {
    rightPane.isHidden.toggle()
    split.adjustSubviews()
  }
  @objc func exportScreenshot(_ sender: Any?) { actions.onExportScreenshot?() }
  @objc func exportWorkspace(_ sender: Any?) { actions.onExportWorkspace?() }

  func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
    ["open", "scene", "play", "reset", "focus", "camera", "capture", "inspector"].map { .init($0) }
      + [.flexibleSpace, .space]
  }
  func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
    [
      .init("open"), .init("scene"), .flexibleSpace, .init("reset"), .init("focus"),
      .init("camera"), .flexibleSpace, .init("capture"), .init("inspector"),
    ]
  }
  func toolbar(
    _ toolbar: NSToolbar, itemForItemIdentifier id: NSToolbarItem.Identifier,
    willBeInsertedIntoToolbar flag: Bool
  ) -> NSToolbarItem? {
    let items: [String: (String, String, Selector)] = [
      "open": (L10n.tr("file.open", "Open Scene…"), "folder", #selector(openDocument(_:))),
      "scene": (
        L10n.tr("outliner.toggle", "Toggle Outliner"), "sidebar.left",
        #selector(toggleScenePane(_:))
      ), "play": (L10n.tr("timeline.play", "Play"), "play.fill", #selector(togglePlayback(_:))),
      "reset": (L10n.tr("view.frameAll", "Frame scene"), "viewfinder", #selector(resetCamera(_:))),
      "focus": (L10n.tr("view.focus", "Frame selection"), "scope", #selector(focusSelection(_:))),
      "camera": (
        L10n.tr("camera.create", "Create camera"), "camera.badge.ellipsis",
        #selector(createCamera(_:))
      ),
      "capture": (
        L10n.tr("file.screenshot", "Save Screenshot…"), "camera", #selector(exportScreenshot(_:))
      ),
      "inspector": (
        L10n.tr("inspector.toggle", "Toggle Inspector"), "sidebar.right",
        #selector(toggleInspector(_:))
      ),
    ]
    guard let item = items[id.rawValue] else { return nil }
    let result = NSToolbarItem(itemIdentifier: id)
    result.label = item.0
    result.toolTip = item.0
    result.image = NSImage(systemSymbolName: item.1, accessibilityDescription: item.0)
    result.target = self
    result.action = item.2
    if id.rawValue == "play" { toolbarPlay = result }
    return result
  }
}
