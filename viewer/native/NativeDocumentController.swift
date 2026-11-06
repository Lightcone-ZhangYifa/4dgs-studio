import AppKit
import CoreFoundation
import MetalKit
import simd

/// Coordinates document objects, a bounded frame working set, and the
/// native workspace. Heavy file work never runs on the AppKit event loop.
final class NativeDocumentController {
  private let workspace: WorkspaceController
  private var renderer: GaussianRenderer?
  private var scene: GeometryScene?
  private var visibleObjectIDs = Set<String>()
  private var selectedObjectID: String?
  private var selectionSet = StudioSelectionSet()
  private var selectionRevision: UInt64 = 0
  private var pendingPick: (
    operation: StudioSelectionOperation, revision: UInt64, generation: UUID,
    space: String?, clock: String, cameras: [(objectID: String, viewID: String)]
  )?
  private var document: GSVDocument?
  private var timeline = [0.0]
  private var authoredTimeline = [String: [Double]]()
  private var sourceCameras = [GSVCamera]()
  private var authoredCameras = [AuthoredCamera]()
  private var editingCameraID: String?
  private var camerasDirty = false
  private let cameraUndo = UndoManager()
  private var cameraRevision: UInt64 = 0
  private var cameraFilePanelOpen = false
  private var authoredSpaces = [String: Any](), authoredClocks = [String: Any]()
  private var viewportSpace: String?, viewportClock: String?
  private var cameraFrustaVisible = true
  private let io = SceneIOWorker()
  private var ioCancellation = SceneIOCancellation()
  private var generation = UUID()
  private var isShuttingDown = false
  private var timer: Timer?
  private var playing = false
  private var speed = 1.0
  private var previewRates = [String: Double]()
  private var previewRate: Double {
    previewRates[activeClockID] ?? (currentTimeUnit() == "frame" ? 30 : 1)
  }
  private var loop = true
  private var lastTick = CACurrentMediaTime()
  private var targetTime = 0.0
  /// Last time committed with the displayed Gaussian buffers. Pending I/O must
  /// never advance cameras, highlights or trails ahead of the actual geometry.
  private var committedTime = 0.0
  private var currentLower = -1
  private var currentA: MTLBuffer?
  private var currentB: MTLBuffer?
  private var preparedA: MTLBuffer?
  private var preparedB: MTLBuffer?
  private var frameRequestActive = false
  private var selectedIndex: Int?
  private var cameraEntries: [(name: String, camera: GSVCamera, view: Int)] = []
  private var inspectedCamera: Int?
  private var previewRange: ClosedRange<Double>?
  private var selectedCamera: Int?
  private var lastAlpha: Float = 0
  private var trajectoryOptions = TrajectoryDisplayOptions()
  private var trajectoryToken = UUID()
  private var trajectoryCancellation: TrajectoryBuildCancellation?
  private let trajectoryQueue = DispatchQueue(label: "org.gsv.trajectory", qos: .userInitiated)
  private var observations = [GSVCameraObservation]()
  private var radiometry = [GSVCameraRadiometry]()
  private var renderProfiles = [GSVRenderProfile]()
  private var renderViews = [GSVRenderView]()
  private var renderFields = [String:StreamingScene]()
  private var comparisonEntries = [WorkspaceComparisonEntry]()
  private var comparisonIndex: Int?
  private var previewRenderViewID: String?
  private var comparisonEnabled = false
  private var comparisonRevision = UUID()
  private var comparisonCancellation: ComparisonCancellation?
  private var comparisonBusy = false
  private var comparisonGeometryPending = false
  private var comparisonPending = false
  private let comparisonQueue = DispatchQueue(label: "org.gsv.comparison", qos: .userInitiated)

  init(workspace: WorkspaceController) {
    self.workspace = workspace
    workspace.cameraUndoManager = cameraUndo
    cameraUndo.groupsByEvent = false
    cameraUndo.levelsOfUndo = 100
    do {
      let renderer = try GaussianRenderer(view: workspace.viewport)
      self.renderer = renderer
      renderer.onMetrics = { [weak self] m in
        guard let self, !self.isShuttingDown else { return }
        if let center = self.renderer?.orbitCenter() { self.workspace.updateOrbitCenter(center) }
        self.workspace.updatePerformance(
          WorkspacePerformance(
            fps: m.fps, gpuMilliseconds: m.gpuMilliseconds,
            visibleCount: m.visibleCount, totalCount: m.totalCount, residentBytes: m.residentBytes,
            sortMilliseconds: m.sortMilliseconds))
      }
      renderer.onScenePick = { [weak self] result in
        guard let self, !self.isShuttingDown else { return }
        self.consumeScenePick(result)
      }
      renderer.onError = { [weak self] message in
        guard let self, !self.isShuttingDown else { return }
        self.setPlaying(false)
        self.workspace.presentError(message: message)
      }
    } catch { workspace.presentError(message: error.localizedDescription) }
    workspace.actions.onActivateRenderView = {[weak self] id in self?.activateRenderPreview(id)}
    workspace.actions.onToggleComparison = {[weak self] enabled in self?.toggleComparison(enabled)}
    workspace.actions.onSelectComparison = {[weak self] index in self?.selectComparison(index)}
    workspace.actions.onCompareObservation = {[weak self] id in
      guard let self, let index = self.comparisonEntries.firstIndex(where:{$0.observationID == id}) else {return}
      self.selectComparison(index)
    }
    workspace.actions.onOpenURLs = { [weak self] urls in
      if let first = urls.first { self?.open(first) }
    }
    workspace.actions.onCancelLoading = { [weak self] in self?.cancelLoading() }
    workspace.actions.onSeekFrame = { [weak self] index in
      guard let self else { return }
      self.setPlaying(false)
      self.requestTime(self.timeline[max(0, min(index, self.timeline.count - 1))])
    }
    workspace.actions.onScrubTime = { [weak self] time in
      self?.setPlaying(false)
      self?.requestTime(time)
    }
    workspace.actions.onSetPreviewRange = { [weak self] range in
      self?.previewRange = range
      self?.setPlaying(false)
    }
    workspace.actions.onRetimeCameraKey = { [weak self] key, time in
      self?.retimeCameraKey(key, time: time)
    }
    workspace.actions.onCameraTransform = { [weak self] p, q in
      self?.keyframeCameraTransform(position: p, quaternion: q)
    }
    workspace.actions.onInspectCamera = { [weak self] index in self?.inspectCamera(index) }
    workspace.actions.onClearSelection = { [weak self] in self?.clearSelection() }
    workspace.actions.onSeekSeconds = { [weak self] time in
      guard let self, self.document != nil else { return }
      self.setPlaying(false)
      self.authoredTimeline[self.activeClockID, default: []].append(time)
      self.refreshWorkspace()
      self.requestTime(time)
    }
    workspace.actions.onSetObjectVisible = { [weak self] id, visible in
      self?.setObjectVisibility(id, visible)
    }
    workspace.actions.onSelectObject = { [weak self] id in self?.selectObject(id) }
    workspace.actions.onSelectSelections = { [weak self] selections in
      self?.applySelectionSet(selections)
    }
    workspace.actions.onTrajectoryOptionsChanged = { [weak self] options in
      guard let self, !self.isShuttingDown else { return }
      let densityChanged = self.trajectoryOptions.densityPercent != options.densityPercent
      self.trajectoryOptions = options
      if densityChanged { self.updateTrajectory() }
      self.updateTrajectoryPlayback()
      self.updateCameraOverlays()
    }
    workspace.actions.onCreateCamera = { [weak self] in self?.createCamera() }
    workspace.actions.onRecordCameraKeyframe = { [weak self] in self?.recordCameraKeyframe() }
    workspace.actions.onSaveCameras = { [weak self] in self?.saveCameras() }
    workspace.actions.onImportCameras = { [weak self] in self?.importCameras() }
    workspace.actions.onRenameCamera = { [weak self] in self?.renameCamera($0) }
    workspace.actions.onDeleteCamera = { [weak self] in self?.deleteCamera() }
    workspace.actions.onJumpCameraKey = { [weak self] in self?.jumpCameraKey($0) }
    workspace.actions.onDeleteCameraKey = { [weak self] in self?.deleteCameraKey($0) }
    workspace.actions.onCameraFOV = { [weak self] in self?.keyframeCameraFOV($0) }
    workspace.actions.onUndoCamera = { [weak self] in
      self?.cameraUndo.undo()
      self?.refreshCameraAuthoring()
    }
    workspace.actions.onRedoCamera = { [weak self] in
      self?.cameraUndo.redo()
      self?.refreshCameraAuthoring()
    }
    workspace.actions.onShowCameras = { [weak self] value in
      guard let self else { return }
      if self.cameraFrustaVisible != value { self.invalidateScenePick() }
      self.cameraFrustaVisible = value
      self.updateCameraOverlays()
    }
    workspace.actions.onPlaybackChanged = { [weak self] value in self?.setPlaying(value) }
    workspace.actions.onPlaybackSpeedChanged = { [weak self] value in self?.speed = value }
    workspace.actions.onPreviewRateChanged = { [weak self] value in
      guard let self else { return }
      self.setPlaying(false)
      self.previewRates[self.activeClockID] = value
      self.refreshWorkspace()
      self.workspace.setStatus(
        L10n.format(
          "timeline.rateChanged", "Preview rate %@ %@/s · Source timestamps unchanged.",
          L10n.number(value), L10n.tr("unit." + self.currentTimeUnit(), self.currentTimeUnit())))
    }
    workspace.actions.onLoopChanged = { [weak self] value in self?.loop = value }
    workspace.actions.onSelectID = { [weak self] id in
      guard let self, let scene = self.scene else { return }
      guard let index = scene.index(of: id, objectID: self.selectedObjectID) else {
        self.workspace.setStatus(
          L10n.format(
            "selection.ambiguousID",
            "Identity %@ is absent or ambiguous. Select its source object in the tree.", String(id))
        )
        return
      }
      self.select(index)
    }
    workspace.actions.onOrbitCenterSelection = { [weak self] in self?.centerOrbitOnSelection() }
    workspace.actions.onOrbitCenterView = { [weak self] in self?.centerOrbitOnView() }
    workspace.actions.onTrackballOrbit = { [weak self] enabled in
      self?.renderer?.setTrackballOrbit(enabled)
    }
    workspace.actions.onFocusSelection = { [weak self] in self?.frameSelection() }
    workspace.actions.onResetCamera = { [weak self] in self?.resetCamera() }
    workspace.actions.onSelectCamera = { [weak self] index in
      guard let self else { return }
      let previousClock = self.activeClockID
      let previousSpace = self.activeSpaceID
      if let index, index < self.cameraEntries.count {
        let camera = self.cameraEntries[index].camera
        if let scene = self.scene,
          camera.object.space != scene.object.space
            || (camera.times.count > 1 && camera.object.clock != scene.object.clock)
        {
          self.workspace.setCameraCaption(
            self.selectedCamera.flatMap {
              $0 < self.cameraEntries.count ? self.cameraEntries[$0].name : nil
            } ?? L10n.tr("view.perspective", "Perspective"))
          self.workspace.setStatus(
            L10n.tr(
              "camera.viewportDomainMismatch",
              "Camera and visible geometry have different spaces or clocks. Existing view retained; explicit alignment is required."
            ))
          return
        }
        self.viewportSpace = camera.object.space
        self.viewportClock = camera.object.clock
      }
      self.selectedCamera = index
      if previousSpace != self.activeSpaceID || previousClock != self.activeClockID {
        self.invalidateScenePick()
      }
      if index != nil {self.adoptSourceNearClip()}
      if self.scene == nil { self.refreshWorkspace() }
      if previousClock != self.activeClockID { self.requestTime(self.timeline[0]) }
      self.applyCamera()
      if index != nil { self.centerOrbitOnView() }
    }
    workspace.actions.onOrbit = { [weak self] delta in
      self?.selectedCamera = nil
      self?.renderer?.orbit(delta: delta)
      self?.applyCamera()
    }
    workspace.actions.onPan = { [weak self] delta in
      self?.selectedCamera = nil
      self?.renderer?.pan(delta: delta)
      self?.applyCamera()
    }
    workspace.actions.onDolly = { [weak self] delta in
      self?.selectedCamera = nil
      self?.renderer?.dolly(delta: delta)
      self?.applyCamera()
    }
    workspace.actions.onRoll = { [weak self] value in
      self?.selectedCamera = nil
      self?.renderer?.roll(radians: value)
      self?.applyCamera()
    }
    workspace.actions.onDollyAt = { [weak self] delta, anchor in
      self?.selectedCamera = nil
      self?.renderer?.dolly(delta: delta, anchor: anchor)
      self?.applyCamera()
    }
    workspace.actions.onNavigationBoundary = { [weak self] start in
      if start { self?.renderer?.beginNavigation() } else { self?.renderer?.endNavigation() }
    }
    workspace.actions.onPreviousView = { [weak self] in
      self?.selectedCamera = nil
      _ = self?.renderer?.previousView()
      self?.applyCamera()
    }
    workspace.actions.onNextView = { [weak self] in
      self?.selectedCamera = nil
      _ = self?.renderer?.nextView()
      self?.applyCamera()
    }
    workspace.actions.onUpAxis = { [weak self] axis in
      self?.renderer?.setUpAxis(axis)
      self?.applyCamera()
    }
    workspace.actions.onPick = { [weak self] pixel, operation in self?.pick(pixel, operation: operation) }
    workspace.actions.onRenderOptionsChanged = { [weak self] options in
      self?.applyRenderOptions(options)
    }
    workspace.actions.onExportScreenshot = { [weak self] in self?.saveScreenshot() }
    workspace.actions.onExportWorkspace = { [weak self] in self?.saveWorkspaceSnapshot() }
    workspace.actions.onCloseRequested = { [weak self] in self?.canClose() ?? true }
    workspace.actions.onCameraPreset = { [weak self] name in
      self?.selectedCamera = nil
      self?.renderer?.setCameraPreset(name)
      self?.applyCamera()
    }
    workspace.actions.onShowTrajectory = { [weak self] value in
      self?.trajectoryOptions.showPaths = value
      self?.trajectoryOptions.showTrails = value
      self?.updateTrajectoryPlayback()
    }
    applyRenderOptions(WorkspaceRenderOptions())
    let timer = Timer(timeInterval: 1.0 / 60, repeats: true) { [weak self] _ in self?.tick() }
    RunLoop.main.add(timer, forMode: .common)
    self.timer = timer
  }

