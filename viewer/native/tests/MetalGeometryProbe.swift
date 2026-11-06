// Independent QA driver. No CPU expectations are taken from renderer output.
import Foundation
import MetalKit
import simd

private struct Case: Decodable {
    let name: String
    let a: [[Float]]
    let b: [[Float]]?
    let interpolation: Float
    let worldToCamera: [[Float]]
    let K: [[Float]]
    let width: Int
    let height: Int
    let integerPixelCenters: Bool
    let background: [Float]
    let pick: [UInt32]?
}

private func records(_ data: [[Float]]) -> [GaussianGPURecord] {
    data.map { a in
        precondition(a.count == 16)
        return GaussianGPURecord(mean: SIMD4(a[0],a[1],a[2],a[3]),
            scale: SIMD4(a[4],a[5],a[6],a[7]), quat: SIMD4(a[8],a[9],a[10],a[11]),
            color: SIMD4(a[12],a[13],a[14],a[15]))
    }
}
private func values(_ a: SIMD4<Float>) -> [Float] { [a.x,a.y,a.z,a.w] }

@main
private struct Main {
    static func main() throws {
        guard CommandLine.arguments.count == 4 else { fatalError("input.json output.json Shaders.metal") }
        let input = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let cases = try JSONDecoder().decode([Case].self, from: input)
        let view = MTKView(frame: .zero, device: MTLCreateSystemDefaultDevice())
        let renderer = try GaussianRenderer(view: view, shaderURL: URL(fileURLWithPath: CommandLine.arguments[3]))
        view.isPaused = true
        var results = [[String: Any]]()
        for item in cases {
            let w = item.worldToCamera, k = item.K
            let camera = simd_float4x4(columns: (SIMD4(w[0][0],w[1][0],w[2][0],w[3][0]),
                SIMD4(w[0][1],w[1][1],w[2][1],w[3][1]), SIMD4(w[0][2],w[1][2],w[2][2],w[3][2]),
                SIMD4(w[0][3],w[1][3],w[2][3],w[3][3])))
            let intrinsics = simd_float3x3(columns: (SIMD3(k[0][0],k[1][0],k[2][0]),
                SIMD3(k[0][1],k[1][1],k[2][1]), SIMD3(k[0][2],k[1][2],k[2][2])))
            let p = item.background
            let result = try renderer.debugRender(a: records(item.a), b: item.b.map(records),
                interpolation: item.interpolation, worldToCamera: camera, intrinsics: intrinsics,
                width: item.width, height: item.height, integerPixelCenters: item.integerPixelCenters,
                background: SIMD4(p[0],p[1],p[2],p[3]), pick: item.pick.map { SIMD2($0[0],$0[1]) })
            results.append(["name": item.name, "bgra_base64": Data(result.bgra).base64EncodedString(),
                "projected": result.projected.map { ["centerDepth": values($0.centerDepth), "axes": values($0.axes),
                    "color": values($0.color), "sourceIndex": $0.info.x, "visible": $0.info.y] },
                "sortedIndices": result.sortedIndices, "visibleCount": result.visibleCount,
                "pickedIndex": result.pickedIndex.map { $0 as Any } ?? NSNull()])
        }
        let output: [String: Any] = ["device": renderer.device.name, "results": results]
        try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]).write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
    }
}
