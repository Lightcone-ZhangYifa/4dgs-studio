import AppKit
import MetalKit
import simd

struct GaussianGPURecord {
    var mean: SIMD4<Float>
    var scale: SIMD4<Float>
    var quat: SIMD4<Float>
    var color: SIMD4<Float>
}

struct GaussianRenderSettings {
    var exposure: Float = 0
    var opacityScale: Float = 1
    var gaussianScale: Float = 1
    var nearClip: Float = 0.001
    var farClip: Float = .infinity
    var nearInclusive = false
    var farInclusive = true
    var divideEpsilon: Float = 0
    var pixelOffset = SIMD2<Float>.zero
    var jacobianMode: Float = 0 // image bounds; 1 symmetric FOV, 2 unclamped
    var jacobianFactor: Float = 1.3
    var covarianceDilation: Float = 0.3
    var background = SIMD4<Float>(0.018, 0.025, 0.04, 1)
    var showGrid = true
    var showOrbitCenter = false
}

struct GaussianRenderMetrics {
    var fps: Double
    var gpuMilliseconds: Double
    var visibleCount: Int
    var totalCount: Int
    var residentBytes: Int
    var sortMilliseconds: Double?
    var projectionMilliseconds: Double
    var renderMilliseconds: Double
    var droppedSubmissions: Int
    var completedFrameCount: UInt64
}

struct ProjectedGaussianRecord {
    var centerDepth: SIMD4<Float>
    var axes: SIMD4<Float>
    var color: SIMD4<Float>
    var info: SIMD4<UInt32>
}
#if STUDIO_TESTING
struct GaussianDebugFrame {
    var bgra: [UInt8]
    var width: Int
    var height: Int
    var projected: [ProjectedGaussianRecord]
    var sortedIndices: [UInt32]
    var visibleCount: Int
    var pickedIndex: UInt32?
    var scenePick: ScenePickResult = .empty
}

#endif

private struct GaussianUniforms {
    var worldToCamera: simd_float4x4
    var intrinsics: SIMD4<Float>
    var viewport: SIMD4<Float>
    var controls: SIMD4<Float>
    var counts: SIMD4<UInt32>
    var depthPolicy: SIMD4<Float>
    var projectionPolicy: SIMD4<Float>
    var filterPolicy: SIMD4<Float>
}

private struct CalibratedCamera {
    var worldToCamera: simd_float4x4
    var K: simd_float3x3
    var imageSize: SIMD2<Float>
    var integerPixelCenters: Bool
}

private struct RenderState {
    var a: MTLBuffer?
    var b: MTLBuffer?
    var count = 0
    var interpolation: Float = 0
    var target = SIMD3<Float>.zero
    var distance: Float = 4
    var yaw: Float = .pi / 5
    var pitch: Float = .pi / 9
    var up = SIMD3<Float>(0,1,0)
    var trackballOrbit = false
    var selected: UInt32 = .max
    var selectedIndices: [UInt32] = []
    var selectedMask: MTLBuffer?
    var selectionBuffer: MTLBuffer?
    var trajectoryGeometry: TrajectoryGeometry?
    var trajectoryOptions = TrajectoryDisplayOptions()
    var trajectoryTime: Double = 0
    var trailStartTime: Double = 0
    var cameraSegments: MTLBuffer?
    var cameraSegmentCount = 0
    var cameraHasTranslucentSegments = false
    var cameraPickSegments: MTLBuffer?
    var cameraPickSegmentCount = 0
    var settings = GaussianRenderSettings()
    var calibrated: CalibratedCamera?
    var pick: SIMD2<UInt32>?
    var scenePickTolerance: Float?
    var sceneRevision: UInt64 = 0
    var pickRevision: UInt64 = 0
    var graphLines: MTLBuffer?
    var graphLineCount = 0
}

private struct NavigationViewState {
    let target: SIMD3<Float>
    let distance: Float
    let yaw: Float
    let pitch: Float
    let up: SIMD3<Float>
    let calibrated: CalibratedCamera?
    init(_ state: RenderState) {
        target=state.target; distance=state.distance; yaw=state.yaw; pitch=state.pitch; up=state.up; calibrated=state.calibrated
    }
    func apply(to state: inout RenderState) {
        state.target=target; state.distance=distance; state.yaw=yaw; state.pitch=pitch; state.up=up; state.calibrated=calibrated
    }
    func differs(from other: NavigationViewState) -> Bool {
        if target != other.target || distance != other.distance || yaw != other.yaw || pitch != other.pitch || up != other.up { return true }
        switch (calibrated,other.calibrated) {
        case (.none,.none): return false
        case (.some(let a),.some(let b)): return a.worldToCamera != b.worldToCamera || a.K != b.K || a.imageSize != b.imageSize
        default: return true
        }
    }
}

/// Internal so deterministic allocation-failure probes can exercise the cache
/// transaction without submitting GPU commands.
final class FrameResources {
    var capacity = 0
    var leased = false
    var retireRequested = false
    var projected: MTLBuffer!
    var indicesA: MTLBuffer!
    var indicesB: MTLBuffer!
    var histogram: MTLBuffer!
    var prefixes: MTLBuffer!
    var totals: MTLBuffer!
    var visible: MTLBuffer!
    var indirect: MTLBuffer!
    var pick: MTLBuffer!
    var sourceA: MTLBuffer?
    var sourceB: MTLBuffer?
    var overlayDepth: MTLTexture?
    var overlayAccumulation: MTLTexture?
    var overlayRevealage: MTLTexture?
    /// Called only while this slot is leased to a new frame, after its prior
    /// GPU use completed. Publish all required attachments together.
    func allocateOverlays(device: MTLDevice, width: Int, height: Int, needsTransparency: Bool,
                          textureFactory: ((MTLTextureDescriptor) -> MTLTexture?)? = nil) throws {
        guard width > 0, height > 0, width <= 16384, height <= 16384 else {
            throw RendererError.message("Preview dimensions exceed the bounded texture capacity")
        }
        let sizeChanged = overlayDepth?.width != width || overlayDepth?.height != height
        if !needsTransparency { overlayAccumulation = nil; overlayRevealage = nil }
        if !sizeChanged && (!needsTransparency || (overlayAccumulation != nil && overlayRevealage != nil)) { return }
        func texture(_ format: MTLPixelFormat, _ label: String) throws -> MTLTexture {
            let descriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: format, width: width, height: height, mipmapped: false)
            descriptor.storageMode = .private; descriptor.usage = [.renderTarget, .shaderRead]
            guard let result = (textureFactory != nil ? textureFactory!(descriptor) : device.makeTexture(descriptor: descriptor)) else { throw RendererError.message("Cannot allocate \(label)") }
            result.label = label; return result
        }
        let nextDepth = sizeChanged ? try texture(.depth32Float, "Preview occlusion depth") : overlayDepth
        let nextAccumulation = needsTransparency ? try texture(.rgba32Float, "Order-independent path accumulation") : nil
        let nextRevealage = needsTransparency ? try texture(.r16Float, "Order-independent path transmittance") : nil
        overlayDepth = nextDepth; overlayAccumulation = nextAccumulation; overlayRevealage = nextRevealage
    }
    func retire() { if leased { retireRequested = true } else { reset() } }
    func finishLease() {
        sourceA = nil; sourceB = nil
        if retireRequested { reset() }
        leased = false
    }
    /// Retire caches only after the command buffer releases this frame slot.
    func reset() {
        projected = nil; indicesA = nil; indicesB = nil; histogram = nil; prefixes = nil; totals = nil
        visible = nil; indirect = nil; pick = nil; sourceA = nil; sourceB = nil
        overlayDepth = nil; overlayAccumulation = nil; overlayRevealage = nil
        capacity = 0; retireRequested = false
    }
    var bytes: Int {
        let buffers = [projected, indicesA, indicesB, histogram, prefixes, totals, visible, indirect, pick]
            .compactMap { $0?.length }.reduce(0, +)
        let overlayBytes = (overlayDepth.map { $0.width*$0.height*4 } ?? 0) + (overlayAccumulation.map { $0.width*$0.height*16 } ?? 0) + (overlayRevealage.map { $0.width*$0.height*2 } ?? 0)
        return buffers + overlayBytes
    }
    func allocate(device: MTLDevice, count: Int,
                  bufferFactory: ((Int, MTLResourceOptions) -> MTLBuffer?)? = nil) throws {
        guard count > 0, count <= Int(UInt32.max), count <= device.maxBufferLength / 64 else {
            throw RendererError.message("Gaussian frame exceeds the device buffer capacity")
        }
        guard count > capacity else { return }
        func buffer(_ length: Int, _ label: String, shared: Bool = false) throws -> MTLBuffer {
            let options: MTLResourceOptions = shared ? .storageModeShared : .storageModePrivate
            guard let b = (bufferFactory != nil ? bufferFactory!(max(length,16),options) : device.makeBuffer(length:max(length,16),options:options)) else {
                throw RendererError.message("Metal allocation failed: \(label), \(length) bytes")
            }
            b.label = label; return b
        }
        // A failed allocation leaves the previous complete cache intact. In
        // particular, capacity must never describe partially replaced buffers.
        let nextProjected = try buffer(count * 64, "Projected ellipses")
        let nextIndicesA = try buffer(count * 8, "Radix index A")
        let nextIndicesB = try buffer(count * 8, "Radix index B")
        let groups = (count + 255) / 256
        let nextHistogram = try buffer(groups * 16 * 4, "Radix group histogram")
        let nextPrefixes = try buffer(groups * 16 * 4, "Radix group prefixes")
        let nextTotals = try buffer(16 * 4, "Radix digit totals")
        let nextVisible = try buffer(16, "Exact visible count", shared: true)
        let nextIndirect = try buffer(16, "Indirect draw arguments")
        let nextPick = try buffer(16, "GPU Gaussian/camera pick result", shared: true)
        projected = nextProjected; indicesA = nextIndicesA; indicesB = nextIndicesB
        histogram = nextHistogram; prefixes = nextPrefixes; totals = nextTotals
        visible = nextVisible; indirect = nextIndirect; pick = nextPick
        capacity = count
    }

}

enum RendererError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
}