  deinit {
    timer?.invalidate()
    ioCancellation.cancel()
    trajectoryCancellation?.cancel()
    comparisonCancellation?.cancel()
  }
  /// Main-thread state advances independently of worker cancellation. Captured
  /// tokens fence queued I/O; generation fences final UI commits and GPU picks.
  private func beginIOGeneration() {
    guard !isShuttingDown else { return }
    ioCancellation.cancel()
    ioCancellation = SceneIOCancellation()
    generation = UUID()
  }
  /// Application termination is a separate lifecycle transition from the user
  /// cancel-loading action, which may resume the current scene's trajectories.
  func shutdown() {
    guard !isShuttingDown else { return }
    isShuttingDown = true
    setPlaying(false)
    timer?.invalidate(); timer = nil
    ioCancellation.cancel(); generation = UUID()
    frameRequestActive = false
    pendingPick = nil
    workspace.viewport.delegate = nil
    // Clear callback ownership before retiring the renderer scene. Already
    // queued completions are also fenced by the guards installed above.
    renderer?.onMetrics = nil; renderer?.onError = nil
    renderer?.onScenePick = nil
    renderer?.clear()
    trajectoryCancellation?.cancel(); trajectoryToken = UUID()
    comparisonCancellation?.cancel(); comparisonRevision = UUID()
    comparisonEnabled = false; comparisonPending = false
  }
  func canClose() -> Bool {
    guard camerasDirty else { return true }
    let alert = NSAlert()
    alert.messageText = L10n.tr("camera.closeUnsaved", "Close with unsaved camera animation?")
    alert.informativeText = L10n.tr(
      "camera.closeUnsavedHint",
      "Cancel and choose Save Camera Animation to preserve your keyframes.")
    alert.addButton(withTitle: L10n.tr("common.cancel", "Cancel"))
    alert.addButton(withTitle: L10n.tr("camera.discardChanges", "Discard Camera Changes"))
    guard alert.runModal() == .alertSecondButtonReturn else { return false }
    camerasDirty = false
    return true
  }

  private func applyRenderOptions(_ options: WorkspaceRenderOptions) {
    var settings = GaussianRenderSettings()
    settings.exposure = log2(max(options.exposure, 0.001))
    settings.opacityScale = options.opacityScale
    settings.gaussianScale = options.gaussianScale
    settings.background = options.background
    settings.showGrid = options.showGrid
    settings.showOrbitCenter = options.showOrbitCenter
    settings.nearClip = options.nearClip
    renderer?.updateSettings(settings)
    workspace.viewport.setRenderScale(options.resolutionScale)
  }

  private func setPlaying(_ value: Bool) {
    playing = !isShuttingDown && value && timeline.count > 1
    lastTick = CACurrentMediaTime()
    workspace.setPlayback(playing)
  }
  private func tick() {
    let now = CACurrentMediaTime()
    defer { lastTick = now }
    guard playing else { return }
    var time = targetTime + (now - lastTick) * speed * previewRate
    let start = max(timeline[0], previewRange?.lowerBound ?? timeline[0])
    let end = min(timeline.last!, previewRange?.upperBound ?? timeline.last!)
    guard end > start else {
      setPlaying(false)
      return
    }
    if time < start { time = start }
    if time > end {
      if loop {
        time = start + (time - start).truncatingRemainder(dividingBy: end - start)
      } else {
        time = end
        setPlaying(false)
      }
    }
    requestTime(time)
  }

