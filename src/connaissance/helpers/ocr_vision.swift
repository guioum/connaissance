// OCR local structuré via le framework Vision de macOS (RecognizeDocumentsRequest,
// macOS 26+). Usage : ocr_vision <fichier.pdf|image> [maxpages] [--fuse]
//   → JSON {text, confidence, pages, lines} — text est du Markdown (paragraphes,
//     tableaux, listes) ; lines = nb de lignes reconnues (le Markdown fusionne
//     les lignes en paragraphes, le triage document/photo a besoin du vrai compte).
// --fuse (PDF born-digital) : la structure vient de Vision mais les caractères
//   viennent de la couche texte embarquée (PDFPage.selection par bloc/cellule)
//   → zéro erreur d'OCR sur les vrais born-digital. Fallback texte OCR par bloc
//   si la sélection est vide ; pages sans couche texte → OCR pur.
// Gratuit, local, Neural Engine. Lit le chemin fourni (miroir SSD) — aucun download.
// Sur un SDK < macOS 26 la compilation échoue → ocr_local.available() désactive
// proprement l'OCR local. Conversion structure → Markdown adaptée de
// riddleling/docOCR (MIT).
import Foundation
import Vision
import PDFKit
import CoreGraphics
import AppKit

struct Block {
    enum Kind { case paragraph, list, table }
    let kind: Kind
    let text: String
    let box: NormalizedRect
}

extension NormalizedRect {
    func isMostlyInside(_ other: NormalizedRect) -> Bool {
        let inter = cgRect.intersection(other.cgRect)
        let area = width * height
        guard !inter.isNull, area > 0 else { return false }
        return (inter.width * inter.height) / area > 0.6
    }
}

func joinLines(_ lines: [String]) -> String {
    lines.filter { !$0.isEmpty }.joined(separator: " ")
}

func normalize(_ t: DocumentObservation.Container.Text) -> String {
    let lines = t.lines.map { ($0.topCandidates(1).first?.string ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { !$0.isEmpty }
    if lines.isEmpty {
        return joinLines(t.transcript.components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) })
    }
    return joinLines(lines)
}

// Texte sélectionné visiblement cassé (glyphes éclatés en tokens d'une lettre,
// typique des en-têtes à interlettrage) → mieux vaut le texte OCR du bloc.
func looksDegenerate(_ s: String) -> Bool {
    let toks = s.split(separator: " ")
    guard toks.count >= 4 else { return false }
    let singles = toks.filter { $0.count == 1 }.count
    return Double(singles) / Double(toks.count) > 0.5
}

// Texte d'un bloc en mode fusion : caractères de la couche embarquée du PDF
// dans le rectangle du bloc (repère mediaBox, élargi de 1 pt — les boîtes
// Vision collent aux glyphes, un inset tronquerait les caractères de bord).
// Fallback : texte OCR du bloc (sélection vide ou dégénérée).
func fusedText(_ page: PDFPage?, _ box: NormalizedRect, fallback: String) -> String {
    guard let page else { return fallback }
    let mb = page.bounds(for: .mediaBox)
    let r = box.cgRect
    let rect = CGRect(x: mb.minX + r.minX * mb.width,
                      y: mb.minY + r.minY * mb.height,
                      width: r.width * mb.width,
                      height: r.height * mb.height).insetBy(dx: -1, dy: -1)
    guard let s = page.selection(for: rect)?.string else { return fallback }
    let joined = joinLines(s.components(separatedBy: .newlines)
        .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) })
    return (joined.isEmpty || looksDegenerate(joined)) ? fallback : joined
}

func mdTable(_ table: DocumentObservation.Container.Table, fuse page: PDFPage?) -> String {
    let nCols = table.columns.count
    guard nCols > 0 else { return "" }
    var rows: [[String]] = []
    for row in table.rows {
        var cells = Array(repeating: "", count: nCols)
        for cell in row {
            let i = cell.columnRange.lowerBound
            if cells.indices.contains(i) {
                let t = cell.content.text
                cells[i] = fusedText(page, t.boundingRegion.boundingBox, fallback: normalize(t))
                    .replacingOccurrences(of: "|", with: "\\|")
            }
        }
        rows.append(cells)
    }
    guard let header = rows.first else { return "" }
    let sep = Array(repeating: "---", count: header.count)
    return ([header, sep] + rows.dropFirst())
        .map { "| " + $0.joined(separator: " | ") + " |" }
        .joined(separator: "\n")
}

