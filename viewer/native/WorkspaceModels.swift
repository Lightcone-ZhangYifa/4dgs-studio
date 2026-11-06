import AppKit
import simd

// UI-only contracts. The document controller owns data, playback, and rendering.
struct WorkspaceObjectInfo {
  let id: String
  let label: String
  let type: String
  let encoding: String
  let supported: Bool
  var visible: Bool? = nil
  var space: String? = nil
  var clock: String? = nil
  var role: String? = nil
  var authored: Bool = false
  var cameraIndices: [Int] = []
}

struct WorkspaceSceneInfo {
  var name: String
  var url: URL
  var pointCount: Int
  var frameCount: Int
  var startTime: Double
  var endTime: Double
  var cameraNames: [String] = []
  var objects: [WorkspaceObjectInfo] = []
  var activeObjectID: String? = nil
  var timeUnit: String = "second"
  var previewRate: Double = 1
  var description: String? = nil
  var times: [Double] = []
  var activeSpace: String = "—"
  var activeClock: String = "—"
  var spaceDescription: String = ""
  var clockDescription: String = ""
}

struct WorkspaceSelectionInfo {
  var id: Int64
  var objectID: String? = nil
  var sourceLabel: String? = nil
  var isScaffold: Bool = false
  var position: SIMD3<Float>
  var scales: SIMD3<Float>
  var opacity: Float
  var color: SIMD3<Float>
  var colorSpace: String? = nil
  var quaternion: SIMD4<Float>? = nil
  var valid: Bool = true
  var confidence: Float? = nil
}

struct WorkspaceRenderOptions {
  var exposure: Float = 1
  var opacityScale: Float = 1
  var gaussianScale: Float = 1
  var nearClip: Float = 0.01
  var resolutionScale: Float = 1
  var background: SIMD4<Float> = SIMD4(0.055, 0.061, 0.071, 1)
  var showGrid: Bool = true
  var showOrbitCenter: Bool = true
}

struct WorkspacePerformance {
  var fps: Double? = nil
  var gpuMilliseconds: Double? = nil
  var visibleCount: Int = 0
  var totalCount: Int = 0
  var residentBytes: Int = 0
  var sortMilliseconds: Double? = nil
}

struct WorkspaceActions {
  var onToggleComparison: ((Bool) -> Void)?
  var onSelectComparison: ((Int) -> Void)?
  var onActivateRenderView: ((String) -> Void)?
  var onCompareObservation: ((String) -> Void)?
  var onOpenURLs: (([URL]) -> Void)?
  var onScrubTime: ((Double) -> Void)?
  var onSetPreviewRange: ((ClosedRange<Double>?) -> Void)?
  var onRetimeCameraKey: ((Int, Double) -> Void)?
  var onCameraTransform: ((SIMD3<Float>, SIMD4<Float>) -> Void)?
  var onInspectCamera: ((Int) -> Void)?
  var onClearSelection: (() -> Void)?
  var onSeekSeconds: ((Double) -> Void)?
  var onSeekFrame: ((Int) -> Void)?
  var onPlaybackChanged: ((Bool) -> Void)?
  var onPlaybackSpeedChanged: ((Double) -> Void)?
  var onPreviewRateChanged: ((Double) -> Void)?
  var onLoopChanged: ((Bool) -> Void)?
  var onSelectCamera: ((Int?) -> Void)?
  var onSetObjectVisible: ((String, Bool) -> Void)?
  var onSelectObject: ((String) -> Void)?
  var onSelectSelections: ((StudioSelectionSet) -> Void)?
  var onCameraPreset: ((String) -> Void)?
  var onShowTrajectory: ((Bool) -> Void)?
  var onSelectID: ((Int64) -> Void)?
  var onFocusSelection: (() -> Void)?
  var onResetCamera: (() -> Void)?
  var onOrbitCenterSelection: (() -> Void)?
  var onOrbitCenterView: (() -> Void)?
  var onTrackballOrbit: ((Bool) -> Void)?
  var onOrbit: ((SIMD2<Float>) -> Void)?
  var onPan: ((SIMD2<Float>) -> Void)?
  var onRoll: ((Float) -> Void)?
  var onDollyAt: ((Float, SIMD2<Float>) -> Void)?
  var onNavigationBoundary: ((Bool) -> Void)?
  var onPreviousView: (() -> Void)?
  var onNextView: (() -> Void)?
  var onUpAxis: ((String) -> Void)?
  var onDolly: ((Float) -> Void)?
  var onPick: ((SIMD2<UInt32>, StudioSelectionOperation) -> Void)?
  var onTrajectoryOptionsChanged: ((TrajectoryDisplayOptions) -> Void)?
  var onRenderOptionsChanged: ((WorkspaceRenderOptions) -> Void)?
  var onCancelLoading: (() -> Void)?
  var onCreateCamera: (() -> Void)?
  var onRecordCameraKeyframe: (() -> Void)?
  var onSaveCameras: (() -> Void)?
  var onImportCameras: (() -> Void)?
  var onRenameCamera: ((String) -> Void)?
  var onDeleteCamera: (() -> Void)?
  var onJumpCameraKey: ((Int) -> Void)?
  var onDeleteCameraKey: ((Int) -> Void)?
  var onCameraFOV: ((Double) -> Void)?
  var onUndoCamera: (() -> Void)?
  var onRedoCamera: (() -> Void)?
  var onShowCameras: ((Bool) -> Void)?
  var onExportScreenshot: (() -> Void)?
  var onExportWorkspace: (() -> Void)?
  var onCloseRequested: (() -> Bool)?
}

struct WorkspaceCameraInspection {
  var objectID: String
  var entry: Int
  var name: String
  var pose: simd_float4x4
  var intrinsics: simd_float3x3
  var size: SIMD2<Float>
  var space: String
  var clock: String
  var role: String
  var integerPixelCenters: Bool
  var authored: Bool
  var sampleTime: Double = 0
  var followsActiveClock: Bool = true
  var captureDetails: String = ""
}

/// Groups describe peer object roles, never an invented transform hierarchy.
enum SceneCategory: String, CaseIterable {
  case representations, cameras, motion, observations, appearance, rendering, data
  static func of(_ type: String) -> SceneCategory {
    switch type {
    case "gaussian", "geometry", "pointcloud", "point_cloud", "mesh": return .representations
    case "camera": return .cameras
    case "scaffold", "motion", "transform", "binding", "tracks", "track": return .motion
    case "observation", "image", "depth": return .observations
    case "appearance": return .appearance
    case "render": return .rendering
    default: return .data
    }
  }
  var symbol: String {
    switch self {
    case .representations: return "cube.transparent"
    case .cameras: return "camera"
    case .motion: return "point.3.connected.trianglepath.dotted"
    case .observations: return "photo.on.rectangle"
    case .appearance: return "paintpalette"
    case .rendering: return "camera.aperture"
    case .data: return "externaldrive"
    }
  }
}