  func open(_ url: URL) {
    guard !isShuttingDown else { return }
    guard let renderer else {
      workspace.presentError(
        message: L10n.tr("error.metalUnavailable", "Metal renderer is unavailable."))
      return
    }
    guard !cameraFilePanelOpen else {
      workspace.setStatus(
        L10n.tr(
          "camera.finishDialog", "Finish the active camera file dialog before switching scenes."))
      return
    }
    if camerasDirty {
      let alert = NSAlert()
      alert.messageText = L10n.tr(
        "camera.openUnsaved", "Open another scene with unsaved camera animation?")
      alert.informativeText = L10n.tr(
        "camera.openUnsavedHint",
        "Cancel and use Save Camera Animation to preserve your camera keyframes.")
      alert.addButton(withTitle: L10n.tr("common.cancel", "Cancel"))
      alert.addButton(withTitle: L10n.tr("camera.discardChanges", "Discard Camera Changes"))
      if alert.runModal() != .alertSecondButtonReturn { return }
    }
    toggleComparison(false)
    cancelLoading(resumeTrajectory: false)
    setPlaying(false)
    let token = generation
    let cancellation = ioCancellation
    let revision = cameraRevision
    comparisonGeometryPending = true
    workspace.setLoading(
      message: L10n.format("loading.verifying", "Verifying %@…", url.lastPathComponent))
    let device = renderer.device
    io.submit(cancellation: cancellation) { [weak self] in
      do {
        try cancellation.check()
        let document = try GSVDocument(url: url)
        try cancellation.check()
        let cameras = try document.objects.filter {
          $0.type == "camera" && $0.encoding == "gsv.camera.pinhole"
        }.map { try cancellation.check(); return try GSVCamera(document: document, object: $0) }
        let radiometry = try document.objects.filter {$0.encoding == "gsv.appearance.camera-affine"}.map {
          try cancellation.check(); return try GSVCameraRadiometry(document:document,object:$0,cameras:cameras)
        }
        let observations = try document.objects.filter {$0.encoding == "gsv.observation.camera-rgb"}.map {
          try cancellation.check(); return try GSVCameraObservation(document:document,object:$0,cameras:cameras)
        }
        let profiles = try document.objects.filter{$0.encoding == "gsv.render.profile"}.map{try cancellation.check(); return try GSVRenderProfile(object:$0)}
        let renderViews = try document.objects.filter{$0.encoding == "gsv.render.view"}.map{
          try cancellation.check(); return try GSVRenderView(document:document,object:$0,cameras:cameras,profiles:profiles,radiometry:radiometry,observations:observations)
        }
        var renderFields = [String:StreamingScene]()
        for id in Set(renderViews.flatMap{$0.fieldIDs}) {
          try cancellation.check()
          if let object = document.objects.first(where:{$0.id == id}),StreamingScene.supports(object) {
            renderFields[id] = try StreamingScene(document:document,object:object,device:device)
          }
        }
        let supported = document.objects.filter(StreamingScene.supports)
        let primary = supported.filter { $0.type == "gaussian" }
        let candidates = primary.isEmpty ? supported : primary
        // Independent spaces/clocks are peer objects, not implicit transforms.
        // Open one compatible cohort; the outliner retains the other objects.
        let basis = candidates.first
        let defaultFields = Set(renderViews.first?.fieldIDs ?? [])
        let visible = !defaultFields.isEmpty ? defaultFields : Set(candidates.filter {
          $0.space == basis?.space && ($0.clock == basis?.clock || $0.clock == nil)
        }.map { $0.id })
        let loaded = try Self.geometry(document, visible: visible, device: device, cancellation: cancellation)
        try cancellation.check()
        let first = try loaded.flatMap { $0.count > 0 ? try $0.frame(0) : nil }
        try cancellation.check()
        let second = try loaded.flatMap {
          $0.count > 0 ? try $0.frame(min(1, $0.frameCount - 1)) : nil
        }
        try cancellation.check()
        DispatchQueue.main.async { [weak self] in
          guard let self, !cancellation.isCancelled, self.generation == token else { return }
          guard self.cameraRevision == revision, !self.cameraFilePanelOpen else {
            self.comparisonGeometryPending = false
            self.beginIOGeneration()
            self.workspace.setLoading(message: nil)
            self.workspace.setStatus(
              L10n.tr(
                "loading.cameraChanged",
                "Scene loading canceled because camera editing continued. Your current camera changes were retained."
              ))
            return
          }
          self.comparisonGeometryPending = false
          self.document = document
          self.scene = loaded
          self.visibleObjectIDs = visible
          self.selectionSet = StudioSelectionSet()
          self.pendingPick = nil
          self.selectedObjectID = loaded?.object.id
          self.committedTime = loaded?.times.first ?? cameras.first?.times.first ?? 0
          self.targetTime = self.committedTime
          self.sourceCameras = cameras
          self.observations = observations
          self.radiometry = radiometry
          self.renderProfiles = profiles;self.renderViews = renderViews;self.renderFields = renderFields
          self.comparisonEntries = renderViews.flatMap { binding in
            observations.filter{binding.observationIDs.contains($0.object.id)}.flatMap { observation in
              observation.images.indices.filter{observation.views[$0] == binding.cameraView}.map { row in
                let view = observation.views[row],time = observation.times[row]
                return WorkspaceComparisonEntry(observationID:observation.object.id,row:row,cameraID:observation.camera.object.id,cameraView:view,time:time,
                  label:binding.object.label + " · " + observation.camera.viewIDs[view] + " · t=" + L10n.time(time),renderViewID:binding.object.id,renderViewLabel:binding.object.label)
              }
            }
          }
          self.comparisonIndex = nil
          self.workspace.resetComparisonDocument()
          self.workspace.updateComparisons(self.comparisonEntries)
          self.authoredCameras = []
          self.authoredTimeline = [:]
          self.editingCameraID = nil
          self.camerasDirty = false
          self.cameraUndo.removeAllActions()
          self.authoredSpaces = [:]
          self.authoredClocks = [:]
          self.viewportSpace = loaded?.object.space ?? cameras.first?.object.space
          self.viewportClock = loaded?.object.clock ?? cameras.first?.object.clock
          self.preparedA = first
          self.preparedB = second
          self.currentA = nil
          self.currentB = nil
          self.currentLower = 0
          self.frameRequestActive = false
          self.selectedIndex = nil
          self.selectedCamera = nil
          self.inspectedCamera = nil
          self.previewRange = nil
          self.renderer?.clear()
          self.trajectoryToken = UUID()
          self.rebuildCameraEntries()
          self.refreshWorkspace()
          self.resetCamera()
          self.renderer?.clearNavigationHistory()
          self.workspace.setLoading(message: nil)
          self.requestTime(self.timeline[0])
          self.updateCameraOverlays()
          if let id = loaded?.object.id { self.selectObject(id) }
          NSDocumentController.shared.noteNewRecentDocumentURL(url)
        }
      } catch {
        guard !cancellation.isCancelled else { return }
        DispatchQueue.main.async { [weak self] in
          guard let self, !cancellation.isCancelled, self.generation == token else { return }
          self.comparisonGeometryPending = false
          self.workspace.setLoading(message: nil)
          self.workspace.presentError(message: error.localizedDescription)
        }
      }
    }
  }
  /// A source-camera view only adopts an explicitly bound center-depth policy.
  private func adoptSourceNearClip() {
    guard let index = selectedCamera,cameraEntries.indices.contains(index) else {return}
    let camera = cameraEntries[index]
    let profiles = renderViews.filter{$0.camera.object.id == camera.camera.object.id && $0.cameraView == camera.view}.map{$0.profile}
    if let profile = profiles.first,profiles.allSatisfy({$0.object.id == profile.object.id}) {
      workspace.renderOptions.nearClip = max(profile.near,Float.leastNormalMagnitude)
      workspace.nearClipField.stringValue = L10n.number(Double(profile.near),digits:6)
      applyRenderOptions(workspace.renderOptions)
    }
  }
  private func toggleComparison(_ enabled: Bool) {
    if enabled {
      guard !comparisonEntries.isEmpty else { return }
      let camera = selectedCamera.flatMap {cameraEntries.indices.contains($0) ? cameraEntries[$0] : nil}
      let exact = camera.flatMap {current in comparisonEntries.firstIndex {
        $0.cameraID == current.camera.object.id && $0.cameraView == current.view && $0.time == targetTime
      }}
      selectComparison(exact ?? comparisonIndex ?? 0)
    } else {
      previewRenderViewID = nil
      comparisonEnabled = false; comparisonRevision = UUID(); comparisonCancellation?.cancel(); comparisonPending = false
      workspace.setComparisonActive(false)
      refreshWorkspace()
      requestTime(targetTime)
      renderer?.requestDraw()
    }
  }
  private func selectComparison(_ index: Int) {
    guard comparisonEntries.indices.contains(index) else { return }
    setPlaying(false)
    let entry = comparisonEntries[index]
    previewRenderViewID = nil
    comparisonIndex = index; comparisonEnabled = true
    adoptSourceNearClip()
    workspace.setComparisonActive(true, selected:index)
    selectedCamera = cameraEntries.firstIndex {$0.camera.object.id == entry.cameraID && $0.view == entry.cameraView}
    if let observation = observations.first(where:{$0.object.id == entry.observationID}) {
      viewportSpace = observation.object.space; viewportClock = observation.object.clock
    }
    // Preserve the exact observation time, including times outside geometry support.
    if !timeline.contains(entry.time) {authoredTimeline[activeClockID,default:[]].append(entry.time)}
    refreshWorkspace()
    requestTime(entry.time)
  }
  private func activateRenderPreview(_ id:String) {
    guard let binding = renderViews.first(where:{$0.object.id == id}) else {return}
    if let index = comparisonEntries.firstIndex(where:{$0.renderViewID == id && $0.time == targetTime}) ?? comparisonEntries.firstIndex(where:{$0.renderViewID == id}) {selectComparison(index);return}
    setPlaying(false);previewRenderViewID = id;comparisonEnabled = true;comparisonIndex = nil
    selectedCamera = cameraEntries.firstIndex{$0.camera.object.id == binding.camera.object.id && $0.view == binding.cameraView}
    viewportSpace = binding.camera.object.space;viewportClock = binding.camera.object.clock
    workspace.setComparisonActive(true)
    workspace.comparisonView.previewViews(renderViews.map{($0.object.id,$0.object.label)},selected:id)
    refreshWorkspace()
    requestTime(targetTime)
  }
  private func scheduleComparison() {
    guard !isShuttingDown else { return }
    if previewRenderViewID != nil {
      comparisonCancellation?.cancel();comparisonRevision = UUID();comparisonPending = true
      workspace.comparisonPending(L10n.tr("render.previewPending","Rendering the selected view with its explicit output and profile…"))
      startComparisonIfNeeded();return
    }
    guard comparisonEnabled, let index = comparisonIndex, comparisonEntries.indices.contains(index) else {return}
    comparisonCancellation?.cancel(); comparisonRevision = UUID(); comparisonPending = false
    let selected = comparisonEntries[index]
    guard let observation = observations.first(where:{$0.object.id == selected.observationID}),
      let row = observation.exactRow(time:targetTime,view:selected.cameraView) else {
      workspace.comparisonPending(L10n.tr("compare.unmatched", "No exact observation at this time and camera view. Metrics are unavailable; no nearest image is substituted."))
      return
    }
    if let matchedIndex = comparisonEntries.firstIndex(where:{$0.observationID == selected.observationID && $0.row == row && $0.renderViewID == selected.renderViewID}) {
      comparisonIndex = matchedIndex
      workspace.comparisonSelection = matchedIndex
      workspace.comparisonView.entries(comparisonEntries,selected:matchedIndex)
    }
    workspace.comparisonPending(L10n.tr("compare.pending", "Rendering exact camera rays and computing full-resolution metrics…"))
    comparisonPending = true
    startComparisonIfNeeded()
  }
  private func startComparisonIfNeeded() {
    guard !isShuttingDown else { return }
    if previewRenderViewID != nil {startRenderPreviewIfNeeded();return}
    guard comparisonEnabled, comparisonPending, !comparisonBusy, !comparisonGeometryPending,
      let index = comparisonIndex, comparisonEntries.indices.contains(index),let renderer else {return}
    let entry = comparisonEntries[index]
    guard let observation = observations.first(where:{$0.object.id == entry.observationID}) else {return}
    comparisonPending = false
    guard let binding = renderViews.first(where:{$0.object.id == entry.renderViewID}) else {
      workspace.comparisonPending(L10n.tr("render.noBinding","No explicit render-view binding selects this observation."));return
    }
    if let issue = binding.comparisonIssue(observation) {workspace.comparisonPending(issue);return}
    let children = binding.fieldIDs.compactMap{renderFields[$0]}
    guard children.count == binding.fieldIDs.count else {
      workspace.comparisonPending(L10n.tr("render.fieldsUnsupported","This render view includes a field encoding that Studio cannot evaluate. No cache or representation is substituted."));return
    }
    var gain = SIMD3<Float>(repeating:1),bias = SIMD3<Float>.zero
    var radiometryLabel = L10n.tr("compare.noRadiometry", "No camera RGB correction")
    if let appearance = binding.appearance {
      let values = appearance.at(sample:observation.samples[entry.row],view:entry.cameraView)
      gain = values.gain;bias = values.bias;radiometryLabel = appearance.object.label
    }
    let time = observation.times[entry.row]
    guard children.allSatisfy({$0.times.contains(time) || $0.object.encoding == "gsv.gaussian.static"}) else {
      workspace.comparisonPending(L10n.tr("compare.geometryUnmatched", "No exact geometry sample at this observation time. Metrics never use interpolated or endpoint-clamped trajectories. A static Gaussian encoding is valid at every time."));return
    }
    let bg = binding.background,profile = binding.profile
    let interval:(near:Float,far:Float?,nearInclusive:Bool,farInclusive:Bool)
    do {interval = try profile.centerInterval(camera:observation.camera,sample:observation.samples[entry.row],view:entry.cameraView)}
    catch {workspace.comparisonPending(error.localizedDescription);return}
    let clip = interval.near
    let sourcePolicy = profile.capabilityDetails + "\n" + L10n.format("render.boundFields","Render view %@ · Bound fields: %@",binding.object.label,binding.fieldIDs.joined(separator:", "))
      + (observation.camera.acquisition.isEmpty ? "" : "\n" + L10n.tr("render.captureFacts","Acquisition exposure/shutter are stored capture facts; this view renders one instant without exposure integration."))
    let token = comparisonRevision, cancellation = ComparisonCancellation()
    comparisonCancellation = cancellation; comparisonBusy = true
    let colorGain = gain, colorBias = bias, appearanceLabel = radiometryLabel
    let pose = observation.camera.at(time:time,view:entry.cameraView)
    let count = children.reduce(0){$0+$1.count}
    comparisonQueue.async { [weak self] in
      guard let self else {return}
      func finish(_ result:Result<(CGImage,CGImage,RGBQualityMetrics),Error>) {
        DispatchQueue.main.async {
          self.comparisonBusy = false
          if self.comparisonEnabled, self.comparisonRevision == token, !cancellation.cancelled {
            switch result {
            case .success(let (render,gt,metrics)):
              func vector(_ value:SIMD3<Float>)->String {(0..<3).map{L10n.number(Double(value[$0]),digits:6)}.joined(separator:", ")}
              let summary = observation.object.label + "\n" + observation.camera.object.label + " · v\(entry.cameraView) · t=" + L10n.time(time)
                + "\n" + L10n.format("compare.colorDomain", "Color domain %@ · RGB [0,1]", observation.colorSpace)
              let context = summary + "\n" + sourcePolicy
                + "\n" + L10n.format("compare.backgroundResolved", "Background RGB %@", vector(bg))
                + "\n" + L10n.format("render.centerInterval", "Gaussian-center depth: %@ %@ z %@ %@",L10n.number(Double(clip),digits:6),interval.nearInclusive ? "≤" : "<",interval.farInclusive ? "≤" : "<",interval.far.map{L10n.number(Double($0),digits:6)} ?? "∞")
                + "\n" + appearanceLabel + "\n" + L10n.format("compare.affineResolved", "RGB gain %@ · bias %@", vector(colorGain), vector(colorBias))
                + "\n" + L10n.format("compare.splitResolved", "Observation split: %@", observation.object.metadata["evaluation_split"] as? String ?? L10n.tr("compare.unspecified", "unspecified"))
              self.workspace.comparisonFinished(render:render,gt:gt,color:observation.colorSpace,role:observation.role,metrics:metrics,context:context,summary:summary)
            case .failure(let error): self.workspace.comparisonPending(L10n.tr("compare.failed", "Comparison unavailable: ") + error.localizedDescription)
            }
          }
          self.startComparisonIfNeeded()
        }
      }
      do {
        guard !cancellation.cancelled else {throw CancellationError()}
        let reference = try observation.read(entry.row)
        guard count <= renderer.device.maxBufferLength/64,let buffer = renderer.device.makeBuffer(length:max(16,count*64),options:.storageModeShared) else {throw SceneError.invalid("Cannot allocate comparison Gaussian buffer.")}
        var offset = 0
        for child in children {
          guard !cancellation.cancelled else {throw CancellationError()}
          if child.count > 0 {
            let frame = try child.frame(child.times.firstIndex(of:time) ?? 0)
            memcpy(buffer.contents().advanced(by:offset*64),frame.contents(),child.count*64)
            offset += child.count
          }
        }
        renderer.captureComparison(a:buffer,b:buffer,count:count,interpolation:0,pose:pose.pose,intrinsics:pose.k,
          width:reference.width,height:reference.height,integerPixelCenters:observation.camera.integerPixelCenters,background:bg,nearClip:clip,farClip:interval.far,nearInclusive:interval.nearInclusive,farInclusive:interval.farInclusive,
          pixelOffset:profile.pixelOffset,divideEpsilon:profile.epsilon,jacobianMode:profile.jacobianMode,jacobianFactor:profile.jacobianFactor,covarianceDilation:profile.dilation,colorGain:colorGain,colorBias:colorBias,cancellation:cancellation) {result in
          self.comparisonQueue.async {
            do {
              let rgb = try result.get()
              guard !cancellation.cancelled else {throw CancellationError()}
              let metrics = try RGBImageQuality.compare(rgb,reference.rgb,width:reference.width,height:reference.height,mask:reference.mask,cancelled:{cancellation.cancelled})
              let rendered = RGBObservationPixels(width:reference.width,height:reference.height,rgb:rgb,mask:nil,colorSpace:reference.colorSpace)
              finish(.success((try rendered.image(),try reference.image(),metrics)))
            } catch {finish(.failure(error))}
          }
        }
      } catch {finish(.failure(error))}
    }
  }

  private func startRenderPreviewIfNeeded() {
    guard !isShuttingDown, comparisonEnabled,comparisonPending,!comparisonBusy,!comparisonGeometryPending,
      let id = previewRenderViewID,let binding = renderViews.first(where:{$0.object.id == id}),let renderer else {return}
    comparisonPending = false
    guard binding.camera.supportsCalibratedRendering else {workspace.comparisonPending(L10n.tr("render.distortionUnsupported","This camera's distortion is stored and inspectable, but calibrated rendering is unsupported."));return}
    guard binding.temporalMode == "instantaneous" else {workspace.comparisonPending(L10n.tr("render.integrationUnsupported","Temporal shutter integration is stored but not implemented by this renderer."));return}
    guard binding.identityOutput else {workspace.comparisonPending(L10n.tr("render.outputPreviewUnsupported","This output transform is stored and inspectable. Calibrated preview currently requires the native camera raster; source-raster support mapping is not approximated silently."));return}
    let time = targetTime,children = binding.fieldIDs.compactMap{renderFields[$0]}
    guard children.count == binding.fieldIDs.count else {workspace.comparisonPending(L10n.tr("render.fieldsUnsupported","This render view includes a field encoding that Studio cannot evaluate. No cache or representation is substituted."));return}
    guard let sample = binding.camera.times.count == 1 ? 0 : binding.camera.times.firstIndex(of:time),children.allSatisfy({$0.object.encoding == "gsv.gaussian.static" || $0.times.contains(time)}) else {
      workspace.comparisonPending(L10n.tr("render.previewExactTime","This render view requires an exact camera calibration and geometry sample at the playhead. No endpoint substitution is used."));return
    }
    let policy = binding.profile,interval:(near:Float,far:Float?,nearInclusive:Bool,farInclusive:Bool)
    do {interval = try policy.centerInterval(camera:binding.camera,sample:sample,view:binding.cameraView)} catch {workspace.comparisonPending(error.localizedDescription);return}
    let camera = binding.camera.at(time:time,view:binding.cameraView),k = binding.transform*camera.k
    let affine = binding.appearance?.at(sample:sample,view:binding.cameraView) ?? (gain:SIMD3<Float>(repeating:1),bias:SIMD3<Float>.zero)
    let offset = SIMD2(policy.pixelOffset.x*binding.transform[0][0],policy.pixelOffset.y*binding.transform[1][1])
    let count = children.reduce(0){$0+$1.count},token = comparisonRevision,cancellation = ComparisonCancellation()
    comparisonBusy = true;comparisonCancellation = cancellation
    comparisonQueue.async {[weak self] in
      guard let self else {return}
      func finish(_ result:Result<CGImage,Error>) {
        DispatchQueue.main.async {
          self.comparisonBusy = false
          if self.comparisonEnabled,self.previewRenderViewID == id,self.comparisonRevision == token,!cancellation.cancelled {
            switch result {
            case .success(let image):
              self.workspace.comparisonMetrics = nil
              self.workspace.comparisonSummary = binding.object.label + "\n" + binding.camera.object.label + " · v\(binding.cameraView) · t=" + L10n.time(time)
                + "\n" + L10n.format("compare.colorDomain", "Color domain %@ · RGB [0,1]", binding.colorSpace)
              self.workspace.comparisonMessage = policy.capabilityDetails + "\n" + L10n.format("render.boundFields","Render view %@ · Bound fields: %@",binding.object.label,binding.fieldIDs.joined(separator:", ")) + "\n" + L10n.tr("render.previewDisplay","No reference is scored. The preview display clamps RGB to [0,1]; the stored output policy and source arrays remain unchanged.")
              self.workspace.comparisonView.showPreview(image,color:binding.colorSpace);self.workspace.rebuildInspector()
            case .failure(let error): self.workspace.comparisonPending(error.localizedDescription)
            }
          };self.startComparisonIfNeeded()
        }
      }
      do {
        guard !cancellation.cancelled,count <= renderer.device.maxBufferLength/64,let buffer = renderer.device.makeBuffer(length:max(16,count*64),options:.storageModeShared) else {throw SceneError.invalid("Render-view geometry exceeds native capacity.")}
        var destination = 0
        for child in children where child.count > 0 {
          guard !cancellation.cancelled else {throw CancellationError()}
          let frame = try child.frame(child.times.firstIndex(of:time) ?? 0)
          memcpy(buffer.contents().advanced(by:destination*64),frame.contents(),child.count*64);destination += child.count
        }
        renderer.captureComparison(a:buffer,b:buffer,count:count,interpolation:0,pose:camera.pose,intrinsics:k,width:binding.imageSize.x,height:binding.imageSize.y,integerPixelCenters:binding.camera.integerPixelCenters,
          background:binding.background,nearClip:interval.near,farClip:interval.far,nearInclusive:interval.nearInclusive,farInclusive:interval.farInclusive,pixelOffset:offset,divideEpsilon:policy.epsilon,jacobianMode:policy.jacobianMode,jacobianFactor:policy.jacobianFactor,covarianceDilation:policy.dilation,clampOutput:binding.clamp == "unit",colorGain:affine.gain,colorBias:affine.bias,cancellation:cancellation) { result in
            do {let rgb = try result.get();guard !cancellation.cancelled else {throw CancellationError()}
              finish(.success(try RGBObservationPixels(width:binding.imageSize.x,height:binding.imageSize.y,rgb:rgb,mask:nil,colorSpace:binding.colorSpace).image()))
            } catch {finish(.failure(error))}
          }
      }catch {finish(.failure(error))}
    }
  }

