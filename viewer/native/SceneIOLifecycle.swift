import Foundation

/// One document/visibility generation. Workers never read mutable controller
/// generations across threads; cancellation alone is shared through this lock.
final class SceneIOCancellation {
  private let lock = NSLock()
  private var cancelled = false
  var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
  func cancel() { lock.lock(); cancelled = true; lock.unlock() }
  func check() throws { if isCancelled { throw CancellationError() } }
}

/// Serial scene I/O discards canceled queued work before parsing or allocating
/// scene data. Each active request also checks cancellation between bounded
/// constructor/frame operations; its autorelease pool releases temporary data
/// at the end of that request rather than at the lifetime of the worker thread.
final class SceneIOWorker {
  private let queue = DispatchQueue(label: "org.gsv.scene-io", qos: .userInitiated)
  func submit(cancellation: SceneIOCancellation, _ body: @escaping () -> Void) {
    queue.async {
      guard !cancellation.isCancelled else { return }
      autoreleasepool { body() }
    }
  }
}
