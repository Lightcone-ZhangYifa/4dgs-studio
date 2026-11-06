import Foundation

/// Deterministic tests of the production serial queue and generation token.
/// No window, document renderer, GPU workload or external download is used.
@main struct SceneIOLifecycleAcceptance {
  final class State {
    private let lock = NSLock()
    private var values = [String: Int]()
    func increment(_ key: String) { lock.lock(); values[key, default: 0] += 1; lock.unlock() }
    func value(_ key: String) -> Int { lock.lock(); defer { lock.unlock() }; return values[key] ?? 0 }
  }
  final class Payload {
    let bytes = [UInt8](repeating: 7, count: 4096)
    let state: State
    init(_ state: State) { self.state = state }
    deinit { state.increment("released") }
  }
  static func main() throws {
    var checks = [[String: Any]]()
    func check(_ passed: Bool, _ name: String) {
      checks.append(["name": name, "passed": passed]); if !passed { print("FAIL: " + name) }
    }
    func wait(_ signal: DispatchSemaphore) -> Bool { signal.wait(timeout: .now() + 5) == .success }
    let active = SceneIOCancellation()
    check((try? active.check()) != nil && !active.isCancelled, "A fresh generation permits normal scene I/O")
    active.cancel(); active.cancel()
    var canceledError = false
    do { try active.check() } catch is CancellationError { canceledError = true }
    check(active.isCancelled && canceledError, "Cancellation is idempotent and has a distinct cancellation result")

    // Hold the serial worker before submitting the superseded and current jobs.
    // Both are queued before cancellation, so no scheduler timing is assumed.
    let worker = SceneIOWorker(), state = State()
    let entered = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0)
    worker.submit(cancellation: SceneIOCancellation()) {
      entered.signal(); _ = release.wait(timeout: .now() + 5)
    }
    check(wait(entered), "The production scene worker starts requests in serial order")
    let superseded = SceneIOCancellation()
    var payload: Payload? = Payload(state)
    weak var weakPayload = payload
    worker.submit(cancellation: superseded) { [captured = payload!] in
      state.increment("superseded"); _ = captured.bytes[0]
    }
    payload = nil
    superseded.cancel()
    let completed = DispatchSemaphore(value: 0)
    worker.submit(cancellation: SceneIOCancellation()) {
      state.increment("current"); completed.signal()
    }
    release.signal()
    check(wait(completed) && state.value("superseded") == 0 && state.value("current") == 1,
      "A superseded queued request never parses or allocates scene data; its fresh replacement completes")
    let until = Date().addingTimeInterval(1)
    while weakPayload != nil && Date() < until { Thread.sleep(forTimeInterval: 0.001) }
    check(weakPayload == nil && state.value("released") == 1,
      "Discarding a canceled queued request releases its captured source payload")

    // Cancel one running request exactly at a bounded operation boundary.
    let cooperative = SceneIOCancellation(), phase = DispatchSemaphore(value: 0)
    let continuePhase = DispatchSemaphore(value: 0), finished = DispatchSemaphore(value: 0)
    worker.submit(cancellation: cooperative) {
      do {
        try cooperative.check(); state.increment("phase1"); phase.signal()
        _ = continuePhase.wait(timeout: .now() + 5)
        try cooperative.check(); state.increment("staleCompletion")
      } catch is CancellationError { state.increment("cooperativeCancel") }
      catch { state.increment("unexpectedError") }
      finished.signal()
    }
    let began = wait(phase)
    cooperative.cancel(); continuePhase.signal()
    check(began && wait(finished) && state.value("phase1") == 1
      && state.value("cooperativeCancel") == 1 && state.value("staleCompletion") == 0,
      "A running superseded request stops at its next constructor/frame boundary without publishing a result")
    let retried = DispatchSemaphore(value: 0)
    worker.submit(cancellation: SceneIOCancellation()) { state.increment("retry"); retried.signal() }
    check(wait(retried) && state.value("retry") == 1 && state.value("unexpectedError") == 0,
      "Cancellation leaves the serial worker usable for a fresh retry")

    // A worker result may already be queued for UI commit when its generation
    // is canceled. Exercise the same thread-safe token check used at handoff.
    let handoff = DispatchQueue(label: "org.gsv.qa.scene-handoff")
    let held = DispatchSemaphore(value: 0), resume = DispatchSemaphore(value: 0)
    handoff.async { held.signal(); _ = resume.wait(timeout: .now() + 5) }
    let heldReady = wait(held), late = SceneIOCancellation(), drained = DispatchSemaphore(value: 0)
    handoff.async { if !late.isCancelled { state.increment("lateCommit") } }
    late.cancel(); handoff.async { drained.signal() }; resume.signal()
    check(heldReady && wait(drained) && state.value("lateCommit") == 0,
      "A canceled generation also rejects an already queued result at its final commit boundary")
    let concurrent = SceneIOCancellation()
    DispatchQueue.concurrentPerform(iterations: 2000) { index in
      if index % 7 == 0 { concurrent.cancel() }
      _ = concurrent.isCancelled
      do { try concurrent.check() } catch is CancellationError {} catch { state.increment("unexpectedError") }
    }
    check(concurrent.isCancelled && state.value("unexpectedError") == 0,
      "Concurrent cancellation and checks use the token's lock and remain monotonic")
    let report: [String: Any] = ["passed": checks.allSatisfy { $0["passed"] as? Bool == true }, "checks": checks,
      "scope": "Production SceneIOWorker/token with deterministic queued, in-flight, retry and late-handoff schedules; no live window or GPU render.",
      "limitation": "A single document, semantic-object or frame constructor is bounded but not preempted internally; cancellation is checked at its boundaries."]
    try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
      .write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
    print("Scene I/O lifecycle: \(report["passed"]!)")
    if report["passed"] as? Bool != true { exit(1) }
  }
}
