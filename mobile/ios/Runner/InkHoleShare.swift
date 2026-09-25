import Flutter
import UIKit

/// 与 Dart `_shareChannel`(com.rexvane.inkhole/share)对应的 iOS 实现。
///
/// Android 侧从 Intent.EXTRA_STREAM 取分享文件；iOS 由 Share Extension 落盘到
/// App Group 共享目录，主 App 启动时来取。当前工程还没有 Share Extension，
/// 因此这里给出完整的消费端实现：只要共享目录里出现文件就能被 Dart 侧读到。
/// 无共享目录时返回空列表，不报错。
final class InkHoleShare {

  /// App Group ID，与将来的 Share Extension 保持一致。
  static let appGroupId = "group.com.rexvane.inkhole"
  private static let shareFolderName = "inkhole-share"

  /// 取走待处理的分享文件；取走即清空(consume 语义)。
  static func consumeSharedFiles() -> [String] {
    guard let directory = shareDirectory() else { return [] }
    let fileManager = FileManager.default
    guard let entries = try? fileManager.contentsOfDirectory(
      at: directory,
      includingPropertiesForKeys: [.isRegularFileKey],
      options: [.skipsHiddenFiles]
    ) else {
      return []
    }
    var paths: [String] = []
    for entry in entries {
      let isFile = (try? entry.resourceValues(forKeys: [.isRegularFileKey]))?
        .isRegularFile ?? false
      guard isFile else { continue }
      // 搬到 Documents 下让收件流程统一处理，源文件删除避免重复入队。
      let destination = uniqueURL(in: documentsDirectory(), name: entry.lastPathComponent)
      do {
        try fileManager.moveItem(at: entry, to: destination)
        paths.append(destination.path)
      } catch {
        paths.append(entry.path)
      }
    }
    return paths
  }

  /// 分享失败提示；没有 Share Extension 时永远为空。
  static func consumeShareErrors() -> [String] {
    return []
  }

  private static func shareDirectory() -> URL? {
    guard let container = FileManager.default.containerURL(
      forSecurityApplicationGroupIdentifier: appGroupId
    ) else {
      return nil
    }
    let directory = container.appendingPathComponent(shareFolderName)
    if !FileManager.default.fileExists(atPath: directory.path) {
      try? FileManager.default.createDirectory(
        at: directory,
        withIntermediateDirectories: true
      )
    }
    return directory
  }

  private static func documentsDirectory() -> URL {
    (try? FileManager.default.url(
      for: .documentDirectory,
      in: .userDomainMask,
      appropriateFor: nil,
      create: true
    )) ?? URL(fileURLWithPath: NSTemporaryDirectory())
  }

  private static func uniqueURL(in directory: URL, name: String) -> URL {
    let fileManager = FileManager.default
    var candidate = directory.appendingPathComponent(name)
    let stem = (name as NSString).deletingPathExtension
    let ext = (name as NSString).pathExtension
    var index = 2
    while fileManager.fileExists(atPath: candidate.path) {
      let next = ext.isEmpty ? "\(stem) (\(index))" : "\(stem) (\(index)).\(ext)"
      candidate = directory.appendingPathComponent(next)
      index += 1
    }
    return candidate
  }
}
