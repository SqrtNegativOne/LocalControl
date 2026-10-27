// Draws the LocalControl mascot (a happy little padlock) and writes the app icons.
// Usage: swift Design/make_icons.swift <output-dir>
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ".")
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(red: CGFloat(hex >> 16 & 0xFF) / 255, green: CGFloat(hex >> 8 & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: a)
}

func render(_ px: Int, _ draw: (CGContext) -> Void) -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    let ctx = NSGraphicsContext(bitmapImageRep: rep)!.cgContext
    // Work in a 1024-unit, top-left-origin space.
    ctx.translateBy(x: 0, y: CGFloat(px))
    ctx.scaleBy(x: CGFloat(px) / 1024, y: -CGFloat(px) / 1024)
    draw(ctx)
    return rep
}

func save(_ rep: NSBitmapImageRep, _ name: String) {
    try! rep.representation(using: .png, properties: [:])!.write(to: out.appendingPathComponent(name))
}

/// Mint gradient background filling `rect`.
func background(_ ctx: CGContext, _ rect: CGRect) {
    let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                       colors: [rgb(0xC4F2DE), rgb(0x7DD3B5), rgb(0x4FB89A)] as CFArray, locations: [0, 0.6, 1])!
    ctx.drawLinearGradient(g, start: CGPoint(x: rect.minX, y: rect.minY), end: CGPoint(x: rect.minX + rect.width * 0.4, y: rect.maxY), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
}

func sparkle(_ ctx: CGContext, _ c: CGPoint, _ r: CGFloat, _ color: CGColor) {
    let p = CGMutablePath()
    p.move(to: CGPoint(x: c.x, y: c.y - r))
    p.addQuadCurve(to: CGPoint(x: c.x + r, y: c.y), control: c)
    p.addQuadCurve(to: CGPoint(x: c.x, y: c.y + r), control: c)
    p.addQuadCurve(to: CGPoint(x: c.x - r, y: c.y), control: c)
    p.addQuadCurve(to: CGPoint(x: c.x, y: c.y - r), control: c)
    ctx.addPath(p)
    ctx.setFillColor(color)
    ctx.fillPath()
}

/// The padlock character, drawn in 1024 units centred on the canvas.
func mascot(_ ctx: CGContext) {
    let ink = rgb(0x24493F)

    // Shackle
    ctx.setLineCap(.round)
    ctx.setLineWidth(78)
    ctx.setStrokeColor(rgb(0x2F6B5B))
    let shackle = CGMutablePath()
    shackle.move(to: CGPoint(x: 362, y: 520))
    shackle.addLine(to: CGPoint(x: 362, y: 400))
    shackle.addArc(center: CGPoint(x: 512, y: 400), radius: 150, startAngle: .pi, endAngle: 0, clockwise: false)
    shackle.addLine(to: CGPoint(x: 662, y: 520))
    ctx.addPath(shackle)
    ctx.strokePath()
    // Shine on the shackle
    ctx.setLineWidth(18)
    ctx.setStrokeColor(rgb(0xFFFFFF, 0.35))
    ctx.addArc(center: CGPoint(x: 512, y: 400), radius: 150, startAngle: .pi * 1.15, endAngle: .pi * 1.4, clockwise: false)
    ctx.strokePath()

    // Body, with a soft drop shadow
    let body = CGPath(roundedRect: CGRect(x: 252, y: 470, width: 520, height: 400), cornerWidth: 130, cornerHeight: 130, transform: nil)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: 18), blur: 30, color: rgb(0x1F4D40, 0.35))
    ctx.addPath(body)
    ctx.setFillColor(rgb(0xFFF7E8))
    ctx.fillPath()
    ctx.restoreGState()
    // Bottom shade inside the body
    ctx.saveGState()
    ctx.addPath(body)
    ctx.clip()
    let shade = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                           colors: [rgb(0xFFF7E8, 0), rgb(0xF5DFC0, 1)] as CFArray, locations: [0.55, 1])!
    ctx.drawLinearGradient(shade, start: CGPoint(x: 0, y: 470), end: CGPoint(x: 0, y: 870), options: [])
    ctx.restoreGState()

    // Eyes with highlights
    for x in [430.0, 594.0] {
        ctx.setFillColor(ink)
        ctx.fillEllipse(in: CGRect(x: x - 34, y: 600, width: 68, height: 80))
        ctx.setFillColor(rgb(0xFFFFFF))
        ctx.fillEllipse(in: CGRect(x: x - 14, y: 612, width: 24, height: 26))
        ctx.fillEllipse(in: CGRect(x: x + 6, y: 648, width: 10, height: 10))
    }
    // Cheeks
    ctx.setFillColor(rgb(0xFF8FA3, 0.55))
    ctx.fillEllipse(in: CGRect(x: 330, y: 690, width: 84, height: 46))
    ctx.fillEllipse(in: CGRect(x: 610, y: 690, width: 84, height: 46))
    // Smile
    ctx.setStrokeColor(ink)
    ctx.setLineWidth(16)
    ctx.addArc(center: CGPoint(x: 512, y: 690), radius: 36, startAngle: .pi * 0.15, endAngle: .pi * 0.85, clockwise: false)
    ctx.strokePath()

    // Sparkles
    sparkle(ctx, CGPoint(x: 790, y: 300), 52, rgb(0xFFFFFF, 0.95))
    sparkle(ctx, CGPoint(x: 850, y: 400), 24, rgb(0xFFFFFF, 0.8))
    sparkle(ctx, CGPoint(x: 220, y: 330), 30, rgb(0xFFFFFF, 0.7))
}

