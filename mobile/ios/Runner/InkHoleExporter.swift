import Flutter
import UIKit
import UniformTypeIdentifiers

/// 与 Dart `ExporterChannel` 对应的 iOS 实现。
///
/// Android 侧的 Exporter.kt 走 MediaStore/SAF，iOS 没有对等概念：应用私有
/// 收件箱对用户可见依赖 Info.plist 里的 `UIFileSharingEnabled` +
/// `LSSupportsOpeningDocumentsInPlace`(已在 Info.plist 打开)，用户可从
/// "文件"App 的"我的 iPhone → Inkhole Mobile"直接取件。
///
/// 因此 iOS 的"导出"语义是:把私有收件箱里的成品复制/移动到应用 Documents
/// 根目录(即文件 App 可见处)，已在该目录内则原样返回成功。
enum InkHoleExporter {

  /// 导出到应用 Documents 根目录(文件 App 可见)。
  static func export(path: String, treeUri: String?) -> [String: String] {
    let fileManager = FileManager.default
    let source = URL(fileURLWithPath: path)
    guard fileManager.fileExists(atPath: source.path) else {
      return ["name": source.lastPathComponent, "location": ""]
    }
    guard let documents = try? documentsDirectory() else {
      return ["name": source.lastPathComponent, "location": ""]
    }
    // 已经在 Documents 里了(Dart 侧直接落盘的情形):无需搬动。
    if source.standardizedFileURL.path.hasPrefix(documents.standardizedFileURL.path + "/") {
      let relative = relativeLocation(for: source, in: documents)
      return ["name": source.lastPathComponent, "location": relative]
    }
    let destination = uniqueURL(in: documents, name: source.lastPathComponent)
    do {
      try fileManager.moveItem(at: source, to: destination)
      return [
        "name": destination.lastPathComponent,
        "location": "文件/Inkhole Mobile",
      ]
    } catch {
      // 跨卷移动失败时退回复制，源文件保留(绝不丢数据)。
      do {
        try fileManager.copyItem(at: source, to: destination)
        return [
          "name": destination.lastPathComponent,
          "location": "文件/Inkhole Mobile",
        ]
      } catch {
        return ["name": source.lastPathComponent, "location": ""]
      }
    }
  }

  /// iOS 没有 SAF 目录选择器，返回 nil 让 Dart 侧按"未选择"处理。
  static func pickDirectory() -> [String: String]? {
    return nil
  }

  /// "打开"一条收件记录:返回 exact 表示直接用系统应用打开，否则回退文件 App。
  static func open(path: String, name: String, treeUri: String?) -> String {
    let fileManager = FileManager.default
    var candidates: [URL] = []
    if !path.isEmpty, fileManager.fileExists(atPath: path) {
      candidates.append(URL(fileURLWithPath: path))
    }
    if !name.isEmpty, let documents = try? documentsDirectory() {
      candidates.append(documents.appendingPathComponent(name))
    }
    for url in candidates where fileManager.fileExists(atPath: url.path) {
      if present(url) { return "exact" }
    }
    return "downloads"
  }

  /// 收件落点(Documents 目录)的绝对路径。
  static func downloadsPath() -> String? {
    guard let documents = try? documentsDirectory() else { return nil }
    return documents.path
  }

  /// iOS 没有 SAF 树 URI，只有自定义 scheme 时才尝试解析，其余原样返回。
  static func describeTree(uri: String) -> String? {
    if uri.isEmpty { return nil }
    if uri.hasPrefix("file://") {
      return URL(string: uri)?.path ?? uri
    }
    return uri
  }

  // MARK: - Helpers

  private static func documentsDirectory() throws -> URL {
    try FileManager.default.url(
      for: .documentDirectory,
      in: .userDomainMask,
      appropriateFor: nil,
      create: false
    )
  }

  private static func relativeLocation(for url: URL, in documents: URL) -> String {
    let base = documents.standardizedFileURL.path
    let full = url.standardizedFileURL.path
    guard full.hasPrefix(base) else { return "文件/Inkhole Mobile" }
    let relative = String(full.dropFirst(base.count))
      .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    return relative.isEmpty ? "文件/Inkhole Mobile" : "文件/Inkhole Mobile/\(relative)"
  }

  /// 同名文件加 ` (n)` 后缀，避免覆盖用户已有文件。
  private static func uniqueURL(in directory: URL, name: String) -> URL {
    let fileManager = FileManager.default
    let stem = (name as NSString).deletingPathExtension
    let ext = (name as NSString).pathExtension
    var candidate = directory.appendingPathComponent(name)
    var index = 2
    while fileManager.fileExists(atPath: candidate.path) {
      let next = ext.isEmpty ? "\(stem) (\(index))" : "\(stem) (\(index)).\(ext)"
      candidate = directory.appendingPathComponent(next)
      index += 1
    }
    return candidate
  }

  private static func present(_ url: URL) -> Bool {
    guard let controller = topViewController() else { return false }
    let activity = UIActivityViewController(
      activityItems: [url],
      applicationActivities: nil
    )
    activity.popoverPresentationController?.sourceView = controller.view
    activity.popoverPresentationController?.sourceRect = CGRect(
      x: controller.view.bounds.midX,
      y: controller.view.bounds.midY,
      width: 0,
      height: 0
    )
    controller.present(activity, animated: true)
    return true
  }

  private static func topViewController() -> UIViewController? {
    let window = UIApplication.shared.connectedScenes
      .compactMap { $0 as? UIWindowScene }
      .flatMap { $0.windows }
      .first { $0.isKeyWindow }
    var controller = window?.rootViewController
    while let presented = controller?.presentedViewController {
      controller = presented
    }
    return controller
  }
}
