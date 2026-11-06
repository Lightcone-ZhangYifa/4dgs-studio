import Foundation

/// Pure model tests; AppKit drawing is not instantiated by this probe.
@main struct TimelineWindowAcceptance {
    static func main() throws {
        var records = [[String:Any]]()
        func check(_ name:String,_ value:Bool) {records.append(["name":name,"passed":value])}
        func valid(_ w:TimelineWindow)->Bool {w.lower.isFinite && w.upper.isFinite && w.upper>w.lower && w.span.isFinite && w.span>0}
        func same(_ a:TimelineWindow,_ b:TimelineWindow)->Bool {a.lower==b.lower && a.upper==b.upper}
        var w=TimelineWindow();w.fit([])
        check("empty document has a usable finite time viewport",valid(w))
        w.fit([4]);check("single sample is contained in finite positive range",valid(w) && w.lower<=4 && w.upper>=4)
        w.fit([-3,0,12]);check("fit contains all document samples with margin",valid(w) && w.lower < -3 && w.upper > 12)
        let before=w,anchor=0.27,anchorTime=w.time(at:anchor)
        w.zoom(0.3,anchor:anchor)
        check("zoom preserves pointer time",abs(w.time(at:anchor)-anchorTime)<1e-10)
        check("zoom applies requested scale",abs(w.span-before.span*0.3)<1e-10)
        let prePan=w;w.pan(0.7)
        check("pan keeps span and moves by viewport fraction",abs(w.span-prePan.span)<1e-10 && abs(w.lower-prePan.lower-prePan.span*0.7)<1e-10)
        let saved=w
        for factor in [Double.nan,.infinity,-.infinity,-1,0] {w.zoom(factor,anchor:0.5)}
        check("invalid zoom factors leave range unchanged",same(w,saved))
        for fraction in [Double.nan,.infinity,-.infinity] {w.pan(fraction)}
        check("invalid pan fractions leave range unchanged",same(w,saved))
        w.zoom(0.5,anchor:.nan)
        check("invalid zoom anchor leaves range unchanged",same(w,saved))
        w=TimelineWindow();w.fit([1e16])
        check("single sample at large clock origin retains representable span",valid(w) && w.lower<=1e16 && w.upper>=1e16)
        w.fit([1e16,1e16+32]);for _ in 0..<20 {w.zoom(0.01,anchor:0.5)}
        check("deep zoom at large clock origin never collapses representable bounds",valid(w))
        w=TimelineWindow();w.fit([-1e16,-1e16+64]);for _ in 0..<20 {w.zoom(0.01,anchor:0.8)}
        check("negative large clock origin has finite positive zoom range",valid(w))
        w=TimelineWindow();w.fit([Double.greatestFiniteMagnitude])
        check("fit near maximum finite time does not create infinity",valid(w))
        w=TimelineWindow();w.fit([-Double.greatestFiniteMagnitude,Double.greatestFiniteMagnitude])
        check("extreme domain fit cannot poison rendering with non-finite span",valid(w))
        w=TimelineWindow();w.fit([0,10]);w.pan(Double.greatestFiniteMagnitude)
        check("overflowing pan cannot poison rendering with infinity",valid(w))
        w=TimelineWindow();w.fit([0,1]);w.zoom(Double.leastNonzeroMagnitude,anchor:0.5)
        check("underflowing zoom preserves usable range",valid(w))
        let failed=records.filter{($0["passed"] as? Bool) == false}
        let result:[String:Any]=["passed":failed.isEmpty,"check_count":records.count,"failed_count":failed.count,"checks":records,"scope":"TimelineWindow numeric invariants only; does not simulate AppKit timeline drawing or keyframe editing."]
        try JSONSerialization.data(withJSONObject:result,options:[.prettyPrinted,.sortedKeys]).write(to:URL(fileURLWithPath:CommandLine.arguments[1]))
        print("Timeline: \(records.count-failed.count)/\(records.count) checks pass")
        for record in failed {print("FAIL: \(record["name"]!)")}
        if !failed.isEmpty {exit(1)}
    }
}