/// Native all-point Gaussian renderer. Interactive frames never map or scan N
/// records on the main thread. Projection, culling, stable radix sorting and
/// picking run on the GPU. Three in-flight resource sets retain source slabs.
final class GaussianRenderer: NSObject, MTKViewDelegate {
    let device: MTLDevice
    private let commandQueue: MTLCommandQueue
    private let projectPipeline: MTLComputePipelineState
    private let histogramPipeline: MTLComputePipelineState
    private let prefixPipeline: MTLComputePipelineState
    private let scatterPipeline: MTLComputePipelineState
    private let argumentsPipeline: MTLComputePipelineState
    private let pickDepthPipeline: MTLComputePipelineState
    private let pickIndexPipeline: MTLComputePipelineState
    private let cameraPickDepthPipeline: MTLComputePipelineState
    private let cameraPickIndexPipeline: MTLComputePipelineState
    private let gaussianPipeline: MTLRenderPipelineState
    private let comparisonPipeline: MTLRenderPipelineState
    private let pivotPipeline: MTLRenderPipelineState
    private let gridPipeline: MTLRenderPipelineState
    private let scaffoldPipeline: MTLRenderPipelineState
    private let gaussianDepthPipeline: MTLRenderPipelineState
    private let trailPipeline: MTLRenderPipelineState
    private let coloredLinePipeline: MTLRenderPipelineState
    private let pathAccumulationPipeline: MTLRenderPipelineState
    private let cameraAccumulationPipeline: MTLRenderPipelineState
    private let pathCompositePipeline: MTLRenderPipelineState
    private let selectionPipeline: MTLRenderPipelineState
    private let overlayDepthWrite: MTLDepthStencilState
    private let overlayDepthRead: MTLDepthStencilState
    private let zeroSelectionMask: MTLBuffer
    private weak var view: MTKView?
    private var state = RenderState()
    private var navigationStart: NavigationViewState?
    private var previousViews = [NavigationViewState]()
    private var nextViews = [NavigationViewState]()
    private let stateLock = NSLock()
    private let inFlight = DispatchSemaphore(value: 3)
    private let resources = [FrameResources(), FrameResources(), FrameResources()]
    private let resourceLock = NSLock()
    private var resourceIndex = 0
    private let metricsLock = NSLock()
    private var completionTimes = [Double]()
    private var dropped = 0
    private var totalCompletedFrames: UInt64 = 0
    private var drawRequestQueued = false
    private var retryWhenAvailable = false
    private var idleMetricsWork: DispatchWorkItem?
    var onMetrics: ((GaussianRenderMetrics) -> Void)?
    var onScenePick: ((ScenePickResult) -> Void)?
    var onError: ((String) -> Void)?

    init(view: MTKView, shaderURL: URL? = nil) throws {
        guard let gpu = view.device ?? MTLCreateSystemDefaultDevice(), let queue = gpu.makeCommandQueue() else {
            throw RendererError.message("No Metal GPU or command queue is available")
        }
        guard MemoryLayout<GaussianGPURecord>.stride == 64, MemoryLayout<ProjectedGaussianRecord>.stride == 64,
              MemoryLayout<GaussianUniforms>.stride == 176,
              MemoryLayout<GaussianUniforms>.offset(of: \.depthPolicy) == 128,
              MemoryLayout<GaussianUniforms>.offset(of: \.projectionPolicy) == 144,
              MemoryLayout<GaussianUniforms>.offset(of: \.filterPolicy) == 160, MemoryLayout<TrajectorySegmentGPU>.stride == 64, MemoryLayout<CameraPickSegmentGPU>.stride == 48 else {
            throw RendererError.message("Gaussian binary/Metal uniform ABI mismatch")
        }
        device = gpu; commandQueue = queue
        guard let shader = shaderURL ?? Bundle.main.url(forResource: "Shaders", withExtension: "metal") else {
            throw RendererError.message("Shaders.metal is missing from the application resources")
        }
        let options = MTLCompileOptions(); options.languageVersion = .version3_0
        let shaderData = try Data(contentsOf: shader)
        let library = try gpu.makeLibrary(source: String(decoding: shaderData, as: UTF8.self), options: options)
        func compute(_ name: String) throws -> MTLComputePipelineState {
            guard let function = library.makeFunction(name: name) else { throw RendererError.message("Missing Metal kernel \(name)") }
            return try gpu.makeComputePipelineState(function: function)
        }
        projectPipeline = try compute("projectGaussians")
        histogramPipeline = try compute("radixHistogram")
        prefixPipeline = try compute("radixPrefix")
        scatterPipeline = try compute("radixScatter")
        argumentsPipeline = try compute("makeDrawArguments")
        pickDepthPipeline = try compute("pickDepth")
        pickIndexPipeline = try compute("pickIndex")
        cameraPickDepthPipeline = try compute("cameraPickDepth")
        cameraPickIndexPipeline = try compute("cameraPickIndex")
        guard scatterPipeline.threadExecutionWidth == 32 else {
            throw RendererError.message("This radix implementation requires 32-lane Apple GPU SIMD groups")
        }
        func render(_ vertex: String, _ fragment: String, format: MTLPixelFormat = .bgra8Unorm, depth: MTLPixelFormat = .invalid, opaque: Bool = false) throws -> MTLRenderPipelineState {
            guard let vertexFunction = library.makeFunction(name: vertex),
                  let fragmentFunction = library.makeFunction(name: fragment) else {
                throw RendererError.message("Missing Metal render function: \(vertex) / \(fragment)")
            }
            let descriptor = MTLRenderPipelineDescriptor()
            descriptor.vertexFunction = vertexFunction
            descriptor.fragmentFunction = fragmentFunction
            descriptor.colorAttachments[0].pixelFormat = format
            descriptor.depthAttachmentPixelFormat = depth
            let attachment = descriptor.colorAttachments[0]!
            attachment.isBlendingEnabled = format != .invalid && !opaque
            attachment.rgbBlendOperation = .add; attachment.alphaBlendOperation = .add
            attachment.sourceRGBBlendFactor = .one; attachment.destinationRGBBlendFactor = .oneMinusSourceAlpha
            attachment.sourceAlphaBlendFactor = .one; attachment.destinationAlphaBlendFactor = .oneMinusSourceAlpha
            return try gpu.makeRenderPipelineState(descriptor: descriptor)
        }
        gaussianPipeline = try render("gaussianVertex", "gaussianFragment")
        comparisonPipeline = try render("gaussianVertex", "gaussianFragment", format: .rgba32Float)
        gridPipeline = try render("gridVertex", "gridFragment")
        pivotPipeline = try render("pivotVertex", "gridFragment")
        scaffoldPipeline = try render("scaffoldLineVertex", "gridFragment")
        gaussianDepthPipeline = try render("gaussianDepthVertex", "gaussianDepthFragment", format: .invalid, depth: .depth32Float)
        trailPipeline = try render("coloredTrajectoryVertex", "lineFragment", depth: .depth32Float, opaque: true)
        coloredLinePipeline = try render("coloredCameraVertex", "cameraOpaqueFragment", depth: .depth32Float, opaque: true)
        selectionPipeline = try render("selectionMarkerVertex", "selectionMarkerFragment", depth: .depth32Float, opaque: true)
        pathCompositePipeline = try render("pathCompositeVertex", "pathCompositeFragment")
        let paths = MTLRenderPipelineDescriptor()
        paths.vertexFunction = library.makeFunction(name: "coloredTrajectoryVertex")
        paths.fragmentFunction = library.makeFunction(name: "pathAccumulationFragment")
        paths.depthAttachmentPixelFormat = .depth32Float
        let accumulation = paths.colorAttachments[0]!
        accumulation.pixelFormat = .rgba32Float; accumulation.isBlendingEnabled = true
        accumulation.sourceRGBBlendFactor = .one; accumulation.destinationRGBBlendFactor = .one
        accumulation.sourceAlphaBlendFactor = .one; accumulation.destinationAlphaBlendFactor = .one
        let revealage = paths.colorAttachments[1]!
        revealage.pixelFormat = .r16Float; revealage.isBlendingEnabled = true
        revealage.sourceRGBBlendFactor = .zero; revealage.destinationRGBBlendFactor = .oneMinusSourceColor
        pathAccumulationPipeline = try gpu.makeRenderPipelineState(descriptor: paths)
        paths.vertexFunction = library.makeFunction(name: "coloredCameraVertex")
        paths.fragmentFunction = library.makeFunction(name: "cameraAccumulationFragment")
        cameraAccumulationPipeline = try gpu.makeRenderPipelineState(descriptor: paths)
        let write = MTLDepthStencilDescriptor(); write.depthCompareFunction = .lessEqual; write.isDepthWriteEnabled = true
        let read = MTLDepthStencilDescriptor(); read.depthCompareFunction = .lessEqual; read.isDepthWriteEnabled = false
        guard let depthWrite = gpu.makeDepthStencilState(descriptor: write), let depthRead = gpu.makeDepthStencilState(descriptor: read),
              let zeroMask = gpu.makeBuffer(length: 1, options: .storageModeShared) else { throw RendererError.message("Cannot allocate overlay/selection state") }
        overlayDepthWrite = depthWrite; overlayDepthRead = depthRead; zeroSelectionMask = zeroMask
        zeroMask.contents().storeBytes(of: UInt8(0), as: UInt8.self)
        super.init()
        self.view = view
        view.device = gpu; view.colorPixelFormat = .bgra8Unorm
        view.depthStencilPixelFormat = .invalid; view.sampleCount = 1
        view.framebufferOnly = false; view.preferredFramesPerSecond = 60
        view.isPaused = true; view.enableSetNeedsDisplay = true; view.delegate = self
        requestDraw()
    }

    /// Buffers may contain page padding; only count*64 bytes are read. Sources
    /// must remain immutable while retained by this renderer/in-flight commands.
    func setFrames(a: MTLBuffer, b: MTLBuffer, count: Int, interpolation: Float) throws {
        guard count > 0, count <= Int(UInt32.max), count <= Int.max / 64,
              a.length >= count * 64, b.length >= count * 64,
              a.device.registryID == device.registryID, b.device.registryID == device.registryID,
              interpolation.isFinite else { throw RendererError.message("Invalid Gaussian buffers/count/interpolation") }
        stateLock.lock(); defer { stateLock.unlock(); requestDraw() }
        if state.selectedMask?.length != count {
            state.selectedMask = makeSelectionMask(count: count, indices: state.selectedIndices)
        }
        state.a = a; state.b = b; state.count = count; state.interpolation = min(max(interpolation, 0), 1)
    }
    func clear() {
        stateLock.lock()
        state.a = nil; state.b = nil; state.count = 0; state.pick = nil; state.scenePickTolerance = nil
        state.pickRevision &+= 1; state.sceneRevision &+= 1; state.selected = .max; state.selectedIndices = []
        state.selectedMask = nil; state.selectionBuffer = nil; state.trajectoryGeometry = nil
        state.cameraSegments = nil; state.cameraSegmentCount = 0; state.cameraHasTranslucentSegments = false
        state.cameraPickSegments = nil; state.cameraPickSegmentCount = 0
        state.graphLines = nil; state.graphLineCount = 0; stateLock.unlock()
        resourceLock.lock()
        for frame in resources { frame.retire() }
        resourceLock.unlock()
        metricsLock.lock(); completionTimes.removeAll(); dropped = 0; metricsLock.unlock()
        requestDraw()
    }