// iOS: full-bleed square (the system applies the mask).
save(render(1024) { ctx in
    background(ctx, CGRect(x: 0, y: 0, width: 1024, height: 1024))
    mascot(ctx)
}, "icon-ios-1024.png")

// macOS: rounded tile with margin and shadow, per the macOS icon grid.
for px in [16, 32, 64, 128, 256, 512, 1024] {
    save(render(px) { ctx in
        let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
        let path = CGPath(roundedRect: tile, cornerWidth: 185, cornerHeight: 185, transform: nil)
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: 10), blur: 24, color: rgb(0x000000, 0.3))
        ctx.addPath(path)
        ctx.setFillColor(rgb(0x7DD3B5))
        ctx.fillPath()
        ctx.restoreGState()
        ctx.saveGState()
        ctx.addPath(path)
        ctx.clip()
        background(ctx, tile)
        ctx.translateBy(x: 100, y: 100)
        ctx.scaleBy(x: 824 / 1024, y: 824 / 1024)
        mascot(ctx)
        ctx.restoreGState()
    }, "icon-mac-\(px).png")
}

// Menu bar: template silhouette (black + alpha) with the face cut out.
for (px, name) in [(18, "menubar.png"), (36, "menubar@2x.png")] {
    save(render(px) { ctx in
        ctx.setStrokeColor(rgb(0x000000))
        ctx.setLineCap(.round)
        ctx.setLineWidth(110)
        let s = CGMutablePath()
        s.move(to: CGPoint(x: 322, y: 470))
        s.addLine(to: CGPoint(x: 322, y: 330))
        s.addArc(center: CGPoint(x: 512, y: 330), radius: 190, startAngle: .pi, endAngle: 0, clockwise: false)
        s.addLine(to: CGPoint(x: 702, y: 470))
        ctx.addPath(s)
        ctx.strokePath()
        ctx.setFillColor(rgb(0x000000))
        ctx.addPath(CGPath(roundedRect: CGRect(x: 132, y: 440, width: 760, height: 560), cornerWidth: 170, cornerHeight: 170, transform: nil))
        ctx.fillPath()
        ctx.setBlendMode(.clear)
        ctx.fillEllipse(in: CGRect(x: 330, y: 610, width: 110, height: 130))
        ctx.fillEllipse(in: CGRect(x: 584, y: 610, width: 110, height: 130))
        ctx.setLineWidth(60)
        ctx.addArc(center: CGPoint(x: 512, y: 780), radius: 80, startAngle: .pi * 0.15, endAngle: .pi * 0.85, clockwise: false)
        ctx.strokePath()
    }, name)
}
print("wrote icons to \(out.path)")
