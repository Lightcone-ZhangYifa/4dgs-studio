#!/usr/bin/env swift
import AppKit
import Foundation

// One vector scene supplies both the editable SVG and every raster icon size.
// Rendering uses a CPU bitmap CGContext; no window, Metal, network or fonts.
// Usage: xcrun swift generate_app_icon.swift ICONSET_DIRECTORY SVG_OUTPUT

private struct Color {
    let hex: String
    let alpha: CGFloat
    init(_ hex: String, _ alpha: CGFloat = 1) { self.hex = hex; self.alpha = alpha }
    var cg: CGColor {
        let value = UInt32(hex, radix: 16)!
        return CGColor(colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, components: [
            CGFloat((value >> 16) & 255) / 255, CGFloat((value >> 8) & 255) / 255,
            CGFloat(value & 255) / 255, alpha])!
    }
}
private enum Command {
    case move(CGPoint), line(CGPoint), curve(CGPoint, CGPoint, CGPoint), close
}
private func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: x, y: y) }
private func number(_ value: CGFloat) -> String {
    String(format: "%.4f", locale: Locale(identifier: "en_US_POSIX"), Double(value))
        .replacingOccurrences(of: "0+$", with: "", options: .regularExpression)
        .replacingOccurrences(of: "\\.$", with: "", options: .regularExpression)
}
private func pair(_ value: CGPoint) -> String { "\(number(value.x)) \(number(value.y))" }
private struct VectorPath {
    let commands: [Command]
    var cg: CGPath {
        let result = CGMutablePath()
        for command in commands {
            switch command {
            case .move(let p): result.move(to: p)
            case .line(let p): result.addLine(to: p)
            case .curve(let a, let b, let p): result.addCurve(to: p, control1: a, control2: b)
            case .close: result.closeSubpath()
            }
        }
        return result
    }
    var svg: String {
        commands.map { command in
            switch command {
            case .move(let p): return "M " + pair(p)
            case .line(let p): return "L " + pair(p)
            case .curve(let a, let b, let p): return "C " + pair(a) + " " + pair(b) + " " + pair(p)
            case .close: return "Z"
            }
        }.joined(separator: " ")
    }
}
private func roundedSquare(_ inset: CGFloat, _ radius: CGFloat) -> VectorPath {
    let a = inset, b = 1024 - inset, r = radius, k: CGFloat = 0.5522847498307936
    return VectorPath(commands: [
        .move(point(a+r,a)), .line(point(b-r,a)),
        .curve(point(b-r+k*r,a),point(b,a+r-k*r),point(b,a+r)), .line(point(b,b-r)),
        .curve(point(b,b-r+k*r),point(b-r+k*r,b),point(b-r,b)), .line(point(a+r,b)),
        .curve(point(a+r-k*r,b),point(a,b-r+k*r),point(a,b-r)), .line(point(a,a+r)),
        .curve(point(a,a+r-k*r),point(a+r-k*r,a),point(a+r,a)), .close])
}
private func ellipse(_ cx: CGFloat, _ cy: CGFloat, _ rx: CGFloat, _ ry: CGFloat,
                     angle: CGFloat = 0) -> VectorPath {
    let k: CGFloat = 0.5522847498307936
    func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
        point(cx + x*cos(angle)-y*sin(angle), cy + x*sin(angle)+y*cos(angle))
    }
    return VectorPath(commands: [
        .move(p(rx,0)), .curve(p(rx,k*ry),p(k*rx,ry),p(0,ry)),
        .curve(p(-k*rx,ry),p(-rx,k*ry),p(-rx,0)),
        .curve(p(-rx,-k*ry),p(-k*rx,-ry),p(0,-ry)),
        .curve(p(k*rx,-ry),p(rx,-k*ry),p(rx,0)), .close])
}
private enum Paint {
    case solid(Color)
    case linear(String, CGPoint, CGPoint, Color, Color)
    case radial(String, CGPoint, CGFloat, Color, Color)
    var svgValue: String {
        switch self { case .solid(let c): return "#" + c.hex
        case .linear(let id,_,_,_,_), .radial(let id,_,_,_,_): return "url(#\(id))" }
    }
    var svgOpacity: CGFloat { if case .solid(let c) = self { return c.alpha }; return 1 }
    var definition: String? {
        func stops(_ a: Color, _ b: Color) -> String {
            "<stop offset=\"0\" stop-color=\"#\(a.hex)\" stop-opacity=\"\(number(a.alpha))\"/>" +
            "<stop offset=\"1\" stop-color=\"#\(b.hex)\" stop-opacity=\"\(number(b.alpha))\"/>"
        }
        switch self {
        case .solid: return nil
        case .linear(let id,let a,let b,let ca,let cb):
            return "<linearGradient id=\"\(id)\" gradientUnits=\"userSpaceOnUse\" x1=\"\(number(a.x))\" y1=\"\(number(a.y))\" x2=\"\(number(b.x))\" y2=\"\(number(b.y))\">\(stops(ca,cb))</linearGradient>"
        case .radial(let id,let center,let radius,let ca,let cb):
            return "<radialGradient id=\"\(id)\" gradientUnits=\"userSpaceOnUse\" cx=\"\(number(center.x))\" cy=\"\(number(center.y))\" r=\"\(number(radius))\">\(stops(ca,cb))</radialGradient>"
        }
    }
    func fill(_ context: CGContext, path: CGPath) {
        context.saveGState(); defer { context.restoreGState() }
        switch self {
        case .solid(let color): context.addPath(path); context.setFillColor(color.cg); context.fillPath()
        case .linear(_,let a,let b,let ca,let cb):
            context.addPath(path); context.clip()
            let gradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                colors: [ca.cg,cb.cg] as CFArray, locations: [0,1])!
            context.drawLinearGradient(gradient, start: a, end: b,
                options: [.drawsBeforeStartLocation,.drawsAfterEndLocation])
        case .radial(_,let center,let radius,let ca,let cb):
            context.addPath(path); context.clip()
            let gradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                colors: [ca.cg,cb.cg] as CFArray, locations: [0,1])!
            context.drawRadialGradient(gradient, startCenter: center, startRadius: 0,
                endCenter: center, endRadius: radius, options: [.drawsAfterEndLocation])
        }
    }
}
private struct Shape {
    let path: VectorPath
    var fill: Paint? = nil
    var stroke: Color? = nil
    var width: CGFloat = 1
    func draw(_ context: CGContext, minimumStroke: CGFloat = 0) {
        fill?.fill(context, path: path.cg)
        if let stroke {
            context.addPath(path.cg); context.setStrokeColor(stroke.cg)
            context.setLineWidth(width > 4 ? max(width,minimumStroke) : width)
            context.setLineCap(.round); context.setLineJoin(.round); context.strokePath()
        }
    }
    var svg: String {
        var attributes = "d=\"\(path.svg)\" fill=\"\(fill?.svgValue ?? "none")\""
        if let fill, fill.svgOpacity < 1 { attributes += " fill-opacity=\"\(number(fill.svgOpacity))\"" }
        if let stroke {
            attributes += " stroke=\"#\(stroke.hex)\" stroke-opacity=\"\(number(stroke.alpha))\" stroke-width=\"\(number(width))\" stroke-linecap=\"round\" stroke-linejoin=\"round\""
        }
        return "<path \(attributes)/>"
    }
}