func mdList(_ list: DocumentObservation.Container.List, fuse page: PDFPage?) -> String {
    list.items.compactMap { item -> String? in
        let t = item.content.text
        let text = fusedText(page, t.boundingRegion.boundingBox, fallback: normalize(t))
        guard !text.isEmpty else { return nil }
        let marker = item.markerString.trimmingCharacters(in: .whitespacesAndNewlines)
        var body = text
        if !marker.isEmpty, body.hasPrefix(marker) {
            body = String(body.dropFirst(marker.count))
                .trimmingCharacters(in: CharacterSet(charactersIn: ".．、)）:： \t"))
        }
        switch item.markerType {
        case .decimal, .decorativeDecimal, .compositeDecimal:
            let n = marker.prefix { $0.isNumber }
            return "\(n.isEmpty ? "1" : String(n)). \(body)"
        default:
            return "- \(body)"
        }
    }.joined(separator: "\n")
}

func pageMarkdown(_ document: DocumentObservation.Container, fuse page: PDFPage?) -> String {
    let tables = document.tables.map { Block(kind: .table, text: mdTable($0, fuse: page), box: $0.boundingRegion.boundingBox) }
        .filter { !$0.text.isEmpty }
    let lists = document.lists.map { Block(kind: .list, text: mdList($0, fuse: page), box: $0.boundingRegion.boundingBox) }
        .filter { !$0.text.isEmpty }
        .filter { l in !tables.contains { l.box.isMostlyInside($0.box) } }
    let structured = tables + lists
    let paras = document.paragraphs.map { Block(kind: .paragraph, text: fusedText(page, $0.boundingRegion.boundingBox, fallback: normalize($0)), box: $0.boundingRegion.boundingBox) }
        .filter { !$0.text.isEmpty }
        .filter { p in !structured.contains { p.box.isMostlyInside($0.box) } }
    let blocks = (paras + structured).sorted { a, b in
        let ra = a.box.cgRect, rb = b.box.cgRect
        let overlap = min(ra.maxY, rb.maxY) - max(ra.minY, rb.minY)
        let shorter = min(ra.height, rb.height)
        if shorter > 0, overlap / shorter >= 0.5 { return ra.minX < rb.minX }
        return ra.maxY > rb.maxY
    }
    return blocks.map(\.text).joined(separator: "\n\n")
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

// (markdown, confiance moyenne des lignes, nb lignes reconnues)
// `fusePage` non-nil → mode fusion : caractères de la couche texte de cette page.
func ocrImage(_ cg: CGImage, fusePage: PDFPage? = nil) async -> (String, Double, Int) {
    var request = RecognizeDocumentsRequest()
    request.textRecognitionOptions.automaticallyDetectLanguage = true
    request.textRecognitionOptions.useLanguageCorrection = true
    request.textRecognitionOptions.maximumCandidateCount = 1
    guard let observations = try? await request.perform(on: cg),
          let document = observations.first?.document else { return ("", 0.0, 0) }
    var conf = 0.0; var n = 0
    for line in document.text.lines {
        if let c = line.topCandidates(1).first { conf += Double(c.confidence); n += 1 }
    }
    return (pageMarkdown(document, fuse: fusePage), n > 0 ? conf / Double(n) : 0.0, n)
}

@main
struct Main {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count > 1 else { exit(1) }
        let url = URL(fileURLWithPath: args[1])
        let fuse = args.contains("--fuse")
        let maxPages = args.dropFirst(2).compactMap { Int($0) }.first ?? 50

        var parts: [String] = []; var confs: [Double] = []
        var pages = 0; var lineCount = 0
        if url.pathExtension.lowercased() == "pdf" {
            guard let doc = PDFDocument(url: url) else { exit(2) }
            for i in 0..<min(doc.pageCount, maxPages) {
                if let p = doc.page(at: i), let cg = render(p, scale: 2.0) {
                    // Fusion seulement si la page a une vraie couche texte.
                    let layer = (p.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    let (t, c, n) = await ocrImage(cg, fusePage: (fuse && layer.count >= 20) ? p : nil)
                    if !t.isEmpty { parts.append(t); if n > 0 { confs.append(c) }; lineCount += n }
                    pages += 1
                }
            }
        } else if let img = NSImage(contentsOf: url),
                  let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
            let (t, c, n) = await ocrImage(cg)
            if !t.isEmpty { parts.append(t) }
            if n > 0 { confs.append(c) }
            lineCount = n; pages = 1
        }
        let text = parts.joined(separator: "\n\n")
        let conf = confs.isEmpty ? 0.0 : confs.reduce(0, +) / Double(confs.count)
        let obj: [String: Any] = ["text": text, "confidence": conf,
                                  "pages": pages, "lines": lineCount]
        let data = try! JSONSerialization.data(withJSONObject: obj)
        FileHandle.standardOutput.write(data)
    }
}
