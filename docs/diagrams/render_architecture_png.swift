// Rasterize this diagram's SVG subset using macOS AppKit; no external packages.
import AppKit
import Foundation

let source = CommandLine.arguments[1]
let destination = CommandLine.arguments[2]
let width = 2600, height = 2470
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width,
    pixelsHigh: height, bitsPerSample: 8, samplesPerPixel: 4,
    hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
    bytesPerRow: 0, bitsPerPixel: 0)!
let context = NSGraphicsContext(bitmapImageRep: bitmap)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context
let transform = NSAffineTransform()
transform.translateX(by: 0, yBy: CGFloat(height))
transform.scaleX(by: 1, yBy: -1)
transform.concat()

func color(_ s: String) -> NSColor {
    let n = UInt32(s.trimmingCharacters(in: CharacterSet(charactersIn: "#")), radix: 16) ?? 0
    return NSColor(srgbRed: CGFloat((n >> 16) & 255)/255,
        green: CGFloat((n >> 8) & 255)/255, blue: CGFloat(n & 255)/255, alpha: 1)
}
final class Renderer: NSObject, XMLParserDelegate {
    var attrs: [String:String] = [:]
    var content = ""
    var activeText = false
    var inDefs = false
    func parser(_ parser: XMLParser, didStartElement name: String, namespaceURI: String?, qualifiedName: String?, attributes a: [String:String]) {
        if name == "defs" { inDefs = true }
        if inDefs { return }
        func v(_ key: String) -> CGFloat { CGFloat(Double(a[key] ?? "0") ?? 0) }
        if name == "rect" {
            let p = NSBezierPath(roundedRect: NSRect(x:v("x"),y:v("y"),width:v("width"),height:v("height")),xRadius:v("rx"),yRadius:v("rx"))
            color(a["fill"] ?? "#ffffff").setFill(); p.fill()
            if let stroke = a["stroke"] { color(stroke).setStroke(); p.lineWidth = v("stroke-width"); p.stroke() }
        }
        if name == "polyline" {
            let points = (a["points"] ?? "").split(separator:" ").map { item -> NSPoint in
                let xy = item.split(separator:",").map { Double($0)! }
                return NSPoint(x:xy[0],y:xy[1])
            }
            guard points.count > 1 else { return }
            let p = NSBezierPath(); p.move(to:points[0]); for point in points.dropFirst() { p.line(to:point) }
            let c = color(a["stroke"] ?? "#536579"); c.setStroke(); c.setFill(); p.lineWidth = 3
            if a["stroke-dasharray"] != nil { p.setLineDash([10,8],count:2,phase:0) }
            p.stroke()
            let end=points.last!, prev=points[points.count-2]
            let angle=atan2(end.y-prev.y,end.x-prev.x)
            let arrow=NSBezierPath(); arrow.move(to:end)
            arrow.line(to:NSPoint(x:end.x-18*cos(angle)+7*sin(angle),y:end.y-18*sin(angle)-7*cos(angle)))
            arrow.line(to:NSPoint(x:end.x-18*cos(angle)-7*sin(angle),y:end.y-18*sin(angle)+7*cos(angle)))
            arrow.close(); arrow.fill()
        }
        if name == "text" { attrs=a; content=""; activeText=true }
    }
    func parser(_ parser: XMLParser, foundCharacters string: String) { if activeText { content += string } }
    func parser(_ parser: XMLParser, didEndElement name: String, namespaceURI: String?, qualifiedName: String?) {
        if name == "defs" { inDefs=false }
        if name != "text" || !activeText { return }
        activeText=false
        let cls=attrs["class"] ?? "body"
        let size: CGFloat = ["title":48,"sub":27,"heading":31,"body":27,"code":23,"label":23,"lane":24][cls] ?? 27
        let bold=["title","heading","lane"].contains(cls)
        let font=NSFont(name:bold ? "STHeitiSC-Medium" : "STHeitiSC-Light",size:size) ?? NSFont.systemFont(ofSize:size,weight:bold ? .semibold : .regular)
        let textColor = ["code","label","lane","sub"].contains(cls) ? "#52667a" : "#172d43"
        let x=Double(attrs["x"]!)!, y=Double(attrs["y"]!)!
        NSGraphicsContext.saveGraphicsState()
        let t=NSAffineTransform(); t.translateX(by:x,yBy:y); t.scaleX(by:1,yBy:-1); t.concat()
        (content as NSString).draw(at:NSPoint(x:0,y:-font.descender),withAttributes:[.font:font,.foregroundColor:color(textColor)])
        NSGraphicsContext.restoreGraphicsState()
    }
}
let renderer=Renderer()
let parser=XMLParser(data:try Data(contentsOf:URL(fileURLWithPath:source)))
parser.delegate=renderer
guard parser.parse() else { fatalError("SVG parse failed") }
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using:.png,properties:[:])!.write(to:URL(fileURLWithPath:destination))
print(destination)