// A luminous anisotropic Gaussian and three persistent colored trajectories.
// Back/front curve portions express spatial depth; the gold point is a playhead.
private let scene: [Shape] = [
    Shape(path: roundedSquare(40,208), fill: .linear("tile",point(160,80),point(840,944),Color("253D60"),Color("091525"))),
    Shape(path: roundedSquare(42,206), stroke: Color("A9CDE9",0.19), width: 3),
    Shape(path: ellipse(512,512,344,302), fill: .radial("halo",point(500,490),344,Color("53B7DE",0.22),Color("53B7DE",0))),
    Shape(path: ellipse(512,512,306,160,angle: -0.50), stroke: Color("3086BE",0.58), width: 16),
    Shape(path: ellipse(512,512,278,112,angle: 0.90), stroke: Color("D68F46",0.5), width: 14),
    Shape(path: ellipse(512,512,204,268,angle: 0.25), stroke: Color("9382D5",0.6), width: 12),
    Shape(path: ellipse(520,532,175,140,angle: -0.50), fill: .radial("gaussian",point(461,461),249,Color("ABF4E6"),Color("205C91")),stroke:Color("CEFFF3",0.38),width:3),
    Shape(path: ellipse(479,463,89,57,angle: -0.50), fill: .radial("highlight",point(472,454),108,Color("DEFFF4",0.66),Color("DEFFF4",0))),
    Shape(path: VectorPath(commands: [.move(point(238,647)),
        .curve(point(340,786),point(689,713),point(792,447))]), stroke: Color("6ADFDA"), width: 20),
    Shape(path: VectorPath(commands: [.move(point(373,283)),
        .curve(point(408,306),point(550,531),point(573,693)),
        .curve(point(583,767),point(560,791),point(520,780))]), stroke: Color("F5B765"), width: 16),
    Shape(path: VectorPath(commands: [.move(point(311,445)),
        .curve(point(315,675),point(502,828),point(645,737))]), stroke: Color("C3A1F0"), width: 14),
    Shape(path: ellipse(238,647,15,15), fill: .solid(Color("91EEE6"))),
    Shape(path: ellipse(792,447,27,27), fill: .radial("playhead",point(783,438),38,Color("FFF2C3"),Color("EDA551"))),
    Shape(path: ellipse(520,780,13,13), fill: .solid(Color("F5BC71"))),
    Shape(path: ellipse(645,737,12,12), fill: .solid(Color("DAC2FC")))
]

