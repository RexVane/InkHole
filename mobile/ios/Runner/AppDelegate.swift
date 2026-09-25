import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate {

  private static let shareChannelName = "com.rexvane.inkhole/share"
  private static let exporterChannelName = "com.rexvane.inkhole/exporter"
  private static let scannerChannelName = "com.rexvane.inkhole/scanner"
  private static let updaterChannelName = "com.rexvane.inkhole/updater"

  private var scanner: InkHoleScanner?
  private let updaterQueue = DispatchQueue(label: "com.rexvane.inkhole.updater")
  private let exportQueue = DispatchQueue(label: "com.rexvane.inkhole.exporter")

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    GeneratedPluginRegistrant.register(with: self)
    if let controller = window?.rootViewController as? FlutterViewController {
      registerChannels(with: controller)
    }
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  private func registerChannels(with controller: FlutterViewController) {
    let messenger = controller.binaryMessenger

    // share:消费 Share Extension 落盘的分享文件。
    FlutterMethodChannel(
      name: Self.shareChannelName,
      binaryMessenger: messenger
    ).setMethodCallHandler { call, result in
      switch call.method {
      case "consumeSharedFiles":
        result(InkHoleShare.consumeSharedFiles())
      case "consumeShareErrors":
        result(InkHoleShare.consumeShareErrors())
      default:
        result(FlutterMethodNotImplemented)
      }
    }

    // exporter:收件导出、打开、目录描述。
    FlutterMethodChannel(
      name: Self.exporterChannelName,
      binaryMessenger: messenger
    ).setMethodCallHandler { [weak self] call, result in
      guard let self else {
        result(FlutterMethodNotImplemented)
        return
      }
      let arguments = call.arguments as? [String: Any] ?? [:]
      switch call.method {
      case "export":
        let path = arguments["path"] as? String ?? ""
        let treeUri = arguments["treeUri"] as? String
        self.exportQueue.async {
          let outcome = InkHoleExporter.export(path: path, treeUri: treeUri)
          DispatchQueue.main.async { result(outcome) }
        }
      case "pickDirectory":
        result(InkHoleExporter.pickDirectory())
      case "open":
        let path = arguments["path"] as? String ?? ""
        let name = arguments["name"] as? String ?? ""
        let treeUri = arguments["treeUri"] as? String
        DispatchQueue.main.async {
          result(InkHoleExporter.open(path: path, name: name, treeUri: treeUri))
        }
      case "downloadsPath":
        result(InkHoleExporter.downloadsPath())
      case "describeTree":
        let uri = arguments["uri"] as? String ?? ""
        result(InkHoleExporter.describeTree(uri: uri))
      default:
        result(FlutterMethodNotImplemented)
      }
    }

    // scanner:相册识码(实时取景由 mobile_scanner 插件负责)。
    let scannerHandler = InkHoleScanner(registrar: registrar(forPlugin: "InkHoleScanner")!)
    scanner = scannerHandler
    FlutterMethodChannel(
      name: Self.scannerChannelName,
      binaryMessenger: messenger
    ).setMethodCallHandler { call, result in
      switch call.method {
      case "scanImage":
        scannerHandler.scanImage(result: result)
      case "scan":
        scannerHandler.scan(result: result)
      default:
        result(FlutterMethodNotImplemented)
      }
    }

    // updater:iOS 不自升级，仅做版本检查。
    FlutterMethodChannel(
      name: Self.updaterChannelName,
      binaryMessenger: messenger
    ).setMethodCallHandler { [weak self] call, result in
      guard let self else {
        result(FlutterMethodNotImplemented)
        return
      }
      switch call.method {
      case "check":
        let current = (call.arguments as? [String: Any])?["current"] as? String ?? ""
        self.updaterQueue.async {
          do {
            let info = try InkHoleUpdater.check(current: current)
            DispatchQueue.main.async { result(info) }
          } catch {
            DispatchQueue.main.async {
              result(FlutterError(
                code: "check_failed",
                message: error.localizedDescription,
                details: nil
              ))
            }
          }
        }
      case "downloadInstall":
        result(FlutterError(
          code: "unsupported",
          message: "iOS 不支持应用内安装更新，请前往发布页面下载",
          details: nil
        ))
      default:
        result(FlutterMethodNotImplemented)
      }
    }
  }
}