  private func sceneStatus(_ scene: GeometryScene) -> String {
    if scene.isScaffold {
      return L10n.format(
        "scene.scaffoldStatus",
        "Scaffold nodes and topology · %@ persistent node IDs · Glyph size is a display setting",
        L10n.number(Double(scene.count), digits: 0))
    }
    return L10n.format(
      "scene.geometryStatus",
      "%@ visible objects · %@ identities",
      L10n.number(Double(visibleObjectIDs.count), digits: 0),
      L10n.number(Double(scene.count), digits: 0))
  }
  private static func geometry(_ document: GSVDocument, visible: Set<String>, device: MTLDevice,
    cancellation: SceneIOCancellation? = nil) throws -> GeometryScene?
  {
    let children = try document.objects.filter {
      visible.contains($0.id) && StreamingScene.supports($0)
    }.map {
      try cancellation?.check()
      return try StreamingScene(document: document, object: $0, device: device)
    }
    try cancellation?.check()
    if children.isEmpty { return nil }
    if children.count == 1 { return children[0] }
    return try CompositeScene(children: children, device: device)
  }
  private func cancelLoading(resumeTrajectory: Bool = true) {
    guard !isShuttingDown else { return }
    comparisonGeometryPending = false
    beginIOGeneration()
    frameRequestActive = false
    trajectoryCancellation?.cancel()
    trajectoryToken = UUID()
    workspace.setLoading(message: nil)
    workspace.setStatus(
      document == nil
        ? L10n.tr("document.openToBegin", "Open a GSV document to begin.")
        : L10n.tr("document.ready", "Document ready."))
    if resumeTrajectory { updateTrajectory() }
  }
  private func refreshWorkspace() {
    guard let document else { return }
    let boundTimes = activeRenderBinding.map {binding in
      binding.fieldIDs.compactMap{renderFields[$0]}.filter{$0.object.encoding != "gsv.gaussian.static"}.flatMap{$0.times} + binding.camera.times
    }
    let times = boundTimes ?? (
      scene?.times.isEmpty == false
      ? scene!.times
      : (selectedCamera.flatMap { $0 < cameraEntries.count ? cameraEntries[$0].camera.times : nil }
        ?? cameraEntries.first(where: {
          $0.camera.object.space == activeSpaceID && $0.camera.object.clock == activeClockID
        })?.camera.times ?? [0]))
    var sampleTimes = Set<Double>(times)
    if sampleTimes.isEmpty { sampleTimes.insert(0) }
    sampleTimes.formUnion(authoredTimeline[activeClockID] ?? [])
    for observation in observations where observation.object.clock == activeClockID {
      sampleTimes.formUnion(observation.times)
    }
    for camera in authoredCameras where camera.clock == activeClockID {
      for key in camera.keyframes { sampleTimes.insert(key.time) }
    }
    timeline = sampleTimes.sorted()
    var objects = document.objects.map { object in
      WorkspaceObjectInfo(
        id: object.id, label: object.label, type: object.type, encoding: object.encoding,
        supported: StreamingScene.supports(object) || ["gsv.camera.pinhole","gsv.observation.camera-rgb","gsv.appearance.camera-affine","gsv.render.profile","gsv.render.view"].contains(object.encoding),
        visible: StreamingScene.supports(object) ? visibleObjectIDs.contains(object.id) : nil,
        space: object.space, clock: object.clock, role: (object.data["role"] as? String) ?? (object.metadata["role"] as? String),
        cameraIndices: cameraEntries.indices.filter {
          cameraEntries[$0].camera.object.id == object.id
        })
    }
    objects += authoredCameras.map { camera in
      WorkspaceObjectInfo(
        id: camera.id, label: camera.label, type: "camera", encoding: "gsv.camera.pinhole",
        supported: true, space: camera.space, clock: camera.clock, role: "user", authored: true,
        cameraIndices: cameraEntries.indices.filter {
          cameraEntries[$0].camera.object.id == camera.id
        })
    }
    let status =
      scene.map(sceneStatus)
      ?? (document.objects.isEmpty && authoredCameras.isEmpty
        ? L10n.tr(
          "document.empty", "Empty GSV document · Create a camera or open another document.")
        : L10n.tr(
          "document.inspectOnly",
          "No visible geometry."))
    workspace.updateScene(
      WorkspaceSceneInfo(
        name: document.url.deletingPathExtension().lastPathComponent, url: document.url,
        pointCount: scene?.count ?? 0, frameCount: timeline.count, startTime: timeline[0],
        endTime: timeline.last!, cameraNames: cameraEntries.map { $0.name },
        objects: objects, activeObjectID: selectedObjectID, timeUnit: currentTimeUnit(),
        previewRate: previewRate, description: status, times: timeline,
        activeSpace: activeSpaceID ?? "—", activeClock: activeClockID,
        spaceDescription: domainDescription("spaces", id: activeSpaceID),
        clockDescription: domainDescription("clocks", id: activeClockID)))
    workspace.updateFrame(index: lowerFrame(committedTime, timeline), time: committedTime)
    workspace.setSelectionPresentation(selectionSet)
    workspace.setStatus(status)
    refreshCameraAuthoring()
    applyCamera()
    refreshInspectedCamera()
  }
  private func domainDescription(_ kind: String, id: String?) -> String {
    guard let id else { return "—" }
    let supplemental = kind == "spaces" ? authoredSpaces : authoredClocks
    guard
      let value = ((document?.manifest[kind] as? [String: Any])?[id] ?? supplemental[id])
        as? [String: Any]
    else { return "—" }
    let unit = value["unit"] as? String ?? "unitless"
    if kind == "spaces" {
      let hand = value["handedness"] as? String ?? "unknown"
      let axes = (value["axes"] as? [String] ?? ["x", "y", "z"]).joined(separator: " · ")
      return L10n.format(
        "coordinate.summary", "Units: %@\nHandedness: %@\nAxes: %@", L10n.tr("unit." + unit, unit),
        L10n.tr("coordinate." + hand, hand), axes)
    }
    if let mapping = value["mapping"] as? [String: Any], let target = mapping["target"] as? String {
      return L10n.format(
        "clock.mapping",
        "Time maps to %@ using scale %@ and offset %@. Stored mapping is preserved; viewport composition requires a matching clock.",
        target, String(describing: mapping["scale"] ?? "—"),
        String(describing: mapping["offset"] ?? "—"))
    }
    return L10n.tr(
      "clock.native",
      "Source timestamps are used directly in the declared clock. No capture rate is inferred.")
  }
  private func clearSelection() {
    applySelectionSet(StudioSelectionSet())
  }
  private func selectObject(_ id: String) {
    let indices = cameraEntries.indices.filter { cameraEntries[$0].camera.object.id == id }
    let item: StudioSelection = indices.count == 1 ? .camera(id, indices[0]) : .object(id)
    applySelectionSet(StudioSelectionSet(items: [item], primary: item))
  }
  private func inspectPrimaryObject(_ id: String) {
    guard let document else { return }
    workspace.selectObjectPresentation(id)
    if let authored = authoredCameras.first(where: { $0.id == id }) {
      workspace.inspectObject(
        L10n.format(
          "camera.authoredDescriptor", "ID: %@\nSpace: %@\nClock: %@\nKeys: %d", authored.id,
          authored.space, authored.clock, authored.keyframes.count))
    } else if let object = document.objects.first(where: { $0.id == id }) {
      let text =
        (try? JSONSerialization.data(
          withJSONObject: object.descriptor, options: [.prettyPrinted, .sortedKeys])).flatMap {
          String(data: $0, encoding: .utf8)
        } ?? object.label
      let profileDetails = renderProfiles.first(where:{$0.object.id == id})?.capabilityDetails
        ?? renderViews.first(where:{$0.object.id == id}).map{$0.profile.capabilityDetails + "\n" + L10n.format("render.boundFields","Render view %@ · Bound fields: %@",$0.object.label,$0.fieldIDs.joined(separator:", "))}
      workspace.inspectObject((profileDetails.map{$0 + "\n\n"} ?? "") + text)
      workspace.setStatus("\(object.label) · \(object.encoding)")
    }
  }
  private func inspectCamera(_ index: Int) {
    guard cameraEntries.indices.contains(index) else { return }
    let item = StudioSelection.camera(cameraEntries[index].camera.object.id, index)
    applySelectionSet(StudioSelectionSet(items: [item], primary: item))
  }
  private func applySelectionSet(_ requested: StudioSelectionSet) {
    guard !isShuttingDown, let document else { return }
    let objectIDs = Set(document.objects.map(\.id) + authoredCameras.map(\.id))
    let items = requested.items.filter { item in
      switch item {
      case .document: return false
      case .object(let id): return objectIDs.contains(id)
      case .element(let id, let identity): return scene?.index(of: identity, objectID: id) != nil
      case .camera(let id, let entry):
        return cameraEntries.indices.contains(entry) && cameraEntries[entry].camera.object.id == id
      }
    }
    let previous = selectionSet
    selectionSet = StudioSelectionSet(items: items, primary: requested.primary)
    selectionRevision &+= 1
    pendingPick = nil
    selectedObjectID = selectionSet.primary.objectID
    selectedIndex = nil
    inspectedCamera = nil
    switch selectionSet.primary {
    case .element(let id, let identity): selectedIndex = scene?.index(of: identity, objectID: id)
    case .camera(_, let entry): inspectedCamera = entry
    case .object(let id):
      let views = cameraEntries.indices.filter { cameraEntries[$0].camera.object.id == id }
      if views.count == 1 { inspectedCamera = views[0] }
    case .document: break
    }
    let indices = selectionSet.items.compactMap { item -> UInt32? in
      guard case .element(let id, let identity) = item,
        let index = scene?.index(of: identity, objectID: id) else { return nil }
      return UInt32(index)
    }
    renderer?.select(indices: indices)
    editingCameraID = authoredCameras.first { $0.id == selectedObjectID }?.id
    workspace.setSelectionPresentation(selectionSet)
    refreshCameraAuthoring()
    switch selectionSet.primary {
    case .document: workspace.clearSelectionPresentation()
    case .object(let id):
      inspectPrimaryObject(id)
      if inspectedCamera != nil { refreshInspectedCamera() }
    case .element: updateSelection()
    case .camera: refreshInspectedCamera()
    }
    // Selection owns the baseline status. A trajectory task may temporarily
    // publish progress or its result only while that same selection is current.
    refreshSelectionStatus()
    if trajectorySelectionSignature(previous) != trajectorySelectionSignature(selectionSet) { updateTrajectory() }
    updateCameraOverlays()
  }
  private var trajectoryObjectIDs: Set<String> {
    let objects = (scene as? CompositeScene)?.children.map(\.object)
      ?? scene.map { [$0.object] } ?? []
    return Set(objects.filter { $0.type == "gaussian" }.map(\.id))
  }
  private func refreshSelectionStatus() {
    let primary = workspace.selectionItemTitle(selectionSet.primary)
    if selectionSet.items.count > 1 {
      workspace.setStatus(L10n.format("selection.multiple", "%d selected", selectionSet.items.count)
        + " · " + L10n.format("selection.primary", "Primary: %@", primary))
    } else {
      workspace.setStatus(primary)
    }
  }
  private func trajectorySelectionSignature(_ selections: StudioSelectionSet) -> Set<StudioSelection> {
    let objectIDs = trajectoryObjectIDs
    return Set(selections.items.filter { item in
      switch item {
      case .object(let id): return objectIDs.contains(id)
      case .element: return true
      case .document, .camera: return false
      }
    })
  }
  private func refreshInspectedCamera() {
    guard let index = inspectedCamera, cameraEntries.indices.contains(index) else { return }
    let entry = cameraEntries[index]
    let camera = entry.camera
    // A foreign clock cannot be silently interpreted as active document time.
    let inspectionTime = camera.object.clock == activeClockID ? committedTime : camera.times.first ?? 0
    let value = camera.at(time: inspectionTime, view: entry.view)
    workspace.inspectCamera(
      WorkspaceCameraInspection(
        objectID: camera.object.id, entry: index, name: entry.name, pose: value.pose,
        intrinsics: value.k, size: value.size, space: camera.object.space ?? "—",
        clock: camera.object.clock ?? "—",
        role: camera.object.metadata["role"] as? String ?? "reference",
        integerPixelCenters: camera.integerPixelCenters,
        authored: authoredCameras.contains { $0.id == camera.object.id },
        sampleTime: inspectionTime, followsActiveClock: camera.object.clock == activeClockID,
        captureDetails:cameraDetails(camera,view:entry.view,time:inspectionTime)))
  }
  private func cameraDetails(_ camera:GSVCamera,view:Int,time:Double)->String {
    let sample = camera.times.indices.min(by:{abs(camera.times[$0]-time) < abs(camera.times[$1]-time)}) ?? 0
    let i = sample*camera.viewCount+view
    var lines = [L10n.format("camera.stableView","View identity: %@",camera.viewIDs[view]),
      L10n.format("camera.distortion","Distortion model: %@",camera.distortionModel)]
    if !camera.supportsCalibratedRendering {lines.append(L10n.tr("render.distortionUnsupported","This camera's distortion is stored and inspectable, but calibrated rendering is unsupported."))}
    if let near = camera.volumeNear,let far = camera.volumeFar,let unbounded = camera.volumeUnbounded {
      lines.append(L10n.format("camera.authoredVolume","Authored center volume: [%@, %@] · applies only if a render profile selects it",L10n.number(near[i]),unbounded[i] ? "∞":L10n.number(far[i])))
    }
    if !camera.depthMapping.isEmpty {lines.append(L10n.tr("camera.depthDescriptive","Projection depth mapping is descriptive; it does not clip Gaussian centers."))}
    for (key,values) in camera.optics.sorted(by:{$0.key < $1.key}) {lines.append(key + ": " + L10n.number(values[i],digits:6))}
    for (key,values) in camera.acquisition.sorted(by:{$0.key < $1.key}) {lines.append(key + ": " + L10n.number(values[i],digits:6))}
    if !camera.optics.isEmpty || !camera.acquisition.isEmpty {lines.append(L10n.tr("camera.captureDescriptive","Physical tracks show the nearest stored calibration sample for inspection; K remains authoritative and capture exposure is not a render gain."))}
    let views = renderViews.filter{$0.camera.object.id == camera.object.id && $0.cameraView == view}
    if !views.isEmpty {lines.append(L10n.format("camera.boundViews","Render views: %@",views.map{$0.object.label}.joined(separator:", ")))}
    return lines.joined(separator:"\n")
  }
  /// Bounded samples estimate navigation depth only. All source identities remain
  /// rendered. A median rejects isolated far/background points and stays stable
  /// for a complete gesture; this does not run on every mouse movement.
  private func centerOrbitOnView() {
    guard let renderer else { return }
    let camera = renderer.cameraSnapshot()
    let inverse = renderer.cameraSnapshot().pose.inverse
    var depths = [Float]()
    if let a = currentA, let b = currentB, let scene, scene.count > 0 {
      let stride = max(1, (scene.count + 4095) / 4096)
      for i in Swift.stride(from: 0, to: scene.count, by: stride) {
        let x = StreamingScene.value(buffer: a, index: i)
        let y = StreamingScene.value(buffer: b, index: i)
        guard lastAlpha <= 0 ? x.valid : lastAlpha >= 1 ? y.valid : x.valid && y.valid else {
          continue
        }
        let p = inverse * SIMD4(simd_mix(x.position, y.position, SIMD3(repeating: lastAlpha)), 1)
        guard p.z.isFinite, p.z > renderOptionsNearClip else { continue }
        let h = camera.k * SIMD3(p.x, p.y, p.z)
        let uv = SIMD2(h.x / h.z, h.y / h.z)
        if uv.x > 0 && uv.x < camera.size.x && uv.y > 0 && uv.y < camera.size.y {
          depths.append(p.z)
        }
      }
    }
    depths.sort()
    let depth =
      depths.isEmpty
      ? max(renderOptionsNearClip * 10, min(scene?.radius ?? 1, 1e6)) : depths[depths.count / 2]
    renderer.setOrbitPivot(
      position: NavigationGeometry.vector(camera.pose[3]) + NavigationGeometry.vector(
        camera.pose[2]) * depth)
    workspace.updateOrbitCenter(renderer.orbitCenter())
    workspace.setStatus(
      L10n.tr(
        "navigation.centerSet",
        "Orbit center updated. The amber cross marks the fixed world-space pivot."))
  }
  private func centerOrbitOnSelection() {
    guard let renderer else { return }
    if selectionSet.items.count > 1, let bounds = mixedSelectionBounds() {
      renderer.setOrbitPivot(position: bounds.center)
    } else if let index = selectedIndex, let a = currentA, let b = currentB {
      let x = StreamingScene.value(buffer: a, index: index)
      let y = StreamingScene.value(buffer: b, index: index)
      guard lastAlpha <= 0 ? x.valid : lastAlpha >= 1 ? y.valid : x.valid && y.valid else { return }
      renderer.setOrbitPivot(
        position: simd_mix(x.position, y.position, SIMD3(repeating: lastAlpha)))
    } else if let child = (scene as? CompositeScene)?.children.first(where: {
      $0.object.id == selectedObjectID
    }) {
      renderer.setOrbitPivot(position: child.center)
    } else if let scene, scene.object.id == selectedObjectID {
      renderer.setOrbitPivot(position: scene.center)
    } else {
      workspace.setStatus(
        L10n.tr(
          "navigation.selectGeometry",
          "Select a visible geometric object or primitive to set the orbit center."))
      return
    }
    workspace.updateOrbitCenter(renderer.orbitCenter())
    workspace.setStatus(
      L10n.tr(
        "navigation.centerSet",
        "Orbit center updated. The amber cross marks the fixed world-space pivot."))
  }
  private func frameSelection() {
    guard let renderer else { return }
    selectedCamera = nil
    if selectionSet.items.count > 1, let bounds = mixedSelectionBounds() {
      renderer.focus(position: bounds.center, radius: bounds.radius)
    } else if let index = selectedIndex, let a = currentA, let b = currentB {
      let x = StreamingScene.value(buffer: a, index: index)
      let y = StreamingScene.value(buffer: b, index: index)
      let position = simd_mix(x.position, y.position, SIMD3(repeating: lastAlpha))
      guard lastAlpha <= 0 ? x.valid : lastAlpha >= 1 ? y.valid : x.valid && y.valid else { return }
      renderer.focus(position: position, radius: max(0.05, simd_length(x.scale) * 8))
    } else if let index = inspectedCamera, cameraEntries.indices.contains(index) {
      let camera = cameraEntries[index].camera
      guard camera.object.space == activeSpaceID,
        camera.times.count == 1 || camera.object.clock == activeClockID
      else {
        workspace.setStatus(
          L10n.tr(
            "camera.foreignSpace",
            "This camera uses a different coordinate space. Explicit alignment is required."))
        return
      }
      let value = camera.at(time: committedTime, view: cameraEntries[index].view)
      renderer.focus(
        position: SIMD3(value.pose[3].x, value.pose[3].y, value.pose[3].z),
        radius: max(0.1, (scene?.radius ?? 1) * 0.1))
    } else if let child = (scene as? CompositeScene)?.children.first(where: {
      $0.object.id == selectedObjectID
    }) {
      renderer.focus(position: child.center, radius: child.radius)
    } else if let scene {
      renderer.focus(position: scene.center, radius: scene.radius)
    }
    applyCamera()
  }
  private func mixedSelectionBounds() -> (center: SIMD3<Float>, radius: Float)? {
    var low = SIMD3<Float>(repeating: .infinity), high = SIMD3<Float>(repeating: -.infinity)
    func include(_ center: SIMD3<Float>, radius: Float) {
      let extent = SIMD3<Float>(repeating: max(0.05, radius))
      low = simd_min(low, center - extent); high = simd_max(high, center + extent)
    }
    func includeCamera(_ index: Int) {
      guard cameraEntries.indices.contains(index) else { return }
      let entry = cameraEntries[index]
      guard entry.camera.object.space == activeSpaceID,
        entry.camera.times.count == 1 || entry.camera.object.clock == activeClockID else { return }
      let position = entry.camera.at(time: committedTime, view: entry.view).pose[3]
      include(SIMD3(position.x, position.y, position.z), radius: max(0.1, (scene?.radius ?? 1) * 0.1))
    }
    for item in selectionSet.items {
      switch item {
      case .element(let objectID, let identity):
        guard let index = scene?.index(of: identity, objectID: objectID), let a = currentA, let b = currentB else { continue }
        let x = StreamingScene.value(buffer: a, index: index), y = StreamingScene.value(buffer: b, index: index)
        guard lastAlpha <= 0 ? x.valid : lastAlpha >= 1 ? y.valid : x.valid && y.valid else { continue }
        include(simd_mix(x.position, y.position, SIMD3(repeating: lastAlpha)),
          radius: simd_length(simd_mix(x.scale, y.scale, SIMD3(repeating: lastAlpha))) * 8)
      case .camera(_, let index): includeCamera(index)
      case .object(let id):
        if let child = (scene as? CompositeScene)?.children.first(where: { $0.object.id == id }) {
          include(child.center, radius: child.radius)
        } else if let scene, scene.object.id == id {
          include(scene.center, radius: scene.radius)
        }
        for index in cameraEntries.indices where cameraEntries[index].camera.object.id == id { includeCamera(index) }
      case .document: break
      }
    }
    guard (0..<3).allSatisfy({ low[$0].isFinite && high[$0].isFinite }) else { return nil }
    let center = low * 0.5 + high * 0.5, radius = max(0.05, simd_length(high - low) * 0.5)
    guard radius.isFinite else { return nil }
    return (center, radius)
  }
  /// Resolve only the immutable request's stable object/view identities. GPU
  /// completion and controller interpretation have separate lifetime fences.
  func consumeScenePick(_ result: ScenePickResult) {
    guard let request = pendingPick else { return }
    pendingPick = nil
    guard request.revision == selectionRevision, request.generation == generation,
      request.space == activeSpaceID, request.clock == activeClockID else { return }
    switch result {
    case .gaussian(let index): select(Int(index), operation: request.operation)
    case .camera(let identity):
      guard cameraFrustaVisible, request.cameras.indices.contains(Int(identity)) else { return }
      let key = request.cameras[Int(identity)]
      guard let entry = cameraEntries.firstIndex(where: {
        $0.camera.object.id == key.objectID && $0.camera.viewIDs[$0.view] == key.viewID
      }) else { return }
      let item = StudioSelection.camera(key.objectID, entry)
      applySelectionSet(selectionSet.applying(item, operation: request.operation))
    case .empty: select(nil, operation: request.operation)
    }
  }
  private func invalidateScenePick() {
    selectionRevision &+= 1
    pendingPick = nil
    renderer?.invalidatePicks()
  }
  private func pick(_ pixel: SIMD2<UInt32>, operation: StudioSelectionOperation) {
    guard !isShuttingDown, let renderer, workspace.viewport.bounds.width > 0,
      workspace.viewport.drawableSize.width > 0 else { return }
    pendingPick = (operation, selectionRevision, generation, activeSpaceID, activeClockID,
      cameraEntries.map { ($0.camera.object.id, $0.camera.viewIDs[$0.view]) })
    // Hit tolerance is expressed in view points and converted once to the
    // actual render resolution. Gaussian and visible camera edges share one
    // asynchronous depth-aware request; camera origins get no CPU priority.
    let scale = Float(workspace.viewport.drawableSize.width / workspace.viewport.bounds.width)
    renderer.requestScenePick(pixel: pixel, tolerancePixels: 4 * scale)
  }
  private var renderOptionsNearClip: Float { workspace.renderOptions.nearClip }
  private func currentTimeUnit() -> String {
    let clocks = document?.manifest["clocks"] as? [String: [String: Any]]
    return clocks?[activeClockID]?["unit"] as? String
      ?? (authoredClocks[activeClockID] as? [String: Any])?["unit"] as? String ?? "second"
  }
  private var activeRenderBinding:GSVRenderView? {
    guard comparisonEnabled else {return nil}
    let id = previewRenderViewID ?? comparisonIndex.flatMap{comparisonEntries.indices.contains($0) ? comparisonEntries[$0].renderViewID:nil}
    return renderViews.first{$0.object.id == id}
  }
  private var activeClockID: String { activeRenderBinding?.camera.object.clock ?? scene?.object.clock ?? viewportClock ?? "timeline" }
  private var activeSpaceID: String? { activeRenderBinding?.camera.object.space ?? scene?.object.space ?? viewportSpace }
  private func setObjectVisibility(_ id: String, _ visible: Bool) {
    guard !isShuttingDown, let document, let renderer else { return }
    if visible { visibleObjectIDs.insert(id) } else { visibleObjectIDs.remove(id) }
    comparisonGeometryPending = true
    comparisonRevision = UUID(); comparisonCancellation?.cancel()
    if comparisonEnabled { workspace.comparisonPending(L10n.tr("compare.visibilityPending", "Visible geometry changed; comparison is pending.")) }
    let requested = visibleObjectIDs
    beginIOGeneration()
    let token = generation
    let cancellation = ioCancellation
    setPlaying(false)
    workspace.setLoading(message: L10n.tr("loading.visibility", "Updating visible objects…"))
    io.submit(cancellation: cancellation) { [weak self] in
      do {
        try cancellation.check()
        let loaded = try Self.geometry(document, visible: requested, device: renderer.device, cancellation: cancellation)
        try cancellation.check()
        let a = try loaded.flatMap { $0.count > 0 ? try $0.frame(0) : nil }
        try cancellation.check()
        let b = try loaded.flatMap { $0.count > 0 ? try $0.frame(min(1, $0.frameCount - 1)) : nil }
        try cancellation.check()
        DispatchQueue.main.async { [weak self] in
          guard let self, !cancellation.isCancelled, self.generation == token else { return }
          // Compare the actual committed viewport domain, not the domain at
          // request launch: camera navigation can continue while I/O is pending.
          let previousSpace = self.activeSpaceID
          let previousClock = self.activeClockID
          self.comparisonGeometryPending = false
          self.scene = loaded
          self.viewportSpace = loaded?.object.space ?? previousSpace
          self.viewportClock = loaded?.object.clock ?? previousClock
          let domainChanged =
            previousSpace != self.activeSpaceID
            || previousClock != self.activeClockID
          self.preparedA = a
          self.preparedB = b
          self.currentA = nil
          self.currentB = nil
          self.currentLower = 0
          self.frameRequestActive = false
          self.selectedIndex = nil
          self.renderer?.clear()
          if domainChanged {
            // A camera pose or playhead value from another declared domain
            // cannot be reinterpreted as belonging to the new geometry.
            self.selectedCamera = nil
            self.previewRange = nil
            self.workspace.rangeEnabled.state = .off
            self.workspace.timeline.previewRange = nil
            self.targetTime = loaded?.times.first ?? 0
            self.trajectoryToken = UUID()
            self.resetCamera()
            self.renderer?.clearNavigationHistory()
          }
          self.refreshWorkspace()
          if domainChanged {
            self.workspace.rangeStart.stringValue = L10n.time(self.timeline[0])
            self.workspace.rangeEnd.stringValue = L10n.time(self.timeline.last!)
          }
          self.workspace.setLoading(message: nil)
          self.requestTime(self.targetTime)
          self.applySelectionSet(self.selectionSet)
          self.updateTrajectory()
          self.updateCameraOverlays()
        }
      } catch {
        guard !cancellation.isCancelled else { return }
        DispatchQueue.main.async { [weak self] in
          guard let self, !cancellation.isCancelled, self.generation == token else { return }
          self.comparisonGeometryPending = false
          self.visibleObjectIDs = Set(
            (self.scene as? CompositeScene)?.children.map { $0.object.id } ?? self.scene.map {
              [$0.object.id]
            } ?? [])
          self.refreshWorkspace()
          if self.comparisonEnabled {self.scheduleComparison()}
          self.workspace.presentError(message: error.localizedDescription)
        }
      }
    }
  }
  private func rebuildCameraEntries() {
    // Camera IDs in the GPU buffer are request-local entry indices. A table
    // rebuild must invalidate the pending draw, even if a deleted entry's slot
    // is immediately occupied by another camera or view.
    invalidateScenePick()
    let oldEntries = cameraEntries
    let previousSelection = selectionSet
    let inspection = inspectedCamera.flatMap {
      cameraEntries.indices.contains($0)
        ? (cameraEntries[$0].camera.object.id, cameraEntries[$0].camera.viewIDs[cameraEntries[$0].view]) : nil
    }
    let viewing = selectedCamera.flatMap {
      cameraEntries.indices.contains($0)
        ? (cameraEntries[$0].camera.object.id, cameraEntries[$0].camera.viewIDs[cameraEntries[$0].view]) : nil
    }
    cameraEntries = sourceCameras.flatMap { camera in
      (0..<camera.viewCount).map {
        (
          name:
            camera.viewCount == 1
            ? camera.object.label
            : camera.object.label + " · " + L10n.format("camera.viewNumber", "View %d", $0 + 1),
          camera: camera, view: $0
        )
      }
    }
    cameraEntries += authoredCameras.map {
      (
        name: L10n.format("camera.userViewName", "%@ · user", $0.label), camera: $0.camera(),
        view: 0
      )
    }
    inspectedCamera = inspection.flatMap { key in
      cameraEntries.firstIndex { $0.camera.object.id == key.0 && $0.camera.viewIDs[$0.view] == key.1 }
    }
    selectedCamera = viewing.flatMap { key in
      cameraEntries.firstIndex { $0.camera.object.id == key.0 && $0.camera.viewIDs[$0.view] == key.1 }
    }
    func remap(_ item: StudioSelection) -> StudioSelection? {
      guard case .camera(let id, let entry) = item else { return item }
      guard oldEntries.indices.contains(entry) else { return nil }
      let viewID = oldEntries[entry].camera.viewIDs[oldEntries[entry].view]
      guard let updated = cameraEntries.firstIndex(where: {
        $0.camera.object.id == id && $0.camera.viewIDs[$0.view] == viewID
      }) else { return nil }
      return .camera(id, updated)
    }
    selectionSet = StudioSelectionSet(items: previousSelection.items.compactMap(remap),
      primary: remap(previousSelection.primary))
    selectedObjectID = selectionSet.primary.objectID
  }
  private func lowerFrame(_ time: Double, _ times: [Double]) -> Int {
    var low = 0
    var high = times.count
    while low < high {
      let mid = (low + high) / 2
      if times[mid] <= time { low = mid + 1 } else { high = mid }
    }
    return max(0, min(low - 1, times.count - 1))
  }