private func svgDocument() -> String {
    let definitions = scene.compactMap { $0.fill?.definition }.joined(separator: "\n    ")
    return """
    <?xml version="1.0" encoding="UTF-8"?>
    <svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024" role="img" aria-labelledby="title description">
      <title id="title">4DGS Studio</title>
      <desc id="description">A spatial Gaussian with continuous cyan, gold and violet trajectories and a highlighted time sample.</desc>
      <defs>
        \(definitions)
      </defs>
      \(scene.map(\.svg).joined(separator: "\n  "))
    </svg>

    """
}
private func png(_ size: Int) throws -> Data {
    guard let context = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8,
        bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        throw NSError(domain: "GSVIcon", code: 1, userInfo: [NSLocalizedDescriptionKey: "Cannot allocate icon bitmap"])
    }
    context.setShouldAntialias(true); context.translateBy(x: 0, y: CGFloat(size))
    context.scaleBy(x: CGFloat(size)/1024, y: -CGFloat(size)/1024)
    // Optical stroke weight preserves the trajectory symbol at Finder's small
    // sizes; it changes neither the source geometry nor the large-size artwork.
    let minimumStroke: CGFloat = size <= 16 ? 40 : size <= 32 ? 28 : 0
    for (index,shape) in scene.enumerated() {
        context.saveGState()
        if index >= 2, size <= 32 {
            context.translateBy(x: 512, y: 512); context.scaleBy(x: 1.1, y: 1.1)
            context.translateBy(x: -512, y: -512)
        }
        shape.draw(context, minimumStroke: minimumStroke)
        context.restoreGState()
    }
    guard let image = context.makeImage(),
        let data = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else {
        throw NSError(domain: "GSVIcon", code: 2, userInfo: [NSLocalizedDescriptionKey: "Cannot encode icon PNG"])
    }
    return data
}

guard CommandLine.arguments.count == 3 else {
    fputs("Usage: generate_app_icon.swift ICONSET_DIRECTORY SVG_OUTPUT\n", stderr); exit(2)
}
let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let svgURL = URL(fileURLWithPath: CommandLine.arguments[2])
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
let sizes = [("icon_16x16.png",16),("icon_16x16@2x.png",32),
    ("icon_32x32.png",32),("icon_32x32@2x.png",64),
    ("icon_128x128.png",128),("icon_128x128@2x.png",256),
    ("icon_256x256.png",256),("icon_256x256@2x.png",512),
    ("icon_512x512.png",512),("icon_512x512@2x.png",1024)]
var rasters = [Int: Data]()
for (name,size) in sizes {
    if rasters[size] == nil { rasters[size] = try png(size) }
    try rasters[size]!.write(to: directory.appendingPathComponent(name), options: .atomic)
}
try svgDocument().write(to: svgURL, atomically: true, encoding: .utf8)
print("Generated ten icon representations and the shared vector SVG.")
