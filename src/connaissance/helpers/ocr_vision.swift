// OCR local via le framework Vision de macOS (moteur Live Text).
// Usage : ocr_vision <fichier.pdf|image> [maxpages]  → JSON {text, confidence, pages}
// Gratuit, local, Neural Engine. Lit le chemin fourni (miroir SSD) — aucun download.
import Foundation
import Vision
import PDFKit
import CoreGraphics
import AppKit

func ocr(_ cg: CGImage) -> (String, Double, Int) {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["fr-FR", "en-US"]
    req.usesLanguageCorrection = true
    let h = VNImageRequestHandler(cgImage: cg, options: [:])
    try? h.perform([req])
    var lines: [String] = []; var conf = 0.0; var n = 0
    for o in (req.results ?? []) {
        if let c = o.topCandidates(1).first { lines.append(c.string); conf += Double(c.confidence); n += 1 }
    }
    return (lines.joined(separator: "\n"), n > 0 ? conf / Double(n) : 0.0, n)
}

func render(_ page: PDFPage, scale: CGFloat) -> CGImage? {
    let r = page.bounds(for: .mediaBox)
    let w = Int(r.width * scale), h = Int(r.height * scale)
    guard w > 0, h > 0,
          let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: ctx)
    return ctx.makeImage()
}

let args = CommandLine.arguments
guard args.count > 1 else { exit(1) }
let url = URL(fileURLWithPath: args[1])
let maxPages = args.count > 2 ? (Int(args[2]) ?? 50) : 50
var parts: [String] = []; var confs: [Double] = []; var pages = 0
if url.pathExtension.lowercased() == "pdf" {
    guard let doc = PDFDocument(url: url) else { exit(2) }
    for i in 0..<min(doc.pageCount, maxPages) {
        if let p = doc.page(at: i), let cg = render(p, scale: 2.0) {
            let (t, c, n) = ocr(cg); if !t.isEmpty { parts.append(t); if n > 0 { confs.append(c) } }; pages += 1
        }
    }
} else if let img = NSImage(contentsOf: url), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
    let (t, c, n) = ocr(cg); parts.append(t); if n > 0 { confs.append(c) }; pages = 1
}
let text = parts.joined(separator: "\n\n")
let conf = confs.isEmpty ? 0.0 : confs.reduce(0, +) / Double(confs.count)
let obj: [String: Any] = ["text": text, "confidence": conf, "pages": pages]
let data = try! JSONSerialization.data(withJSONObject: obj)
FileHandle.standardOutput.write(data)