  private func requestTime(_ time: Double) {
    guard !isShuttingDown, let document else { return }
    do { try document.checkUnchanged() } catch {
      setPlaying(false)
      renderer?.clear()
      workspace.presentError(message: error.localizedDescription)
      return
    }
    targetTime = min(max(time, timeline[0]), timeline.last!)
    if comparisonEnabled { scheduleComparison() }
    guard let scene, scene.count > 0 else {
      currentA = nil; currentB = nil
      committedTime = targetTime
      currentLower = lowerFrame(targetTime, timeline)
      workspace.updateFrame(index: currentLower, time: targetTime)
      refreshCameraAuthoring()
      refreshInspectedCamera()
      applyCamera()
      updateTrajectoryPlayback()
      updateCameraOverlays()
      return
    }
    let lower = lowerFrame(targetTime, scene.times)
    let upper = min(lower + 1, scene.frameCount - 1)
    if lower == currentLower, let a = preparedA, let b = preparedB {
      let aTime = scene.times[lower]
      let bTime = scene.times[upper]
      let alpha: Float
      if upper == lower || targetTime.isNaN || targetTime <= aTime {
        alpha = 0
      } else if targetTime >= bTime {
        alpha = 1
      } else {
        let span = bTime - aTime
        // Finite source clocks can span more than Double.max. Scale all
        // operands together so their interpolation fraction remains finite.
        let fraction =
          span.isFinite
          ? (targetTime - aTime) / span
          : (targetTime * 0.5 - aTime * 0.5) / (bTime * 0.5 - aTime * 0.5)
        alpha = Float(min(1, max(0, fraction)))
      }
      do { try renderer?.setFrames(a: a, b: b, count: scene.count, interpolation: alpha) } catch {
        setPlaying(false)
        workspace.presentError(message: error.localizedDescription)
        return
      }
      currentA = a; currentB = b
      lastAlpha = alpha
      committedTime = targetTime
      workspace.updateFrame(index: lowerFrame(targetTime, timeline), time: targetTime)
      updateSelection()
      refreshCameraAuthoring()
      refreshInspectedCamera()
      applyCamera()
      updateTrajectoryPlayback()
      updateCameraOverlays()
      return
    }
    if frameRequestActive { return }
    frameRequestActive = true
    let token = generation
    let cancellation = ioCancellation
    io.submit(cancellation: cancellation) { [weak self] in
      do {
        try cancellation.check()
        let a = try scene.frame(lower)
        try cancellation.check()
        let b = try scene.frame(upper)
        try cancellation.check()
        DispatchQueue.main.async { [weak self] in
          guard let self, !cancellation.isCancelled, self.generation == token else { return }
          self.frameRequestActive = false
          self.currentLower = lower
          self.preparedA = a
          self.preparedB = b
          self.requestTime(self.targetTime)
        }
      } catch {
        guard !cancellation.isCancelled else { return }
        DispatchQueue.main.async { [weak self] in
          guard let self, !cancellation.isCancelled, self.generation == token else { return }
          self.frameRequestActive = false
          self.setPlaying(false)
          self.workspace.presentError(message: error.localizedDescription)
        }
      }
    }
  }