    func updateSettings(_ settings: GaussianRenderSettings) {
        guard settings.exposure.isFinite, settings.opacityScale.isFinite, settings.gaussianScale.isFinite,
              settings.nearClip.isFinite, settings.opacityScale >= 0, settings.gaussianScale > 0, settings.nearClip > 0 else { return }
        stateLock.lock(); state.settings = settings; stateLock.unlock(); requestDraw()
    }
    private func makeSelectionMask(count: Int, indices: [UInt32]) -> MTLBuffer? {
        guard count > 0, let buffer = device.makeBuffer(length: count, options: .storageModeShared) else { return nil }
        memset(buffer.contents(), 0, count)
        let mask = buffer.contents().assumingMemoryBound(to: UInt8.self)
        for index in indices where index < UInt32(count) { mask[Int(index)] = 1 }
        return buffer
    }
    func select(indices: [UInt32]) {
        let ordered = Array(Set(indices)).sorted()
        let buffer: MTLBuffer? = ordered.isEmpty ? nil : ordered.withUnsafeBytes {
            device.makeBuffer(bytes: $0.baseAddress!, length: $0.count, options: .storageModeShared)
        }
        stateLock.lock()
        state.pick = nil; state.pickRevision &+= 1; state.selected = ordered.first ?? .max
        state.selectedIndices = ordered; state.selectedMask = makeSelectionMask(count: state.count, indices: ordered); state.selectionBuffer = buffer
        stateLock.unlock(); requestDraw()
    }
    func requestScenePick(pixel: SIMD2<UInt32>, tolerancePixels: Float) {
        guard tolerancePixels.isFinite, tolerancePixels >= 0 else { return }
        stateLock.lock(); state.pickRevision &+= 1; state.pick = pixel
        state.scenePickTolerance = tolerancePixels; stateLock.unlock(); requestDraw()
    }
    /// Fence source table replacement without changing the current selection.
    func invalidatePicks() {
        stateLock.lock(); state.pick = nil; state.scenePickTolerance = nil; state.pickRevision &+= 1; stateLock.unlock()
    }
    private func scenePickResult(_ buffer: MTLBuffer, gaussianCount: Int) -> ScenePickResult {
        let value = buffer.contents().assumingMemoryBound(to: UInt32.self)
        if value[2] != .max, value[3] <= value[1] { return .camera(value[2]) }
        if value[0] < UInt32(gaussianCount) { return .gaussian(value[0]) }
        return .empty
    }
    /// History captures camera-only state once per completed gesture, never point buffers.
    func beginNavigation() {
        stateLock.lock(); if navigationStart == nil { navigationStart=NavigationViewState(state) }; stateLock.unlock()
    }
    func endNavigation() {
        stateLock.lock(); defer { stateLock.unlock() }
        if let start=navigationStart, start.differs(from:NavigationViewState(state)) {
            previousViews.append(start); if previousViews.count>64 { previousViews.removeFirst() }; nextViews.removeAll()
        }
        navigationStart=nil
    }
    @discardableResult func previousView() -> Bool {
        endNavigation(); stateLock.lock()
        guard let value=previousViews.popLast() else { stateLock.unlock(); return false }
        nextViews.append(NavigationViewState(state)); value.apply(to:&state); stateLock.unlock(); requestDraw(); return true
    }
    @discardableResult func nextView() -> Bool {
        endNavigation(); stateLock.lock()
        guard let value=nextViews.popLast() else { stateLock.unlock(); return false }
        previousViews.append(NavigationViewState(state)); value.apply(to:&state); stateLock.unlock(); requestDraw(); return true
    }
    func clearNavigationHistory() {
        stateLock.lock(); navigationStart=nil; previousViews.removeAll(); nextViews.removeAll(); stateLock.unlock()
    }
    /// The axis is a navigation/grid preference, never an implicit data transform.
    func setUpAxis(_ name: String) {
        guard let axis=NavigationGeometry.upAxis(name) else { return }
        stateLock.lock()
        // Preserve the actual pose when changing the navigation convention.
        if state.calibrated == nil { state.calibrated=navigationCamera(state) }
        state.up=axis; stateLock.unlock(); requestDraw()
    }
    func setOrbitPivot(position: SIMD3<Float>) {
        guard position.x.isFinite,position.y.isFinite,position.z.isFinite else { return }
        stateLock.lock(); defer { stateLock.unlock() }
        let camera=navigationCamera(state),pose=camera.worldToCamera.inverse
        state.calibrated=camera; state.target=position
        state.distance=max(0.0001,simd_length(NavigationGeometry.vector(pose[3])-position))
    }
    func resetCamera(center: SIMD3<Float> = .zero, radius: Float = 3) {
        guard center.x.isFinite,center.y.isFinite,center.z.isFinite,radius.isFinite else { return }
        beginNavigation()
        stateLock.lock(); state.target = center; state.distance = max(radius * 2.8, 0.05)
        state.yaw = .pi / 5; state.pitch = .pi / 9; state.calibrated = nil; stateLock.unlock()
        endNavigation(); requestDraw()
    }
    func focus(position: SIMD3<Float>, radius: Float = 0.5) {
        guard position.x.isFinite,position.y.isFinite,position.z.isFinite,radius.isFinite else { return }
        beginNavigation(); stateLock.lock()
        var camera=navigationCamera(state),pose=camera.worldToCamera.inverse
        let shortFOV=min(2*atan(camera.imageSize.x/(2*camera.K[0,0])),2*atan(camera.imageSize.y/(2*camera.K[1,1])))
        state.distance=max(0.02,max(radius,0.001)*1.12/max(0.01,sin(shortFOV*0.5)))
        state.target=position
        pose[3]=SIMD4(position-NavigationGeometry.vector(pose[2])*state.distance,1)
        camera.worldToCamera=pose.inverse; state.calibrated=camera
        stateLock.unlock(); endNavigation(); requestDraw()
    }
    func setTrackballOrbit(_ enabled: Bool) {
        stateLock.lock(); state.trackballOrbit = enabled; stateLock.unlock()
    }
    func orbitCenter() -> SIMD3<Float> { stateLock.lock(); defer { stateLock.unlock() }; return state.target }
    /// Default orbit uses the declared navigation up axis; free trackball is explicit.
    func orbit(delta: SIMD2<Float>) {
        guard delta.x.isFinite,delta.y.isFinite,delta != .zero else { return }
        stateLock.lock(); defer { stateLock.unlock(); requestDraw() }
        var camera=navigationCamera(state)
        let pose=camera.worldToCamera.inverse
        if state.trackballOrbit {
            let right=NavigationGeometry.vector(pose[0]),down=NavigationGeometry.vector(pose[1])
            let rotation=simd_quatf(angle:delta.y*0.006,axis:right)*simd_quatf(angle:delta.x*0.006,axis:down)
            camera.worldToCamera=NavigationGeometry.rotate(pose,around:state.target,rotation:rotation).inverse
        } else {
            camera.worldToCamera=NavigationGeometry.turntable(pose,pivot:state.target,up:state.up,delta:delta).inverse
        }
        state.calibrated=camera
    }
    func pan(delta: SIMD2<Float>) {
        guard delta.x.isFinite,delta.y.isFinite,delta != .zero else { return }
        stateLock.lock(); defer { stateLock.unlock(); requestDraw() }
        var camera=navigationCamera(state),pose=camera.worldToCamera.inverse
        let size=navigationViewportSize()
        let k=effectiveNavigationK(camera,width:Float(size.width),height:Float(size.height))
        let depth=max(0.0001,simd_dot(state.target-NavigationGeometry.vector(pose[3]),NavigationGeometry.vector(pose[2])))
        let shift=NavigationGeometry.panShift(pose:pose,delta:delta,depth:depth,k:k)
        pose[3] += SIMD4(shift,0); state.target += shift
        camera.worldToCamera=pose.inverse; state.calibrated=camera
    }
    func dolly(delta: Float) { dolly(delta:delta,anchor:nil) }
    /// Cursor anchoring is on the current pivot plane, not an invented depth pick.
    func dolly(delta: Float, anchor: SIMD2<Float>?) {
        guard delta.isFinite,delta != 0 else { return }
        stateLock.lock(); defer { stateLock.unlock(); requestDraw() }
        var camera=navigationCamera(state),pose=camera.worldToCamera.inverse
        let nextDistance=min(max(state.distance*exp(min(20,max(-20,delta*0.025))),0.0001),1e8)
        let ratio=nextDistance/max(state.distance,0.0001)
        let origin=NavigationGeometry.vector(pose[3])
        var anchorWorld=state.target
        if let anchor,anchor.x.isFinite,anchor.y.isFinite {
            let size=navigationViewportSize()
            let k=effectiveNavigationK(camera,width:Float(size.width),height:Float(size.height))
            let depth=max(0.0001,simd_dot(state.target-origin,NavigationGeometry.vector(pose[2])))
            anchorWorld=NavigationGeometry.pointOnPlane(pose:pose,pixel:anchor*SIMD2(Float(size.width),Float(size.height)),depth:depth,k:k)
        }
        pose[3]=SIMD4(anchorWorld+(origin-anchorWorld)*ratio,1)
        state.target=anchorWorld+(state.target-anchorWorld)*ratio
        state.distance=nextDistance; camera.worldToCamera=pose.inverse; state.calibrated=camera
    }
    func roll(radians: Float) {
        guard radians.isFinite,radians != 0 else { return }
        stateLock.lock(); defer { stateLock.unlock(); requestDraw() }
        var camera=navigationCamera(state)
        let pose=camera.worldToCamera.inverse
        let rotation=simd_quatf(angle:radians,axis:NavigationGeometry.vector(pose[2]))
        // Roll about the optical center, preserving location and focal calibration.
        camera.worldToCamera=NavigationGeometry.rotate(pose,around:NavigationGeometry.vector(pose[3]),rotation:rotation).inverse
        state.calibrated=camera
    }
    private func navigationViewportSize() -> CGSize {
        let size=view?.bounds.size ?? CGSize(width:1280,height:720)
        return CGSize(width:max(1,size.width),height:max(1,size.height))
    }
    private func effectiveNavigationK(_ camera: CalibratedCamera,width:Float,height:Float) -> simd_float3x3 {
        let scale=min(width/camera.imageSize.x,height/camera.imageSize.y)
        let offset=(SIMD2(width,height)-camera.imageSize*scale)*0.5
        var k=camera.K
        k[0,0] *= scale; k[1,1] *= scale; k[1,0] *= scale
        let half:Float=camera.integerPixelCenters ? 0.5:0
        k[2,0]=(k[2,0]+half)*scale+offset.x; k[2,1]=(k[2,1]+half)*scale+offset.y
        return k
    }
    private func navigationCamera(_ state: RenderState) -> CalibratedCamera {
        if let camera=state.calibrated { return camera }
        // Keep generated camera image dimensions in actual render pixels. Pan
        // separately converts to AppKit points through effectiveNavigationK.
        let size=view?.drawableSize ?? CGSize(width:1280,height:720)
        let width=max(1,Int(size.width)),height=max(1,Int(size.height))
        let u=uniforms(state,width:width,height:height)
        var k=matrix_identity_float3x3
        k[0,0]=u.intrinsics.x; k[1,1]=u.intrinsics.y; k[2,0]=u.intrinsics.z; k[2,1]=u.intrinsics.w; k[1,0]=u.viewport.z
        return CalibratedCamera(worldToCamera:u.worldToCamera,K:k,imageSize:SIMD2(Float(width),Float(height)),integerPixelCenters:false)
    }
    func setCalibratedCamera(cameraToWorld: simd_float4x4, intrinsics: simd_float3x3,
                             imageSize: SIMD2<Float>, integerPixelCenters: Bool = true) {
        guard imageSize.x > 0, imageSize.y > 0 else { return }
        stateLock.lock()
        let origin = SIMD3(cameraToWorld[3].x,cameraToWorld[3].y,cameraToWorld[3].z)
        // A source camera update must never silently move an explicit orbit pivot.
        let distance = simd_length(state.target - origin)
        if distance.isFinite && distance > 0.0001 { state.distance = distance }
        state.calibrated = CalibratedCamera(worldToCamera: cameraToWorld.inverse, K: intrinsics,
            imageSize: imageSize, integerPixelCenters: integerPixelCenters); stateLock.unlock(); requestDraw()
    }
    func setCameraPreset(_ preset: String) {
        beginNavigation(); stateLock.lock(); state.calibrated=nil
        switch preset.lowercased() {
        case "front": state.yaw=0; state.pitch=0
        case "back": state.yaw = .pi; state.pitch=0
        case "top": state.yaw=0; state.pitch = .pi/2-0.0001
        case "bottom": state.yaw=0; state.pitch = -.pi/2+0.0001
        case "right": state.yaw = .pi/2; state.pitch=0
        case "left": state.yaw = -.pi/2; state.pitch=0
        default: state.yaw = .pi/4; state.pitch=asin(1/sqrt(Float(3)))
        }
        stateLock.unlock(); endNavigation(); requestDraw()
    }
    /// Replacing immutable geometry is a selection/density operation, never a playback operation.
    func replaceTrajectory(_ geometry: TrajectoryGeometry?) {
        if let geometry {
            guard geometry.timeOrigin.isFinite, geometry.timeSpan.isFinite, geometry.timeSpan > 0,
                  geometry.chunks.allSatisfy({ chunk in chunk.segmentCount >= 0 && chunk.segmentCount <= Int(UInt32.max)/2 && chunk.buffer.length >= chunk.segmentCount*64 && chunk.buffer.device.registryID == device.registryID && chunk.hasCompleteTimeRanges && chunk.timeRanges.allSatisfy { range in
                      range.firstSegment >= 0 && range.firstSegment <= chunk.segmentCount && range.segmentCount >= 0 && range.segmentCount <= chunk.segmentCount-range.firstSegment && range.startTime.isFinite && range.endTime.isFinite && range.startTime <= range.endTime
                  } }) else {
                onError?("Invalid immutable trajectory geometry"); return
            }
        }
        stateLock.lock(); state.trajectoryGeometry = geometry;  stateLock.unlock(); requestDraw()
    }
    func setTrajectoryPlayback(time: Double, trailStartTime: Double, options: TrajectoryDisplayOptions) {
        guard time.isFinite, trailStartTime.isFinite, options.pathOpacity.isFinite else { return }
        var bounded = options; bounded.pathOpacity = min(1,max(0,options.pathOpacity))
        bounded.trailFrames = max(0, options.trailFrames); bounded.densityPercent = min(100,max(0,options.densityPercent))
        stateLock.lock(); state.trajectoryTime = time; state.trailStartTime = min(time,trailStartTime); state.trajectoryOptions = bounded; stateLock.unlock(); requestDraw()
    }
    func setCameraSegments(_ segments: [TrajectorySegmentGPU], pickSegments: [CameraPickSegmentGPU] = []) {
        guard segments.count <= device.maxBufferLength/64, pickSegments.count <= device.maxBufferLength/48,
              pickSegments.allSatisfy({ edge in edge.cameraID != .max && [edge.startPosition.x,edge.startPosition.y,edge.startPosition.z,edge.endPosition.x,edge.endPosition.y,edge.endPosition.z].allSatisfy(\.isFinite) }) else { onError?("Camera segments exceed device buffer capacity or use a reserved pick ID"); return }
        let buffer: MTLBuffer? = segments.isEmpty ? nil : segments.withUnsafeBytes { device.makeBuffer(bytes:$0.baseAddress!,length:$0.count,options:.storageModeShared) }
        if !segments.isEmpty && buffer == nil { onError?("Cannot allocate camera segments"); return }
        let pickBuffer: MTLBuffer? = pickSegments.isEmpty ? nil : pickSegments.withUnsafeBytes { device.makeBuffer(bytes:$0.baseAddress!,length:$0.count,options:.storageModeShared) }
        if !pickSegments.isEmpty && pickBuffer == nil { onError?("Cannot allocate camera picking segments"); return }
        stateLock.lock(); state.cameraSegments = buffer; state.cameraSegmentCount = segments.count; state.cameraHasTranslucentSegments = segments.contains { $0.startColor.w < 0.999 || $0.endColor.w < 0.999 }
        state.cameraPickSegments = pickBuffer; state.cameraPickSegmentCount = pickSegments.count
        stateLock.unlock(); requestDraw()
    }
    func setGraphLines(points: [SIMD4<Float>]) {
        guard points.count<=2_000_000 else{onError?("Graph overlay exceeds two million vertices.");return}
        let buffer:MTLBuffer?=points.isEmpty ? nil:points.withUnsafeBytes{device.makeBuffer(bytes:$0.baseAddress!,length:$0.count,options:.storageModeShared)}
        stateLock.lock();state.graphLines=buffer;state.graphLineCount=buffer==nil ? 0:points.count;stateLock.unlock();requestDraw()
    }
    /// Captures the exact current projection in half-pixel-centered coordinates.
    func cameraSnapshot() -> (pose:simd_float4x4,k:simd_float3x3,size:SIMD2<Float>) {
        stateLock.lock();let s=state;stateLock.unlock()
        if let calibrated=s.calibrated {
            var k=calibrated.K
            if calibrated.integerPixelCenters{k[2][0] += 0.5;k[2][1] += 0.5}
            return(calibrated.worldToCamera.inverse,k,calibrated.imageSize)
        }
        let size=view?.drawableSize ?? CGSize(width:1280,height:720)
        let width=max(1,Int(size.width)),height=max(1,Int(size.height)),u=uniforms(s,width:width,height:height)
        var k=matrix_identity_float3x3;k[0][0]=u.intrinsics.x;k[1][1]=u.intrinsics.y;k[2][0]=u.intrinsics.z;k[2][1]=u.intrinsics.w;k[1][0]=u.viewport.z
        return(u.worldToCamera.inverse,k,SIMD2(Float(width),Float(height)))
    }

