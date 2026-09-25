import Flutter
import UIKit

/// 与 Dart `UpdaterChannel`(com.rexvane.inkhole/updater)对应的 iOS 实现。
///
/// iOS 不允许应用内自升级(App Store 规则)，所以这里刻意不做下载安装：
/// - `check` 返回 GitHub 最新 tag 与是否有新版，但 apkUrl 恒为空串，
///   Dart 侧看到空 URL 会走"跳转 Release 页面"分支，绕过应用内下载。
/// - `downloadInstall` 直接以 `unsupported` 错误拒绝，提示用户去商店/网页。
///
/// 这样 Dart 侧无需按平台分支，语义仍正确。
enum InkHoleUpdater {

  private static let repository = "RexVane/InkHole"
  private static let releaseApi =
    "https://api.github.com/repos/\(repository)/releases/latest"

  /// 查询最新版本。同步阻塞式网络请求，调用方需在工作线程执行。
  static func check(current: String) throws -> [String: Any] {
    guard let url = URL(string: releaseApi) else {
      throw UpdaterError.invalidResponse
    }
    var request = URLRequest(url: url)
    request.timeoutInterval = 20
    request.setValue("InkHole-Updater", forHTTPHeaderField: "User-Agent")
    request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")

    let semaphore = DispatchSemaphore(value: 0)
    // 结果在闭包里写、在闭包外读，用一个盒子承载以避免 Swift 对捕获变量
    // 的并发告警，同时保持语义清晰。
    let box = UpdaterResultBox()

    URLSession.shared.dataTask(with: request) { data, response, error in
      defer { semaphore.signal() }
      if let error = error {
        box.failure = error
        return
      }
      guard let http = response as? HTTPURLResponse,
            (200..<300).contains(http.statusCode),
            let data = data,
            let decoded = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
      else {
        box.failure = UpdaterError.invalidResponse
        return
      }
      box.payload = decoded
    }.resume()
    semaphore.wait()

    if let failure = box.failure { throw failure }
    guard let body = box.payload else { throw UpdaterError.invalidResponse }
    let tag = (body["tag_name"] as? String ?? "").trimmingCharacters(in: .whitespaces)
    guard !tag.isEmpty else { throw UpdaterError.invalidResponse }
    let notes = summarize(body["body"] as? String ?? "")
    return [
      "version": tag,
      // iOS 不自升级：空 URL 让 Dart 走网页跳转分支。
      "apkUrl": "",
      "notes": notes,
      "newer": versionIsNewer(remote: tag, local: current),
    ]
  }

  // MARK: - Helpers

  /// remote 是否比 local 新。与 Dart `versionIsNewer` 保持同一口径。
  static func versionIsNewer(remote: String, local: String) -> Bool {
    let left = parse(remote)
    let right = parse(local)
    let count = max(left.count, right.count)
    for index in 0..<count {
      let x = index < left.count ? left[index] : 0
      let y = index < right.count ? right[index] : 0
      if x != y { return x > y }
    }
    return false
  }

  private static func parse(_ value: String) -> [Int] {
    var trimmed = value.trimmingCharacters(in: .whitespaces)
    if trimmed.hasPrefix("v") || trimmed.hasPrefix("V") {
      trimmed.removeFirst()
    }
    var parts = trimmed.split(separator: ".").map { segment -> Int in
      let digits = segment.filter { $0.isNumber }
      return Int(digits) ?? 0
    }
    if parts.count >= 4 { return parts }
    parts.append(contentsOf: [Int](repeating: 0, count: 4 - parts.count))
    return parts
  }

  /// 与 Dart `_summarize` 对齐：取前 4 条非标题行。
  private static func summarize(_ raw: String) -> String {
    var items: [String] = []
    for rawLine in raw.replacingOccurrences(of: "\r\n", with: "\n").split(separator: "\n") {
      var text = rawLine.trimmingCharacters(in: .whitespaces)
      if text.isEmpty || text.hasPrefix("#") || text.hasPrefix(">") { continue }
      for prefix in ["- ", "* ", "+ "] where text.hasPrefix(prefix) {
        text = String(text.dropFirst(prefix.count))
      }
      if let range = text.range(of: #"^\d+[.)]\s+"#, options: .regularExpression) {
        text.removeSubrange(range)
      }
      text = text.replacingOccurrences(of: "**", with: "")
        .replacingOccurrences(of: "`", with: "")
        .trimmingCharacters(in: .whitespaces)
      if text.isEmpty || text.hasPrefix("http://") || text.hasPrefix("https://") { continue }
      if text.count > 90 {
        let end = text.index(text.startIndex, offsetBy: 89)
        text = text[..<end].trimmingCharacters(in: .whitespaces) + "…"
      }
      items.append("• \(text)")
      if items.count >= 4 { break }
    }
    return items.joined(separator: "\n")
  }

  enum UpdaterError: LocalizedError {
    case invalidResponse

    var errorDescription: String? {
      switch self {
      case .invalidResponse: return "无法获取更新信息"
      }
    }
  }
}

/// 承载一次异步请求的结果。因为读写发生在不同线程但由信号量串行化，
/// 用引用类型避免 Swift 对闭包捕获可变局部变量的限制。
private final class UpdaterResultBox {
  var payload: [String: Any]?
  var failure: Error?
}