  private func resetCamera() {
    selectedCamera = nil
    if let scene {
      renderer?.resetCamera(center: scene.center, radius: max(scene.radius, 0.01))
    } else {
      renderer?.resetCamera()
    }
    applyCamera()
  }
  private func select(_ index: Int?, operation: StudioSelectionOperation = .replace) {
    if let index, index < 0 || index >= (scene?.count ?? 0) { return }
    guard let index, let object = scene?.sourceObject(at: index), let id = scene?.id(at: index) else {
      if operation == .replace { clearSelection() }
      return
    }
    applySelectionSet(selectionSet.applying(.element(object.id, id), operation: operation))
    if let a = currentA, let b = currentB {
      let x = StreamingScene.value(buffer: a, index: index)
      let y = StreamingScene.value(buffer: b, index: index)
      if x.valid && y.valid {
        renderer?.setOrbitPivot(
          position: simd_mix(x.position, y.position, SIMD3(repeating: lastAlpha)))
      }
    }
  }
  private func updateTrajectory() {
    guard !isShuttingDown else { return }
    trajectoryCancellation?.cancel()
    trajectoryCancellation = nil
    trajectoryToken = UUID()
    let token = trajectoryToken
    renderer?.replaceTrajectory(nil)
    refreshSelectionStatus()
    guard let scene else { return }
    let allowedObjects = trajectoryObjectIDs
    let objectIDs = Set(selectionSet.items.compactMap { item -> String? in
      guard case .object(let id) = item, allowedObjects.contains(id) else { return nil }
      return id
    })
    let indices = selectionSet.items.compactMap { item -> Int? in
      guard case .element(let id, let identity) = item else { return nil }
      return scene.index(of: identity, objectID: id)
    }
    guard !objectIDs.isEmpty || !indices.isEmpty else { return }
    let density = trajectoryOptions.densityPercent
    let cancellation = TrajectoryBuildCancellation()
    trajectoryCancellation = cancellation
    workspace.setStatus(L10n.tr("trajectory.preparing", "Preparing complete trajectories…"))
    trajectoryQueue.async { [weak self] in
      do {
        let geometry = try scene.buildTrajectories(objectIDs: objectIDs, elementIndices: indices,
          densityPercent: density, cancellation: cancellation, progress: { fraction in
            DispatchQueue.main.async { [weak self] in
              guard let self, self.trajectoryToken == token else { return }
              self.workspace.setStatus(L10n.format("trajectory.progress", "Preparing complete trajectories · %@%%",
                L10n.number(fraction * 100, digits: 0)))
            }
          })
        DispatchQueue.main.async {
          guard let self, self.trajectoryToken == token else { return }
          self.renderer?.replaceTrajectory(geometry)
          self.updateTrajectoryPlayback()
          self.workspace.setStatus(L10n.format("trajectory.ready", "%@ identities · %@ trajectory segments · %@%% density",
            L10n.number(Double(geometry.identityCount), digits: 0),
            L10n.number(Double(geometry.segmentCount), digits: 0), String(density)))
        }
      } catch {
        DispatchQueue.main.async {
          if self?.trajectoryToken == token {
            self?.workspace.setStatus(error.localizedDescription)
          }
        }
      }
    }
  }
  private func updateTrajectoryPlayback() {
    let times = scene?.times ?? timeline
    renderer?.setTrajectoryPlayback(time: committedTime, trailStartTime: trajectoryTrailStartTime(times: times),
      options: trajectoryOptions)
  }
  private func trajectoryTrailStartTime(times: [Double]) -> Double {
    TrajectoryPlayback.trailStart(time: committedTime, times: times,
      frames: trajectoryOptions.trailFrames)
  }
  private func updateSelection() {
    guard case .element = selectionSet.primary else { return }
    guard let index = selectedIndex, let a = currentA, let b = currentB,
      let id = scene?.id(at: index)
    else {
      workspace.updateSelection(nil)
      return
    }
    let x = StreamingScene.value(buffer: a, index: index)
    let y = StreamingScene.value(buffer: b, index: index)
    let t = lastAlpha
    let q0 = simd_quatf(
      ix: x.quaternion.y, iy: x.quaternion.z, iz: x.quaternion.w, r: x.quaternion.x)
    let q1 = simd_quatf(
      ix: y.quaternion.y, iy: y.quaternion.z, iz: y.quaternion.w, r: y.quaternion.x)
    let q = simd_slerp(q0, q1, t)
    workspace.updateSelection(
      WorkspaceSelectionInfo(
        id: id, objectID: scene?.sourceObject(at: index)?.id,
        sourceLabel: scene?.sourceObject(at: index)?.label,
        isScaffold: scene?.sourceObject(at: index)?.type == "scaffold",
        position: simd_mix(x.position, y.position, SIMD3(repeating: t)),
        scales: simd_mix(x.scale, y.scale, SIMD3(repeating: t)),
        opacity: x.opacity + (y.opacity - x.opacity) * t,
        color: simd_mix(x.color, y.color, SIMD3(repeating: t)),
        colorSpace: scene?.sourceObject(at: index)?.data["color_space"] as? String,
        quaternion: SIMD4(q.real, q.imag.x, q.imag.y, q.imag.z),
        valid: t <= 0 ? x.valid : t >= 1 ? y.valid : x.valid && y.valid,
        confidence: x.confidence < 0 || y.confidence < 0
          ? nil : x.confidence + (y.confidence - x.confidence) * t))
  }