    /// Coalesce state changes into AppKit's normal dirty-view drawing. Paused
    /// static scenes submit no recurring projection/sort/render work.
    func requestDraw() {
        stateLock.lock()
        if drawRequestQueued { stateLock.unlock(); return }
        drawRequestQueued = true; stateLock.unlock()
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.stateLock.lock(); self.drawRequestQueued = false; self.stateLock.unlock()
            self.view?.needsDisplay = true
        }
    }

    private func uniforms(_ s: RenderState, width: Int, height: Int) -> GaussianUniforms {
        let w = Float(width), h = Float(height)
        var camera: simd_float4x4, intrinsics: SIMD4<Float>, skew: Float = 0, rasterShift: Float = 0
        if let calibrated = s.calibrated {
            camera = calibrated.worldToCamera
            let scale = min(w / calibrated.imageSize.x, h / calibrated.imageSize.y)
            let offset = (SIMD2<Float>(w, h) - calibrated.imageSize * scale) * 0.5
            let half: Float = calibrated.integerPixelCenters ? 0.5 : 0
            rasterShift = half*scale
            intrinsics = SIMD4<Float>(calibrated.K[0,0] * scale, calibrated.K[1,1] * scale,
                                      (calibrated.K[2,0] + half) * scale + offset.x,
                                      (calibrated.K[2,1] + half) * scale + offset.y)
            skew = calibrated.K[1,0] * scale
        } else {
            let direction = NavigationGeometry.worldBasis(up:s.up) * SIMD3<Float>(sin(s.yaw) * cos(s.pitch), sin(s.pitch), cos(s.yaw) * cos(s.pitch))
            let origin = s.target + direction * s.distance
            let forward = simd_normalize(s.target - origin)
            let right = simd_normalize(simd_cross(forward, s.up))
            let down = simd_cross(forward, right)
            camera = simd_float4x4(columns: (SIMD4<Float>(right.x, down.x, forward.x, 0),
                SIMD4<Float>(right.y, down.y, forward.y, 0), SIMD4<Float>(right.z, down.z, forward.z, 0),
                SIMD4<Float>(-simd_dot(right, origin), -simd_dot(down, origin), -simd_dot(forward, origin), 1)))
            let focal = min(w, h) / (2 * tan(Float.pi / 7))
            intrinsics = SIMD4<Float>(focal, focal, w * 0.5, h * 0.5)
        }
        return GaussianUniforms(worldToCamera: camera, intrinsics: intrinsics,
            viewport: SIMD4<Float>(w, h, skew, s.interpolation),
            controls: SIMD4<Float>(s.settings.opacityScale, s.settings.gaussianScale, s.settings.exposure, s.settings.nearClip),
            counts: SIMD4<UInt32>(UInt32(s.count), s.selected, s.settings.showGrid ? 1 : 0, abs(s.up.x)>0.5 ? 0 : (abs(s.up.z)>0.5 ? 2 : 1)),
            depthPolicy: SIMD4(s.settings.farClip,s.settings.nearInclusive ? 1:0,s.settings.farInclusive ? 1:0,s.settings.divideEpsilon),
            projectionPolicy: SIMD4(s.settings.pixelOffset.x,s.settings.pixelOffset.y,s.settings.jacobianMode,s.settings.jacobianFactor),
            filterPolicy: SIMD4(s.settings.covarianceDilation,rasterShift,exp2(min(32,max(-32,s.settings.exposure))),0))
    }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) { requestDraw() }

    private func acquireFrame(for snapshot: RenderState) throws -> FrameResources {
        resourceLock.lock(); defer { resourceLock.unlock() }
        guard let index = (0..<resources.count).map({ (resourceIndex + $0) % resources.count }).first(where: { !resources[$0].leased }) else {
            throw RendererError.message("No completed GPU frame slot is available")
        }
        let frame = resources[index]
        try frame.allocate(device: device, count: max(snapshot.count, 1))
        frame.leased = true; resourceIndex = (index + 1) % resources.count
        frame.sourceA = snapshot.a; frame.sourceB = snapshot.b
        return frame
    }
    private func releaseFrame(_ frame: FrameResources) {
        resourceLock.lock()
        frame.finishLease(); resourceLock.unlock()
    }

    func draw(in view: MTKView) {
        guard inFlight.wait(timeout: .now()) == .success else {
            metricsLock.lock(); dropped += 1; metricsLock.unlock()
            stateLock.lock(); retryWhenAvailable = true; stateLock.unlock(); return
        }
        guard let drawable = view.currentDrawable else { inFlight.signal(); return }
        stateLock.lock(); let snapshot = state; state.pick = nil; stateLock.unlock()
        var leasedFrame: FrameResources?
        do {
            let frame = try acquireFrame(for: snapshot); leasedFrame = frame
            let submitted = try encode(snapshot, frame, texture: drawable.texture, drawable: drawable)
            submitted.render.addCompletedHandler { [weak self, snapshot, frame] command in
                guard let self else { return }
                defer {
                    self.releaseFrame(frame)
                    self.inFlight.signal()
                    self.stateLock.lock(); let retry = self.retryWhenAvailable; self.retryWhenAvailable = false; self.stateLock.unlock()
                    if retry { self.requestDraw() }
                }
                if let failed = [submitted.projection,submitted.sort,command].first(where: { $0.status == .error }) {
                    let message = failed.error?.localizedDescription ?? "Metal command failed"
                    DispatchQueue.main.async {
                        self.stateLock.lock(); let current = self.state.sceneRevision; self.stateLock.unlock()
                        guard current == snapshot.sceneRevision else { return }
                        self.onError?(message)
                    }
                    return
                }
                // Retain source buffers until every GPU read has finished.
                _ = snapshot.a; _ = snapshot.b
                if snapshot.pick != nil {
                    // Copy both result kinds before the in-flight frame slot is
                    // released. Main-queue delivery may outlive its GPU buffer.
                    let sceneResult = self.scenePickResult(frame.pick, gaussianCount: snapshot.count)
                    DispatchQueue.main.async {
                        // A composite index is valid only for the request and
                        // scene which produced it. Never select in a replacement
                        // document from a late GPU completion.
                        self.stateLock.lock(); let current=self.state.pickRevision; self.stateLock.unlock()
                        guard snapshot.pickRevision==current else{return}
                        self.onScenePick?(sceneResult)
                    }
                }
                self.publishMetrics(snapshot, frame, submitted)
            }
            submitted.render.commit()
        } catch {
            if let frame = leasedFrame { releaseFrame(frame) }
            inFlight.signal()
            stateLock.lock(); let current = state.sceneRevision; stateLock.unlock()
            if current == snapshot.sceneRevision { onError?(error.localizedDescription) }
        }
    }

    /// Encoder exhaustion is a recoverable error, not a force-unwrap crash.
    /// Each caller keeps its existing endEncoding / command commit order.
    private func computeEncoder(_ command: MTLCommandBuffer) throws -> MTLComputeCommandEncoder {
        guard let encoder = command.makeComputeCommandEncoder() else {
            throw RendererError.message("Cannot allocate Metal compute encoder")
        }
        return encoder
    }
    private func blitEncoder(_ command: MTLCommandBuffer) throws -> MTLBlitCommandEncoder {
        guard let encoder = command.makeBlitCommandEncoder() else {
            throw RendererError.message("Cannot allocate Metal blit encoder")
        }
        return encoder
    }

    private struct Submission {
        var projection: MTLCommandBuffer
        var sort: MTLCommandBuffer
        var render: MTLCommandBuffer
    }
    private func encode(_ s: RenderState, _ r: FrameResources, texture: MTLTexture,
                        drawable: CAMetalDrawable?) throws -> Submission {
        guard let projection = commandQueue.makeCommandBuffer(), let sort = commandQueue.makeCommandBuffer(),
              let rendering = commandQueue.makeCommandBuffer() else { throw RendererError.message("Metal command buffer allocation failed") }
        projection.label = "Gaussian projection and culling"; sort.label = "Eight-pass stable radix depth sort"; rendering.label = "Gaussian blend and pick"
        var u = uniforms(s, width: texture.width, height: texture.height)
        let count = s.count, groups = (count + 255) / 256
        let blit = try blitEncoder(projection)
        blit.fill(buffer: r.visible, range: 0..<16, value: 0)
        if s.pick != nil { blit.fill(buffer: r.pick, range: 0..<16, value: 255) }
        blit.endEncoding()
        if count > 0, let a = s.a, let b = s.b {
            let encoder = try computeEncoder(projection); encoder.setComputePipelineState(projectPipeline)
            [a,b,r.projected!,r.indicesA!,r.visible!].enumerated().forEach { encoder.setBuffer($0.element, offset: 0, index: $0.offset) }
            encoder.setBytes(&u, length: MemoryLayout<GaussianUniforms>.stride, index: 5)
            encoder.setBuffer(s.selectedMask ?? zeroSelectionMask, offset: 0, index: 6)
            var hasSelectionMask: UInt32 = s.selectedMask == nil ? 0 : 1
            encoder.setBytes(&hasSelectionMask, length: 4, index: 7)
            encoder.dispatchThreads(MTLSize(width: count, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 256, height: 1, depth: 1)); encoder.endEncoding()
            for pass in 0..<8 {
                var args = SIMD4<UInt32>(UInt32(count), UInt32(pass * 4), UInt32(groups), 0)
                let source = pass % 2 == 0 ? r.indicesA! : r.indicesB!
                let destination = pass % 2 == 0 ? r.indicesB! : r.indicesA!
                let histogram = try computeEncoder(sort); histogram.setComputePipelineState(histogramPipeline)
                [r.projected!,source,r.histogram!].enumerated().forEach { histogram.setBuffer($0.element, offset: 0, index: $0.offset) }
                histogram.setBytes(&args, length: 16, index: 3)
                histogram.dispatchThreadgroups(MTLSize(width: groups, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 256, height: 1, depth: 1)); histogram.endEncoding()
                let prefix = try computeEncoder(sort); prefix.setComputePipelineState(prefixPipeline)
                [r.histogram!,r.prefixes!,r.totals!].enumerated().forEach { prefix.setBuffer($0.element, offset: 0, index: $0.offset) }
                prefix.setBytes(&args, length: 16, index: 3)
                prefix.dispatchThreads(MTLSize(width: 16, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 16, height: 1, depth: 1)); prefix.endEncoding()
                let scatter = try computeEncoder(sort); scatter.setComputePipelineState(scatterPipeline)
                [r.projected!,source,destination,r.prefixes!,r.totals!].enumerated().forEach { scatter.setBuffer($0.element, offset: 0, index: $0.offset) }
                scatter.setBytes(&args, length: 16, index: 5)
                scatter.dispatchThreadgroups(MTLSize(width: groups, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 256, height: 1, depth: 1)); scatter.endEncoding()
            }

        }
        let argsEncoder = try computeEncoder(rendering); argsEncoder.setComputePipelineState(argumentsPipeline)
        argsEncoder.setBuffer(r.visible, offset: 0, index: 0); argsEncoder.setBuffer(r.indirect, offset: 0, index: 1)
        argsEncoder.dispatchThreads(MTLSize(width: 1, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 1, height: 1, depth: 1)); argsEncoder.endEncoding()
        if let pixel = s.pick, count > 0 {
            var args = SIMD4<UInt32>(UInt32(count),pixel.x,pixel.y,0)
            for pipeline in [pickDepthPipeline, pickIndexPipeline] {
                let e = try computeEncoder(rendering); e.setComputePipelineState(pipeline)
                e.setBuffer(r.projected, offset: 0, index: 0); e.setBuffer(r.pick, offset: 0, index: 1); e.setBytes(&args, length: 16, index: 2)
                e.dispatchThreads(MTLSize(width: count, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 256, height: 1, depth: 1)); e.endEncoding()
            }
        }
        let pass = MTLRenderPassDescriptor(); pass.colorAttachments[0].texture = texture
        pass.colorAttachments[0].loadAction = .clear; pass.colorAttachments[0].storeAction = .store
        let bg = s.settings.background; pass.colorAttachments[0].clearColor = MTLClearColor(red: Double(bg.x), green: Double(bg.y), blue: Double(bg.z), alpha: Double(bg.w))
        guard let renderer = rendering.makeRenderCommandEncoder(descriptor: pass) else { throw RendererError.message("Cannot allocate Metal scene render encoder") }
        if s.settings.showGrid { renderer.setRenderPipelineState(gridPipeline); renderer.setVertexBytes(&u, length: MemoryLayout<GaussianUniforms>.stride, index: 0); renderer.drawPrimitives(type: .line, vertexStart: 0, vertexCount: 90) }
        if count > 0 {
            renderer.setRenderPipelineState(texture.pixelFormat == .rgba32Float ? comparisonPipeline : gaussianPipeline); renderer.setCullMode(.none)
            renderer.setVertexBuffer(r.projected, offset: 0, index: 0); renderer.setVertexBuffer(r.indicesA, offset: 0, index: 1)
            renderer.setVertexBytes(&u, length: MemoryLayout<GaussianUniforms>.stride, index: 2)
            renderer.drawPrimitives(type: .triangle, indirectBuffer: r.indirect, indirectBufferOffset: 0)
        }
        if let lines=s.graphLines,s.graphLineCount>=2 {
            renderer.setRenderPipelineState(scaffoldPipeline);renderer.setVertexBuffer(lines,offset:0,index:0);renderer.setVertexBytes(&u, length: MemoryLayout<GaussianUniforms>.stride,index:1)
            renderer.drawPrimitives(type:.line,vertexStart:0,vertexCount:(s.graphLineCount-1)*2)
        }
        if s.settings.showOrbitCenter {
            var pivot=SIMD4<Float>(s.target,1)
            renderer.setRenderPipelineState(pivotPipeline)
            renderer.setVertexBytes(&u, length: MemoryLayout<GaussianUniforms>.stride,index:0)
            renderer.setVertexBytes(&pivot,length:16,index:1)
            renderer.drawPrimitives(type:.line,vertexStart:0,vertexCount:4)
        }
        renderer.endEncoding()
        if texture.pixelFormat == .bgra8Unorm { try encodePreviewOverlays(s, r, uniforms: u, texture: texture, command: rendering) }
        if let drawable { rendering.present(drawable) }
        projection.commit(); sort.commit()
        return Submission(projection: projection, sort: sort, render: rendering)
    }
    /// Display overlays use a separate scene-surface depth pass. Transparent
    /// full paths accumulate with weighted blended OIT, so extraction/chunk
    /// ordering never controls their color. Opaque tails write actual line depth.
    /// Gaussian splats are volumes without a unique surface; the display depth
    /// uses alpha>=0.1, while scientific captures retain the untouched compositor.
    private func encodePreviewOverlays(_ s: RenderState, _ r: FrameResources, uniforms u: GaussianUniforms,
                                       texture: MTLTexture, command: MTLCommandBuffer) throws {
        let geometry = s.trajectoryGeometry
        let cameraPaths = s.cameraHasTranslucentSegments && s.cameraSegmentCount > 0
        let paths = geometry != nil && s.trajectoryOptions.showPaths && s.trajectoryOptions.pathOpacity > 0
        let opaquePaths = paths && s.trajectoryOptions.pathOpacity >= 0.999
        let transparentPaths = paths && !opaquePaths
        let trails = geometry != nil && s.trajectoryOptions.showTrails && s.trajectoryOptions.trailFrames > 0 && s.trajectoryTime > s.trailStartTime
        let markers = !s.selectedIndices.isEmpty && s.selectionBuffer != nil
        let pickCameras = s.pick != nil && s.scenePickTolerance != nil && s.cameraPickSegmentCount > 0
        guard paths || trails || markers || s.cameraSegmentCount > 0 || pickCameras else {
            resourceLock.lock(); r.overlayDepth = nil; r.overlayAccumulation = nil; r.overlayRevealage = nil; resourceLock.unlock()
            return
        }
        resourceLock.lock()
        do { try r.allocateOverlays(device: device, width: texture.width, height: texture.height, needsTransparency: transparentPaths || cameraPaths) }
        catch { resourceLock.unlock(); throw error }
        resourceLock.unlock()
        var uniforms = u
        let depthPass = MTLRenderPassDescriptor()
        depthPass.depthAttachment.texture = r.overlayDepth; depthPass.depthAttachment.loadAction = .clear
        depthPass.depthAttachment.storeAction = .store; depthPass.depthAttachment.clearDepth = 1
        guard let depth = command.makeRenderCommandEncoder(descriptor: depthPass) else { throw RendererError.message("Cannot encode preview occlusion depth") }
        depth.label = "Gaussian display surface depth"
        depth.setRenderPipelineState(gaussianDepthPipeline); depth.setDepthStencilState(overlayDepthWrite); depth.setCullMode(.none)
        if s.count > 0 {
            depth.setVertexBuffer(r.projected, offset: 0, index: 0); depth.setVertexBuffer(r.indicesA, offset: 0, index: 1)
            depth.setVertexBytes(&uniforms, length: MemoryLayout<GaussianUniforms>.stride, index: 2)
            depth.drawPrimitives(type: .triangle, indirectBuffer: r.indirect, indirectBufferOffset: 0)
        }
        depth.endEncoding()
        if pickCameras, let cameraPickSegments = s.cameraPickSegments, let pixel = s.pick,
           let tolerance = s.scenePickTolerance, let depthTexture = r.overlayDepth {
            var pickArgs = SIMD4<Float>(Float(pixel.x) + 0.5, Float(pixel.y) + 0.5, tolerance, 0)
            var segmentCount = UInt32(s.cameraPickSegmentCount)
            for pipeline in [cameraPickDepthPipeline, cameraPickIndexPipeline] {
                guard let pick = command.makeComputeCommandEncoder() else { throw RendererError.message("Cannot encode camera wireframe picking") }
                pick.setComputePipelineState(pipeline)
                pick.setBuffer(cameraPickSegments, offset: 0, index: 0)
                pick.setBuffer(r.pick, offset: 0, index: 1)
                pick.setBytes(&uniforms, length: MemoryLayout<GaussianUniforms>.stride, index: 2)
                pick.setBytes(&pickArgs, length: 16, index: 3)
                pick.setBytes(&segmentCount, length: 4, index: 4)
                pick.setTexture(depthTexture, index: 0)
                pick.dispatchThreads(MTLSize(width: s.cameraPickSegmentCount, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 256, height: 1, depth: 1))
                pick.endEncoding()
            }
        }
        let opaquePass = MTLRenderPassDescriptor()
        opaquePass.colorAttachments[0].texture = texture; opaquePass.colorAttachments[0].loadAction = .load; opaquePass.colorAttachments[0].storeAction = .store
        opaquePass.depthAttachment.texture = r.overlayDepth; opaquePass.depthAttachment.loadAction = .load; opaquePass.depthAttachment.storeAction = .store
        guard let opaque = command.makeRenderCommandEncoder(descriptor: opaquePass) else { throw RendererError.message("Cannot encode opaque preview lines") }
        opaque.label = "Depth-tested tails, camera wireframes and selected elements"
        opaque.setDepthStencilState(overlayDepthWrite); opaque.setCullMode(.none)
        var playback = SIMD4<Float>(geometry?.normalizedTime(s.trajectoryTime) ?? 0, geometry?.normalizedTime(s.trailStartTime) ?? 0, s.trajectoryOptions.pathOpacity, 1)
        if trails, let geometry {
            opaque.setRenderPipelineState(trailPipeline)
            opaque.setVertexBytes(&uniforms, length: MemoryLayout<GaussianUniforms>.stride, index: 1)
            opaque.setVertexBytes(&playback, length: 16, index: 2)
            for chunk in geometry.chunks where chunk.segmentCount > 0 {
                opaque.setVertexBuffer(chunk.buffer, offset: 0, index: 0)
                if chunk.timeRanges.isEmpty {
                    opaque.drawPrimitives(type:.line,vertexStart:0,vertexCount:chunk.segmentCount*2)
                } else {
                    // Native frame-pair spans are immutable extraction metadata.
                    // Only draw time-intersecting spans; no identity is removed.
                    for range in chunk.timeRanges where range.endTime >= playback.y && range.startTime <= playback.x {
                        opaque.drawPrimitives(type:.line,vertexStart:range.firstSegment*2,vertexCount:range.segmentCount*2)
                    }
                }
            }
        }
        if opaquePaths, let geometry {
            opaque.setRenderPipelineState(trailPipeline); playback.w = 0; playback.z = 1
            opaque.setVertexBytes(&uniforms,length:MemoryLayout<GaussianUniforms>.stride,index:1)
            opaque.setVertexBytes(&playback,length:16,index:2)
            for chunk in geometry.chunks where chunk.segmentCount > 0 {
                opaque.setVertexBuffer(chunk.buffer,offset:0,index:0)
                opaque.drawPrimitives(type:.line,vertexStart:0,vertexCount:chunk.segmentCount*2)
            }
        }
        if let cameras = s.cameraSegments, s.cameraSegmentCount > 0 {
            opaque.setRenderPipelineState(coloredLinePipeline); opaque.setVertexBuffer(cameras, offset: 0, index: 0)
            opaque.setVertexBytes(&uniforms, length: MemoryLayout<GaussianUniforms>.stride, index: 1)
            opaque.drawPrimitives(type: .line, vertexStart: 0, vertexCount: s.cameraSegmentCount*2)
        }
        opaque.endEncoding()
        func drawSelectedMarkers() throws {
            guard markers, let selected = s.selectionBuffer else { return }
            let markerPass = MTLRenderPassDescriptor()
            markerPass.colorAttachments[0].texture = texture; markerPass.colorAttachments[0].loadAction = .load; markerPass.colorAttachments[0].storeAction = .store
            markerPass.depthAttachment.texture = r.overlayDepth; markerPass.depthAttachment.loadAction = .load; markerPass.depthAttachment.storeAction = .store
            guard let markerEncoder = command.makeRenderCommandEncoder(descriptor: markerPass) else { throw RendererError.message("Cannot encode selected elements") }
            markerEncoder.setDepthStencilState(overlayDepthRead); markerEncoder.setRenderPipelineState(selectionPipeline)
            markerEncoder.setVertexBuffer(r.projected, offset:0,index:0); markerEncoder.setVertexBuffer(selected,offset:0,index:1)
            markerEncoder.setVertexBytes(&uniforms,length:MemoryLayout<GaussianUniforms>.stride,index:2)
            markerEncoder.drawPrimitives(type:.triangle,vertexStart:0,vertexCount:6,instanceCount:s.selectedIndices.count)
            markerEncoder.endEncoding()
        }
        guard transparentPaths || cameraPaths else { try drawSelectedMarkers(); return }
        let translucentPass = MTLRenderPassDescriptor()
        translucentPass.colorAttachments[0].texture = r.overlayAccumulation; translucentPass.colorAttachments[0].loadAction = .clear; translucentPass.colorAttachments[0].storeAction = .store
        translucentPass.colorAttachments[0].clearColor = MTLClearColor(red:0,green:0,blue:0,alpha:0)
        translucentPass.colorAttachments[1].texture = r.overlayRevealage; translucentPass.colorAttachments[1].loadAction = .clear; translucentPass.colorAttachments[1].storeAction = .store
        translucentPass.colorAttachments[1].clearColor = MTLClearColor(red:1,green:1,blue:1,alpha:1)
        translucentPass.depthAttachment.texture = r.overlayDepth; translucentPass.depthAttachment.loadAction = .load; translucentPass.depthAttachment.storeAction = .store
        guard let translucent = command.makeRenderCommandEncoder(descriptor: translucentPass) else { throw RendererError.message("Cannot encode full paths") }
        translucent.label = "Order-independent translucent full paths"
        translucent.setRenderPipelineState(pathAccumulationPipeline); translucent.setDepthStencilState(overlayDepthRead)
        translucent.setVertexBytes(&uniforms, length: MemoryLayout<GaussianUniforms>.stride, index: 1)
        playback.w = 0; translucent.setVertexBytes(&playback, length: 16, index: 2)
        for chunk in geometry?.chunks ?? [] where transparentPaths && chunk.segmentCount > 0 {
            translucent.setVertexBuffer(chunk.buffer, offset: 0, index: 0)
            translucent.drawPrimitives(type: .line, vertexStart: 0, vertexCount: chunk.segmentCount*2)
        }
        if let cameras = s.cameraSegments, s.cameraSegmentCount > 0 {
            translucent.setRenderPipelineState(cameraAccumulationPipeline); translucent.setVertexBuffer(cameras, offset:0,index:0)
            translucent.setVertexBytes(&uniforms,length:MemoryLayout<GaussianUniforms>.stride,index:1)
            translucent.drawPrimitives(type:.line,vertexStart:0,vertexCount:s.cameraSegmentCount*2)
        }
        translucent.endEncoding()
        let compositePass = MTLRenderPassDescriptor()
        compositePass.colorAttachments[0].texture = texture; compositePass.colorAttachments[0].loadAction = .load; compositePass.colorAttachments[0].storeAction = .store
        guard let composite = command.makeRenderCommandEncoder(descriptor: compositePass) else { throw RendererError.message("Cannot composite paths") }
        composite.label = "Composite full-path transmittance"
        composite.setRenderPipelineState(pathCompositePipeline)
        composite.setFragmentTexture(r.overlayAccumulation, index: 0); composite.setFragmentTexture(r.overlayRevealage, index: 1)
        composite.drawPrimitives(type:.triangle, vertexStart:0, vertexCount:3); composite.endEncoding()
        try drawSelectedMarkers()
    }
    private func milliseconds(_ command: MTLCommandBuffer) -> Double { max(0, command.gpuEndTime - command.gpuStartTime) * 1000 }
    private func publishMetrics(_ s: RenderState, _ r: FrameResources, _ submission: Submission) {
        metricsLock.lock(); let now = CACurrentMediaTime(); totalCompletedFrames += 1
        if let last = completionTimes.last, now-last > 0.5 { completionTimes.removeAll(keepingCapacity: true) }
        completionTimes.append(now); completionTimes.removeAll(where: { now-$0 > 1 })
        let fps = completionTimes.count > 1 ? Double(completionTimes.count-1)/max(now-completionTimes[0],1e-6) : 0
        let droppedCount = dropped, totalFrames = totalCompletedFrames; dropped = 0; metricsLock.unlock()
        let projection = milliseconds(submission.projection), sort = milliseconds(submission.sort), render = milliseconds(submission.render)
        resourceLock.lock()
        var sourceBuffers = [ObjectIdentifier:MTLBuffer]()
        for buffer in ([s.a,s.b,s.graphLines,s.selectedMask,s.selectionBuffer,s.cameraSegments,s.cameraPickSegments] + (s.trajectoryGeometry?.chunks.map { Optional($0.buffer) } ?? []) + resources.flatMap { [$0.sourceA,$0.sourceB] }).compactMap({$0}) {
            sourceBuffers[ObjectIdentifier(buffer)] = buffer
        }
        let bufferBytes = resources.reduce(sourceBuffers.values.reduce(0) { $0+$1.length }) { $0+$1.bytes }
        resourceLock.unlock()
        let metrics = GaussianRenderMetrics(fps: fps, gpuMilliseconds: projection + sort + render,
            visibleCount: Int(r.visible.contents().assumingMemoryBound(to: UInt32.self).pointee), totalCount: s.count,
            residentBytes: bufferBytes, sortMilliseconds: sort,
            projectionMilliseconds: projection, renderMilliseconds: render, droppedSubmissions: droppedCount, completedFrameCount: totalFrames)
        DispatchQueue.main.async { [self] in
            self.stateLock.lock(); let current = self.state.sceneRevision; self.stateLock.unlock()
            guard current == s.sceneRevision else { return }
            self.idleMetricsWork?.cancel(); self.onMetrics?(metrics)
            let work = DispatchWorkItem { [weak self] in
                guard let self else { return }
                self.stateLock.lock(); let current = self.state.sceneRevision; self.stateLock.unlock()
                guard current == s.sceneRevision else { return }
                var idle = metrics; idle.fps = 0; idle.droppedSubmissions = 0; self.onMetrics?(idle)
            }
            self.idleMetricsWork = work
            DispatchQueue.main.asyncAfter(deadline: .now()+0.5, execute: work)
        }
    }

    /// Renders the current camera directly to an offscreen Metal texture. No
    /// screen-capture permission, window visibility or CPU N-record scan needed.
    func captureImage(completion: @escaping (Result<CGImage, Error>) -> Void) {
        stateLock.lock(); let snapshot = state; stateLock.unlock()
        let size = view?.drawableSize ?? CGSize(width: 1280, height: 720)
        let width = max(1, Int(size.width)), height = max(1, Int(size.height))
        DispatchQueue.global(qos: .userInitiated).async { [self, snapshot] in
            do {
                let textureDescriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false)
                textureDescriptor.usage = [.renderTarget,.shaderRead]; textureDescriptor.storageMode = .private
                guard let texture = device.makeTexture(descriptor: textureDescriptor) else { throw RendererError.message("Snapshot texture allocation failed") }
                let r = FrameResources(); try r.allocate(device: device, count: max(1,snapshot.count))
                let submission = try encode(snapshot,r,texture:texture,drawable:nil)
                let rowBytes = ((width * 4 + 255) / 256) * 256
                guard let readback = device.makeBuffer(length: rowBytes * height, options: .storageModeShared) else { throw RendererError.message("Snapshot readback allocation failed") }
                let copy = try blitEncoder(submission.render)
                copy.copy(from:texture,sourceSlice:0,sourceLevel:0,sourceOrigin:MTLOrigin(x:0,y:0,z:0),sourceSize:MTLSize(width:width,height:height,depth:1),to:readback,destinationOffset:0,destinationBytesPerRow:rowBytes,destinationBytesPerImage:rowBytes*height)
                copy.endEncoding(); submission.render.commit(); submission.render.waitUntilCompleted()
                for command in [submission.projection,submission.sort,submission.render] where command.status == .error { throw RendererError.message(command.error?.localizedDescription ?? "Snapshot Metal command failed") }
                let data = Data(bytes:readback.contents(),count:rowBytes*height)
                guard let provider = CGDataProvider(data:data as CFData), let image = CGImage(width:width,height:height,bitsPerComponent:8,bitsPerPixel:32,bytesPerRow:rowBytes,
                    space:CGColorSpaceCreateDeviceRGB(),bitmapInfo:CGBitmapInfo(rawValue:CGImageAlphaInfo.premultipliedFirst.rawValue).union(.byteOrder32Little),provider:provider,decode:nil,shouldInterpolate:false,intent:.defaultIntent) else {
                    throw RendererError.message("Snapshot image construction failed")
                }
                DispatchQueue.main.async { completion(.success(image)) }
            } catch { DispatchQueue.main.async { completion(.failure(error)) } }
        }
    }

    /// Independent, immutable camera capture. The callback runs on a worker;
    /// no waitUntilCompleted, AppKit readback, overlays or display scaling.
    /// Caller coalesces requests and allows one comparison capture in flight.
    func captureComparison(a: MTLBuffer, b: MTLBuffer, count: Int, interpolation: Float,
                           pose: simd_float4x4, intrinsics: simd_float3x3,
                           width: Int, height: Int, integerPixelCenters: Bool,
                           background: SIMD3<Float>, nearClip: Float = 0.001, farClip: Float? = nil, nearInclusive: Bool = false, farInclusive: Bool = true,
                           pixelOffset: SIMD2<Float> = .zero, divideEpsilon: Float = 0, jacobianMode: String = "image-bounds", jacobianFactor: Float = 1.3, covarianceDilation: Float = 0.3, clampOutput: Bool = true, colorGain: SIMD3<Float> = SIMD3(repeating:1), colorBias: SIMD3<Float> = .zero, cancellation: ComparisonCancellation,
                           completion: @escaping (Result<[Float], Error>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            do {
                guard !cancellation.cancelled else { throw CancellationError() }
                guard width > 0, height > 0, width <= 16384, height <= 16384,
                      width*height <= 16_777_216, nearClip.isFinite, nearClip >= 0,
                      farClip == nil || (farClip!.isFinite && farClip! >= 0),
                      pixelOffset.x.isFinite && pixelOffset.y.isFinite,divideEpsilon.isFinite,divideEpsilon >= 0,jacobianFactor.isFinite,jacobianFactor > 0,covarianceDilation.isFinite,covarianceDilation >= 0,
                      count >= 0, count <= device.maxBufferLength/64,
                      a.device.registryID == device.registryID, b.device.registryID == device.registryID,
                      (0..<3).allSatisfy({colorGain[$0].isFinite && colorGain[$0] >= 0 && colorBias[$0].isFinite}),
                      a.length >= count*64, b.length >= count*64 else {
                    throw RendererError.message("Comparison capture exceeds the bounded image or geometry capacity")
                }
                var snapshot = RenderState()
                snapshot.a = a; snapshot.b = b; snapshot.count = count; snapshot.interpolation = interpolation
                snapshot.settings.showGrid = false; snapshot.settings.showOrbitCenter = false
                snapshot.settings.background = SIMD4(background,1); snapshot.settings.nearClip = nearClip
                snapshot.settings.farClip = farClip ?? .infinity;snapshot.settings.nearInclusive = nearInclusive;snapshot.settings.farInclusive = farInclusive
                snapshot.settings.pixelOffset = pixelOffset;snapshot.settings.divideEpsilon = divideEpsilon
                snapshot.settings.jacobianMode = jacobianMode == "symmetric-fov" ? 1 : (jacobianMode == "unclamped" ? 2:3)
                snapshot.settings.jacobianFactor = jacobianFactor;snapshot.settings.covarianceDilation = covarianceDilation
                snapshot.calibrated = CalibratedCamera(worldToCamera:pose.inverse,K:intrinsics,imageSize:SIMD2(Float(width),Float(height)),integerPixelCenters:integerPixelCenters)
                let descriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat:.rgba32Float,width:width,height:height,mipmapped:false)
                descriptor.usage = [.renderTarget]; descriptor.storageMode = .private
                guard let texture = device.makeTexture(descriptor:descriptor) else { throw RendererError.message("Cannot allocate full-resolution comparison texture") }
                let resources = FrameResources(); try resources.allocate(device:device,count:max(1,count))
                let rowBytes = ((width*16+255)/256)*256
                guard let pixels = device.makeBuffer(length:rowBytes*height,options:.storageModeShared) else { throw RendererError.message("Cannot allocate full-resolution comparison readback") }
                guard !cancellation.cancelled else { throw CancellationError() }
                let submission = try encode(snapshot,resources,texture:texture,drawable:nil)
                guard let blit = submission.render.makeBlitCommandEncoder() else { throw RendererError.message("Comparison copy encoder failed") }
                blit.copy(from:texture,sourceSlice:0,sourceLevel:0,sourceOrigin:MTLOrigin(x:0,y:0,z:0),sourceSize:MTLSize(width:width,height:height,depth:1),to:pixels,destinationOffset:0,destinationBytesPerRow:rowBytes,destinationBytesPerImage:rowBytes*height)
                blit.endEncoding()
                submission.render.addCompletedHandler { [snapshot,resources,texture,pixels] command in
                    DispatchQueue.global(qos:.userInitiated).async {
                        _ = snapshot; _ = resources; _ = texture
                        do {
                            guard !cancellation.cancelled else { throw CancellationError() }
                            for stage in [submission.projection,submission.sort,command] where stage.status == .error {
                                throw RendererError.message(stage.error?.localizedDescription ?? "Comparison GPU command failed")
                            }
                            var rgb = [Float](repeating:0,count:width*height*3)
                            for y in 0..<height {
                                if cancellation.cancelled { throw CancellationError() }
                                let row = pixels.contents().advanced(by:y*rowBytes).assumingMemoryBound(to:Float.self)
                                for x in 0..<width { for c in 0..<3 {
                                    let value = row[x*4+c]
                                    guard value.isFinite else { throw RendererError.message("Nonfinite comparison render") }
                                    // Source camera radiometry acts on the completed RGB, before clipping.
                                    let corrected = value*colorGain[c]+colorBias[c]
                                    guard corrected.isFinite else {throw RendererError.message("Camera radiometry overflowed the Float32 render domain")}
                                    rgb[(y*width+x)*3+c] = clampOutput ? min(1,max(0,corrected)) : corrected
                                } }
                            }
                            completion(.success(rgb))
                        } catch { completion(.failure(error)) }
                    }
                }
                submission.render.commit()
            } catch { completion(.failure(error)) }
        }
    }

