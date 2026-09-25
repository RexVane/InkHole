import Flutter
import UIKit
import Vision

/// 与 Dart `ScannerChannel` 对应的 iOS 实现。
///
/// 实时取景由 `mobile_scanner` 插件在 Dart 侧完成，本通道只负责
/// "从相册选图识码"(`scanImage`)。用 Vision 的 VNDetectBarcodesRequest，
/// 只认 QR 码，与 Dart 侧 `parseScannedCode` 的输入口径一致。
final class InkHoleScanner: NSObject {

  private weak var registrar: FlutterPluginRegistrar?
  private var pendingResult: FlutterResult?
  private var pendingPicker: UIImagePickerController?

  init(registrar: FlutterPluginRegistrar) {
    self.registrar = registrar
    super.init()
  }

  /// 拉起相册选择器。用户取消 → nil；图里没码 → FlutterError('scan_not_found')。
  func scanImage(result: @escaping FlutterResult) {
    if pendingResult != nil {
      result(FlutterError(code: "busy", message: "扫码进行中", details: nil))
      return
    }
    guard let controller = Self.topViewController() else {
      result(FlutterError(code: "scan_unavailable", message: "无法打开相册", details: nil))
      return
    }
    guard UIImagePickerController.isSourceTypeAvailable(.photoLibrary) else {
      result(FlutterError(code: "scan_unavailable", message: "相册不可用", details: nil))
      return
    }
    pendingResult = result
    let picker = UIImagePickerController()
    picker.sourceType = .photoLibrary
    picker.mediaTypes = ["public.image"]
    picker.delegate = self
    pendingPicker = picker
    controller.present(picker, animated: true)
  }

  /// 实时扫码在 iOS 由 mobile_scanner 完成；此方法仅为通道对齐保留。
  func scan(result: @escaping FlutterResult) {
    result(FlutterError(
      code: "scan_unsupported",
      message: "iOS 实时扫码由内置取景器处理",
      details: nil
    ))
  }

  private func finish(_ result: @escaping FlutterResult) {
    pendingResult = nil
    pendingPicker = nil
    result(nil)
  }

  private func fail(code: String, message: String) {
    let result = pendingResult
    pendingResult = nil
    pendingPicker = nil
    result?(FlutterError(code: code, message: message, details: nil))
  }

  /// 用 Vision 从图里解一个 QR 码。
  private func decode(image: UIImage) -> String? {
    guard let cgImage = image.cgImage else { return nil }
    let request = VNDetectBarcodesRequest()
    request.symbologies = [.qr]
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
      try handler.perform([request])
    } catch {
      return nil
    }
    guard let observations = request.results else { return nil }
    for observation in observations {
      if let payload = observation.payloadStringValue, !payload.isEmpty {
        return payload
      }
    }
    return nil
  }

  static func topViewController() -> UIViewController? {
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

extension InkHoleScanner: UIImagePickerControllerDelegate, UINavigationControllerDelegate {

  func imagePickerController(
    _ picker: UIImagePickerController,
    didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]
  ) {
    picker.dismiss(animated: true)
    guard let image = info[.originalImage] as? UIImage else {
      fail(code: "scan_not_found", message: "图片里没有识别到二维码")
      return
    }
    // Vision 解码放后台线程，避免大图阻塞主线程。
    DispatchQueue.global(qos: .userInitiated).async { [weak self] in
      let decoded = self?.decode(image: image)
      DispatchQueue.main.async {
        guard let self = self, let result = self.pendingResult else { return }
        guard let text = decoded, !text.isEmpty else {
          self.fail(code: "scan_not_found", message: "图片里没有识别到二维码")
          return
        }
        self.pendingResult = nil
        self.pendingPicker = nil
        result(text)
      }
    }
  }

  func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
    picker.dismiss(animated: true)
    if let result = pendingResult {
      finish(result)
    }
  }
}