  private func applyCamera() {
    guard let selected = selectedCamera, selected < cameraEntries.count else {
      if renderer != nil {
        workspace.setCameraCaption(L10n.tr("view.freeCaption", "Perspective · free view"))
      }
      return
    }
    let entry = cameraEntries[selected]
    let camera = entry.camera
    if let scene,
      camera.object.space != scene.object.space
        || (camera.times.count > 1 && camera.object.clock != scene.object.clock)
    {
      selectedCamera = nil
      workspace.setStatus(
        L10n.tr(
          "camera.geometryDomainMismatch",
          "Camera and Gaussian object have different spaces or clocks. Explicit alignment is required."
        ))
      return
    }
    guard camera.supportsCalibratedRendering else {
      selectedCamera = nil
      workspace.setStatus(L10n.tr("render.distortionUnsupported","This camera's distortion is stored and inspectable, but calibrated rendering is unsupported."))
      return
    }
    let value = camera.at(time: committedTime, view: entry.view)
    workspace.setCameraCaption(entry.name)
    renderer?.setCalibratedCamera(
      cameraToWorld: value.pose, intrinsics: value.k, imageSize: value.size,
      integerPixelCenters: camera.integerPixelCenters)
  }
  private func updateCameraOverlays() {
    var graph = [SIMD4<Float>]()
    if let scene, let a = currentA, let b = currentB, !scene.edges.isEmpty {
      for (x, y) in scene.edges {
        let xa = StreamingScene.value(buffer: a, index: x)
        let xb = StreamingScene.value(buffer: b, index: x)
        let ya = StreamingScene.value(buffer: a, index: y)
        let yb = StreamingScene.value(buffer: b, index: y)
        if xa.valid && xb.valid && ya.valid && yb.valid {
          graph += [
            SIMD4(simd_mix(xa.position, xb.position, SIMD3(repeating: lastAlpha)), 1),
            SIMD4(simd_mix(ya.position, yb.position, SIMD3(repeating: lastAlpha)), 1),
            SIMD4<Float>(0, 0, 0, 0),
          ]
        }
      }
    }
    renderer?.setGraphLines(points: graph)
    guard cameraFrustaVisible else {
      renderer?.setCameraSegments([])
      return
    }
    var lines = [TrajectorySegmentGPU]()
    var pickSegments = [CameraPickSegmentGPU]()
    let scale = max(0.05, min(1, (scene?.radius ?? 3) * 0.08))
    func segment(_ a: SIMD3<Float>, _ b: SIMD3<Float>, _ color: SIMD4<Float>, cameraID: UInt32? = nil) {
      guard simd_length_squared(a - b) > 0 else { return }
      lines.append(TrajectorySegmentGPU(startPositionTime: SIMD4(a, 0),
        endPositionTime: SIMD4(b, 0), startColor: color, endColor: color))
      if let cameraID {
        pickSegments.append(CameraPickSegmentGPU(startPosition: SIMD4(a, 0),
          endPosition: SIMD4(b, 0), cameraID: cameraID))
      }
    }
    for (entryIndex, entry) in cameraEntries.enumerated() {
      let camera = entry.camera
      if camera.object.space != activeSpaceID
        || (camera.times.count > 1 && camera.object.clock != activeClockID)
      {
        continue
      }
      let isSelected = selectionSet.items.contains(.camera(camera.object.id, entryIndex))
        || selectionSet.items.contains(.object(camera.object.id))
      let color = isSelected ? SIMD4<Float>(1, 0.67, 0.15, 1) : SIMD4<Float>(0.3, 0.55, 0.65, 0.65)
      let value = camera.at(time: committedTime, view: entry.view)
      let origin = SIMD3(value.pose[3].x, value.pose[3].y, value.pose[3].z)
      let inverse = value.k.inverse
      let edge: Float = camera.integerPixelCenters ? -0.5 : 0
      let corners = [
        SIMD3<Float>(edge, edge, 1), SIMD3(value.size.x + edge, edge, 1),
        SIMD3(value.size.x + edge, value.size.y + edge, 1), SIMD3(edge, value.size.y + edge, 1),
      ].map { pixel -> SIMD3<Float> in
        let ray = inverse * pixel
        let point = value.pose * SIMD4(ray * scale, 1)
        return SIMD3(point.x, point.y, point.z)
      }
      for i in 0..<4 {
        segment(origin, corners[i], color, cameraID: UInt32(entryIndex))
        segment(corners[i], corners[(i + 1) % 4], color, cameraID: UInt32(entryIndex))
      }
      guard isSelected, camera.times.count > 1,
        trajectoryOptions.showPaths || (trajectoryOptions.showTrails && trajectoryOptions.trailFrames > 0) else { continue }
      let trailStart = trajectoryTrailStartTime(times: camera.times)
      func pathSegment(_ start: Double, _ end: Double, opacity: Float) {
        guard end > start, opacity > 0 else { return }
        let a = camera.at(time: start, view: entry.view).pose[3]
        let b = camera.at(time: end, view: entry.view).pose[3]
        segment(SIMD3(a.x, a.y, a.z), SIMD3(b.x, b.y, b.z), SIMD4(color.x, color.y, color.z, opacity))
      }
      for i in 0..<(camera.times.count - 1) {
        let start = camera.times[i], end = camera.times[i + 1]
        let trailA = max(start, trailStart), trailB = min(end, committedTime)
        let hasTrail = trajectoryOptions.showTrails && trajectoryOptions.trailFrames > 0 && trailB > trailA
        if trajectoryOptions.showPaths {
          if hasTrail {
            pathSegment(start, trailA, opacity: trajectoryOptions.pathOpacity)
            pathSegment(trailB, end, opacity: trajectoryOptions.pathOpacity)
          } else { pathSegment(start, end, opacity: trajectoryOptions.pathOpacity) }
        }
        if hasTrail { pathSegment(trailA, trailB, opacity: 1) }
      }
    }
    renderer?.setCameraSegments(lines, pickSegments: pickSegments)
  }
  private func createCamera() {
    guard let renderer else { return }
    guard document != nil else {
      workspace.setStatus(
        L10n.tr(
          "camera.openFirst",
          "Open a GSV document, including an empty document, before creating cameras."))
      return
    }
    let snapshot = renderer.cameraSnapshot()
    let camera = AuthoredCamera(
      label: L10n.format("camera.defaultName", "Camera %d", authoredCameras.count + 1),
      space: activeSpaceID, clock: activeClockID,
      time: targetTime, pose: snapshot.pose, intrinsics: snapshot.k, imageSize: snapshot.size,nearClip:workspace.renderOptions.nearClip)
    rememberCameraEdit(L10n.tr("undo.createCamera", "Create Camera"))
    editingCameraID = camera.id
    camerasDirty = true
    viewportSpace = camera.space
    viewportClock = camera.clock
    if (document?.manifest["spaces"] as? [String: Any])?[camera.space] == nil,
      authoredSpaces[camera.space] == nil
    {
      authoredSpaces[camera.space] = [
        "unit": "unitless", "handedness": "right", "axes": ["x", "y", "z"],
      ]
    }
    if (document?.manifest["clocks"] as? [String: Any])?[camera.clock] == nil,
      authoredClocks[camera.clock] == nil
    {
      authoredClocks[camera.clock] = ["unit": "second"]
    }
    authoredCameras.append(camera)
    rebuildCameraEntries()
    refreshWorkspace()
    selectObject(camera.id)
    updateCameraOverlays()
    workspace.setStatus(
      L10n.format(
        "camera.created",
        "Created %@ from the current view. Move the view, seek a time, then Record Keyframe.",
        camera.label))
  }
  private func recordCameraKeyframe() {
    guard let renderer, let index = editingCameraIndex else {
      workspace.setStatus(L10n.tr("camera.createFirst", "Select an editable camera first."))
      return
    }
    let snapshot = renderer.cameraSnapshot()
    guard authoredCameras[index].space == activeSpaceID || activeSpaceID == nil,
      authoredCameras[index].clock == activeClockID
    else {
      workspace.setStatus(
        L10n.tr(
          "camera.recordDomainMismatch",
          "The recording camera uses a different space or clock. Select its user view before recording."
        ))
      return
    }
    rememberCameraEdit(L10n.tr("undo.recordKeyframe", "Record Keyframe"))
    camerasDirty = true
    authoredCameras[index].record(
      time: targetTime, pose: snapshot.pose, intrinsics: snapshot.k, imageSize: snapshot.size,nearClip:workspace.renderOptions.nearClip)
    rebuildCameraEntries()
    refreshWorkspace()
    updateCameraOverlays()
    workspace.setStatus(
      L10n.format(
        "camera.recorded", "Recorded %@ at %@ %@.", authoredCameras[index].label,
        L10n.number(targetTime, digits: 3), L10n.tr("unit." + currentTimeUnit(), currentTimeUnit()))
    )
  }
  private func rememberCameraEdit(_ name: String) {
    let tracks = authoredCameras
    let target = editingCameraID
    let spaces = authoredSpaces
    let clocks = authoredClocks
    cameraRevision &+= 1
    cameraUndo.beginUndoGrouping()
    cameraUndo.registerUndo(withTarget: self) { controller in
      controller.restoreCameraEdit(
        tracks, target: target, name: name, spaces: spaces, clocks: clocks)
    }
    cameraUndo.setActionName(name)
    cameraUndo.endUndoGrouping()
  }
  private func restoreCameraEdit(
    _ tracks: [AuthoredCamera], target: String?, name: String, spaces: [String: Any],
    clocks: [String: Any]
  ) {
    rememberCameraEdit(name)
    authoredCameras = tracks
    editingCameraID = target
    authoredSpaces = spaces
    authoredClocks = clocks
    camerasDirty = true
    selectedCamera = nil
    rebuildCameraEntries()
    refreshWorkspace()
    if let target { selectObject(target) }
    updateCameraOverlays()
    workspace.setStatus(
      L10n.format("camera.historyUpdated", "%@ · Camera edit history updated.", name))
  }
  private var editingCameraIndex: Int? {
    editingCameraID.flatMap { id in authoredCameras.firstIndex(where: { $0.id == id }) }
  }
  private func refreshCameraAuthoring() {
    let camera = editingCameraIndex.map { authoredCameras[$0] }
    let inspectionTime =
      camera?.clock == activeClockID ? committedTime : (camera?.keyframes.first?.time ?? 0)
    let value = camera?.camera().at(time: inspectionTime, view: 0)
    let fov = value.map { Double(2 * atan($0.size.x / (2 * $0.k[0][0])) * 180 / Float.pi) }
    workspace.updateCameraAuthoring(
      name: camera?.label, times: camera?.keyframes.map { $0.time } ?? [], fov: fov,
      canUndo: cameraUndo.canUndo, canRedo: cameraUndo.canRedo,
      timelineCompatible: camera?.clock == activeClockID && camera?.space == activeSpaceID)
  }
  private func renameCamera(_ name: String) {
    guard let index = editingCameraIndex else { return }
    let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !name.isEmpty, name.count <= 120 else {
      workspace.setStatus(L10n.tr("camera.invalidName", "Camera names require 1–120 characters."))
      return
    }
    rememberCameraEdit(L10n.tr("undo.renameCamera", "Rename Camera"))
    authoredCameras[index].label = name
    camerasDirty = true
    rebuildCameraEntries()
    refreshWorkspace()
  }
  private func deleteCamera() {
    guard let index = editingCameraIndex else { return }
    let deletedID = authoredCameras[index].id
    rememberCameraEdit(L10n.tr("undo.deleteCamera", "Delete Camera"))
    authoredCameras.remove(at: index)
    editingCameraID = nil
    selectedCamera = nil
    camerasDirty = true
    rebuildCameraEntries()
    refreshWorkspace()
    applySelectionSet(StudioSelectionSet(items: selectionSet.items.filter { $0.objectID != deletedID },
      primary: selectionSet.primary))
    updateCameraOverlays()
  }
  private func jumpCameraKey(_ key: Int) {
    guard let index = editingCameraIndex, key >= 0, key < authoredCameras[index].keyframes.count
    else { return }
    let track = authoredCameras[index]
    if let scene, track.space != scene.object.space || track.clock != scene.object.clock {
      workspace.setStatus(
        L10n.tr(
          "camera.keyDomainMismatch",
          "Camera keyframe uses a different space or clock. Explicit alignment is required."))
      return
    }
    viewportSpace = track.space
    viewportClock = track.clock
    setPlaying(false)
    refreshWorkspace()
    requestTime(authoredCameras[index].keyframes[key].time)
  }
  private func deleteCameraKey(_ key: Int) {
    guard let index = editingCameraIndex, key >= 0, key < authoredCameras[index].keyframes.count
    else { return }
    guard authoredCameras[index].keyframes.count > 1 else {
      workspace.setStatus(
        L10n.tr(
          "camera.lastKey", "A camera requires at least one keyframe. Delete Camera to remove it."))
      return
    }
    rememberCameraEdit(L10n.tr("undo.deleteKeyframe", "Delete Keyframe"))
    authoredCameras[index].keyframes.remove(at: key)
    camerasDirty = true
    rebuildCameraEntries()
    refreshWorkspace()
    requestTime(targetTime)
    updateCameraOverlays()
  }
  private func keyframeCameraFOV(_ degrees: Double) {
    guard let index = editingCameraIndex else { return }
    guard authoredCameras[index].space == activeSpaceID || activeSpaceID == nil,
      authoredCameras[index].clock == activeClockID
    else {
      workspace.setStatus(
        L10n.tr(
          "camera.fovDomainMismatch",
          "The editing camera uses a different space or clock. Select its user view before keyframing FOV."
        ))
      return
    }
    guard degrees >= 1, degrees <= 170 else {
      workspace.setStatus(
        L10n.tr("camera.invalidFOV", "Horizontal FOV must be between 1° and 170°."))
      return
    }
    let camera = authoredCameras[index].camera()
    let value = camera.at(time: targetTime, view: 0)
    var k = value.k
    let focal = value.size.x / (2 * tan(Float(degrees) * Float.pi / 360))
    let ratio = focal / k[0][0]
    k[0][0] = focal
    k[1][1] *= ratio
    k[1][0] *= ratio
    rememberCameraEdit(L10n.tr("undo.keyFOV", "Keyframe FOV"))
    authoredCameras[index].record(
      time: targetTime, pose: value.pose, intrinsics: k, imageSize: value.size)
    camerasDirty = true
    rebuildCameraEntries()
    refreshWorkspace()
    requestTime(targetTime)
    workspace.setStatus(
      L10n.format(
        "camera.fovRecorded", "Recorded horizontal FOV %@° at current time.",
        L10n.number(degrees, digits: 2)))
  }
  private func retimeCameraKey(_ key: Int, time: Double) {
    guard let index = editingCameraIndex, key >= 0, key < authoredCameras[index].keyframes.count,
      time.isFinite
    else { return }
    guard authoredCameras[index].clock == activeClockID,
      authoredCameras[index].space == activeSpaceID
    else {
      workspace.setStatus(
        L10n.tr(
          "camera.keyDomainMismatch",
          "Camera keyframe uses a different space or clock. Explicit alignment is required."))
      return
    }
    guard
      !authoredCameras[index].keyframes.enumerated().contains(where: {
        $0.offset != key && $0.element.time == time
      })
    else {
      workspace.setStatus(
        L10n.tr(
          "camera.keyCollision", "Another key already exists at this time. No keys were changed."))
      return
    }
    let old = authoredCameras[index].keyframes[key]
    rememberCameraEdit(L10n.tr("undo.retimeKeyframe", "Retime Keyframe"))
    authoredCameras[index].keyframes[key] = CameraKeyframe(
      time: time, pose: old.pose, intrinsics: old.intrinsics,nearClip:old.nearClip,farClip:old.farClip)
    authoredCameras[index].keyframes.sort { $0.time < $1.time }
    camerasDirty = true
    rebuildCameraEntries()
    refreshWorkspace()
    requestTime(time)
    updateCameraOverlays()
  }
  private func keyframeCameraTransform(position: SIMD3<Float>, quaternion: SIMD4<Float>) {
    guard let index = editingCameraIndex else { return }
    guard authoredCameras[index].space == activeSpaceID,
      authoredCameras[index].clock == activeClockID
    else {
      workspace.setStatus(
        L10n.tr(
          "camera.transformDomain",
          "Select a camera in the active coordinate space and clock before editing its transform."))
      return
    }
    guard position.x.isFinite, position.y.isFinite, position.z.isFinite,
      (0..<4).allSatisfy({ quaternion[$0].isFinite }), simd_length(quaternion).isFinite,
      simd_length(quaternion) > 1e-7
    else {
      workspace.setStatus(
        L10n.tr(
          "camera.invalidQuaternion", "The quaternion must be finite and have nonzero length."))
      return
    }
    let q = simd_normalize(
      simd_quatf(ix: quaternion.y, iy: quaternion.z, iz: quaternion.w, r: quaternion.x))
    let r = simd_float3x3(q)
    let pose = simd_float4x4(
      columns: (SIMD4(r[0], 0), SIMD4(r[1], 0), SIMD4(r[2], 0), SIMD4(position, 1)))
    let value = authoredCameras[index].camera().at(time: targetTime, view: 0)
    rememberCameraEdit(L10n.tr("undo.keyTransform", "Keyframe Transform"))
    authoredCameras[index].record(
      time: targetTime, pose: pose, intrinsics: value.k, imageSize: value.size)
    camerasDirty = true
    rebuildCameraEntries()
    refreshWorkspace()
    requestTime(targetTime)
    updateCameraOverlays()
  }
  private func saveCameras() {
    guard let source = document, !cameraFilePanelOpen else {
      workspace.setStatus(
        L10n.tr(
          "camera.savePrerequisite",
          "Open a source document and finish any active camera file dialog first."))
      return
    }
    let panel = NSSavePanel()
    panel.title = L10n.tr("camera.saveTitle", "Save Camera Animation")
    panel.nameFieldStringValue = "camera-animation.gsv"
    panel.allowedContentTypes = [.data]
    guard let window = workspace.window else { return }
    let tracks = authoredCameras
    let spaces = authoredSpaces
    let clocks = authoredClocks
    let revision = cameraRevision
    cameraFilePanelOpen = true
    panel.beginSheetModal(for: window) { [weak self] response in
      guard let self else { return }
      defer { self.cameraFilePanelOpen = false }
      guard response == .OK, let url = panel.url else { return }
      guard self.document === source else {
        self.workspace.setStatus(
          L10n.tr(
            "camera.exportSourceChanged",
            "Camera export canceled because the source document changed."))
        return
      }
      do {
        try GSVCameraWriter.write(tracks, to: url, source: source, spaces: spaces, clocks: clocks)
        if self.cameraRevision == revision { self.camerasDirty = false }
        self.workspace.setStatus(
          L10n.format(
            "camera.saved", "Saved %@ · %d camera objects; reconstruction unchanged.",
            url.lastPathComponent, tracks.count))
      } catch { self.workspace.presentError(message: error.localizedDescription) }
    }
  }
  private func importCameras() {
    guard let document, let window = workspace.window, !cameraFilePanelOpen else {
      workspace.setStatus(
        L10n.tr(
          "camera.importPrerequisite",
          "Open the source GSV document and finish any active camera file dialog first."))
      return
    }
    let panel = NSOpenPanel()
    panel.title = L10n.tr("camera.importTitle", "Import Camera Animation")
    panel.allowedContentTypes = [.data]
    panel.allowsMultipleSelection = false
    cameraFilePanelOpen = true
    panel.beginSheetModal(for: window) { [weak self] response in
      guard let self else { return }
      defer { self.cameraFilePanelOpen = false }
      guard response == .OK, let url = panel.url else { return }
      guard self.document === document else {
        self.workspace.setStatus(
          L10n.tr(
            "camera.importSourceChanged",
            "Camera import canceled because the source document changed."))
        return
      }
      do { try self.importCameraAnimation(from: url) } catch {
        self.workspace.presentError(message: error.localizedDescription)
      }
    }
  }