#if STUDIO_TESTING
    /// Small, synchronous GPU readback for independent geometry/pixel QA.
    /// Uses exactly the production kernels and compositing path; never used by
    /// interactive draw. Returned bytes are BGRA8 with top-left image origin.
    func debugRender(a: [GaussianGPURecord], b: [GaussianGPURecord]? = nil, interpolation: Float = 0,
                     worldToCamera: simd_float4x4 = matrix_identity_float4x4, intrinsics: simd_float3x3,
                     width: Int, height: Int, integerPixelCenters: Bool = true,
                     background: SIMD4<Float> = .zero, pick: SIMD2<UInt32>? = nil,
                     nearClip: Float = 0.001, exposure: Float = 0, trajectory: TrajectoryGeometry? = nil,
                     trajectoryTime: Double = 0, trailStartTime: Double = 0,
                     trajectoryOptions: TrajectoryDisplayOptions = TrajectoryDisplayOptions(),
                     selectedIndices: [UInt32] = [], cameraSegments: [TrajectorySegmentGPU] = [],
                     cameraPickSegments: [CameraPickSegmentGPU] = [], scenePickTolerance: Float? = nil,
                     farClip: Float = .infinity, nearInclusive: Bool = false, farInclusive: Bool = true,
                     pixelOffset: SIMD2<Float> = .zero, divideEpsilon: Float = 0) throws -> GaussianDebugFrame {
        let second = b ?? a
        guard a.count <= 32768, second.count == a.count, (1...16384).contains(width), (1...16384).contains(height),
              exposure.isFinite, nearClip.isFinite, nearClip > 0, farClip > nearClip, divideEpsilon.isFinite, divideEpsilon >= 0,
              (scenePickTolerance == nil || (scenePickTolerance!.isFinite && scenePickTolerance! >= 0)),
              pixelOffset.x.isFinite, pixelOffset.y.isFinite else {
            throw RendererError.message("Invalid offscreen test dimensions, camera, or Gaussian endpoints")
        }
        let bytes = a.count * 64
        let firstBuffer = a.isEmpty ? nil : a.withUnsafeBytes { device.makeBuffer(bytes: $0.baseAddress!, length: bytes, options: .storageModeShared) }
        let secondBuffer = second.isEmpty ? nil : second.withUnsafeBytes { device.makeBuffer(bytes: $0.baseAddress!, length: bytes, options: .storageModeShared) }
        var s = RenderState(); s.a = firstBuffer; s.b = secondBuffer; s.count = a.count; s.interpolation = interpolation
        s.settings.showGrid = false; s.settings.background = background; s.settings.nearClip = nearClip; s.pick = pick
        s.scenePickTolerance = scenePickTolerance; s.settings.farClip = farClip; s.settings.exposure = exposure
        s.settings.nearInclusive = nearInclusive; s.settings.farInclusive = farInclusive
        s.settings.pixelOffset = pixelOffset; s.settings.divideEpsilon = divideEpsilon
        s.trajectoryGeometry = trajectory; s.trajectoryTime = trajectoryTime; s.trailStartTime = trailStartTime; s.trajectoryOptions = trajectoryOptions
        s.selectedIndices = selectedIndices; s.selectedMask = makeSelectionMask(count: a.count, indices: selectedIndices)
        s.selectionBuffer = selectedIndices.isEmpty ? nil : selectedIndices.withUnsafeBytes {device.makeBuffer(bytes:$0.baseAddress!,length:$0.count,options:.storageModeShared)}
        s.cameraSegments = cameraSegments.isEmpty ? nil : cameraSegments.withUnsafeBytes {device.makeBuffer(bytes:$0.baseAddress!,length:$0.count,options:.storageModeShared)}; s.cameraSegmentCount = cameraSegments.count; s.cameraHasTranslucentSegments = cameraSegments.contains { $0.startColor.w < 0.999 || $0.endColor.w < 0.999 }
        s.cameraPickSegments = cameraPickSegments.isEmpty ? nil : cameraPickSegments.withUnsafeBytes { device.makeBuffer(bytes:$0.baseAddress!,length:$0.count,options:.storageModeShared) }
        s.cameraPickSegmentCount = cameraPickSegments.count
        s.calibrated = CalibratedCamera(worldToCamera: worldToCamera, K: intrinsics, imageSize: SIMD2<Float>(Float(width),Float(height)), integerPixelCenters: integerPixelCenters)
        let r = FrameResources(); try r.allocate(device: device, count: max(1,a.count))
        let descriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false)
        descriptor.usage = [.renderTarget,.shaderRead]; descriptor.storageMode = .private
        guard let texture = device.makeTexture(descriptor: descriptor) else { throw RendererError.message("QA texture allocation failed") }
        let rowBytes = ((width * 4 + 255) / 256) * 256
        guard let pixels = device.makeBuffer(length: rowBytes * height, options: .storageModeShared),
              let projected = device.makeBuffer(length: max(16, bytes), options: .storageModeShared),
              let indices = device.makeBuffer(length: max(16, a.count * 8), options: .storageModeShared) else {
            throw RendererError.message("Cannot allocate test readback buffers")
        }
        let submission = try encode(s,r,texture:texture,drawable:nil)
        let copy = try blitEncoder(submission.render)
        copy.copy(from: texture, sourceSlice: 0, sourceLevel: 0, sourceOrigin: MTLOrigin(x:0,y:0,z:0),
                  sourceSize: MTLSize(width:width,height:height,depth:1), to: pixels, destinationOffset: 0,
                  destinationBytesPerRow: rowBytes, destinationBytesPerImage: rowBytes * height)
        if bytes > 0 {
            copy.copy(from:r.projected,sourceOffset:0,to:projected,destinationOffset:0,size:bytes)
            copy.copy(from:r.indicesA,sourceOffset:0,to:indices,destinationOffset:0,size:a.count*8)
        }; copy.endEncoding()
        submission.render.commit(); submission.render.waitUntilCompleted()
        for command in [submission.projection,submission.sort,submission.render] where command.status == .error {
            throw RendererError.message(command.error?.localizedDescription ?? "QA GPU command failed")
        }
        var bgra=[UInt8](); bgra.reserveCapacity(width*height*4)
        let pointer=pixels.contents().assumingMemoryBound(to:UInt8.self)
        for y in 0..<height { bgra.append(contentsOf:UnsafeBufferPointer(start:pointer+y*rowBytes,count:width*4)) }
        let pickIndex = r.pick.contents().assumingMemoryBound(to:UInt32.self).pointee
        return GaussianDebugFrame(bgra:bgra,width:width,height:height,
            projected:Array(UnsafeBufferPointer(start:projected.contents().assumingMemoryBound(to:ProjectedGaussianRecord.self),count:a.count)),
            sortedIndices:(0..<a.count).map { indices.contents().assumingMemoryBound(to:UInt32.self)[$0*2+1] },
            visibleCount:Int(r.visible.contents().assumingMemoryBound(to:UInt32.self).pointee),
            pickedIndex:pick == nil || pickIndex == .max ? nil : pickIndex,
            scenePick: pick == nil || scenePickTolerance == nil ? .empty:scenePickResult(r.pick, gaussianCount: a.count))
    }


#endif
}

/// Generation cancellation is shared by file reads, metric work and callbacks.
final class ComparisonCancellation {
  private let lock = NSLock()
  private var value = false
  func cancel() { lock.lock(); value = true; lock.unlock() }
  var cancelled: Bool { lock.lock(); defer {lock.unlock()}; return value }
}