  /// The native file panel and programmatic imports share this validated
  /// transaction. Imported cameras become the explicit selection/edit target.
  func importCameraAnimation(from url: URL) throws {
    guard let document else {
      throw SceneError.invalid("Open the source document before importing cameras.")
    }
    let incoming = try GSVDocument(url: url)
    guard
      incoming.objects.allSatisfy({ $0.encoding == "gsv.camera.pinhole" && $0.type == "camera" }
      )
    else {
      throw SceneError.invalid(
        L10n.tr(
          "camera.importCameraOnly",
          "Import a camera-only animation GSV created for this source scene."))
    }
    guard let metadata = incoming.manifest["metadata"] as? [String: Any],
      let source = metadata["source_document"] as? [String: Any],
      let hash = source["manifest_sha256"] as? String, hash == document.manifestSHA256
    else {
      throw SceneError.invalid(
        L10n.tr(
          "camera.importDigestMismatch",
          "Camera animation requires a matching source manifest digest. Open its original source before importing."
        ))
    }
    let spaces = incoming.manifest["spaces"] as? [String: Any] ?? [:]
    let clocks = incoming.manifest["clocks"] as? [String: Any] ?? [:]
    let tracks = try incoming.objects.map {
      try AuthoredCamera(importing: GSVCamera(document: incoming, object: $0))
    }
    for track in tracks {
      guard !self.authoredCameras.contains(where: { $0.id == track.id }),
        !document.objects.contains(where: { $0.id == track.id })
      else {
        throw SceneError.invalid(
          L10n.format(
            "camera.importDuplicate", "Camera identity already exists in this scene: %@.",
            track.label))
      }
      if let scene = self.scene,
        track.space != scene.object.space || track.clock != scene.object.clock
      {
        throw SceneError.invalid(
          L10n.tr(
            "camera.importDomainMismatch",
            "Imported camera and scene spaces/clocks differ. Explicit alignment is required."))
      }
      for field in ["spaces", "clocks"] {
        let key = field == "spaces" ? track.space : track.clock
        guard let imported = (incoming.manifest[field] as? [String: Any])?[key] as? NSDictionary
        else {
          throw SceneError.invalid(
            L10n.format(
              "camera.importDescriptorMissing",
              "Imported camera lacks its complete %@ descriptor.", field))
        }
        let supplemental = field == "spaces" ? self.authoredSpaces : self.authoredClocks
        if let existing =
          ((document.manifest[field] as? [String: Any])?[key] ?? supplemental[key])
          as? NSDictionary, !existing.isEqual(imported)
        {
          throw SceneError.invalid(
            L10n.format(
              "camera.importDescriptorMismatch",
              "Imported %@ descriptor disagrees with the source document.", field))
        }
      }
    }
    self.rememberCameraEdit(L10n.tr("camera.importTitle", "Import Camera Animation"))
    self.authoredCameras += tracks
    self.authoredSpaces.merge(spaces) { existing, _ in existing }
    self.authoredClocks.merge(clocks) { existing, _ in existing }
    self.camerasDirty = true
    if self.scene == nil, let track = tracks.first {
      self.viewportSpace = track.space
      self.viewportClock = track.clock
    }
    self.rebuildCameraEntries()
    self.refreshWorkspace()
    if let imported = tracks.last { self.selectObject(imported.id) }
    self.updateCameraOverlays()
    self.workspace.setStatus(
      L10n.format(
        "camera.imported", "Imported %d editable cameras; source reconstruction unchanged.",
        tracks.count))
  }

  private func saveScreenshot() {
    guard document != nil, let renderer else { return }
    let panel = NSSavePanel()
    panel.allowedContentTypes = [.png]
    panel.title = L10n.tr("file.screenshot", "Save Screenshot…")
    panel.nameFieldStringValue = "4DGS-Studio-viewport.png"
    guard let window = workspace.window else { return }
    panel.beginSheetModal(for: window) { [weak self] response in
      guard response == .OK, let url = panel.url else { return }
      let save: (Result<CGImage,Error>)->Void = { result in
        do {
          let image = try result.get()
          let bitmap = NSBitmapImageRep(cgImage: image)
          guard let data = bitmap.representation(using: .png, properties: [:]) else {
            throw SceneError.invalid(L10n.tr("error.pngEncoding", "PNG encoding failed."))
          }
          try data.write(to: url, options: .atomic)
          self?.workspace.setStatus(
            L10n.format(
              "file.screenshotSaved", "Saved %@. Inspection render; not an evaluation image.",
              url.lastPathComponent))
        } catch { self?.workspace.presentError(message: error.localizedDescription) }
      }
      if self?.comparisonEnabled == true {
        do {save(.success(try self!.workspace.comparisonView.exportImage()))} catch {save(.failure(error))}
      } else {renderer.captureImage(completion:save)}
    }
  }
  private func saveWorkspaceSnapshot() {
    guard document != nil, let renderer, let window = workspace.window else { return }
    setPlaying(false)
    workspace.prepareWorkspaceSnapshot(captureViewport: renderer.captureImage) { [weak self] result in
      guard let self else { return }
      do {
        let png = try result.get()
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.png]
        panel.title = L10n.tr("file.workspaceSnapshot", "Save Workspace Snapshot…")
        panel.nameFieldStringValue = "4DGS-Studio-workspace.png"
        panel.beginSheetModal(for: window) { [weak self] response in
          guard response == .OK, let url = panel.url, let self else { return }
          do {
            // Never recache UI while the save sheet is dimming the workspace.
            try png.write(to: url, options: .atomic)
            self.workspace.setStatus(
              L10n.tr(
                "file.workspaceSaved",
                "Saved workspace snapshot with current view and inspector state."))
          } catch { self.workspace.presentError(message: error.localizedDescription) }
        }
      } catch { self.workspace.presentError(message: error.localizedDescription) }
    }
  }
}
