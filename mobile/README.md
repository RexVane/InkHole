# InkHole Flutter client

The mobile UI is Flutter 3 + Dart. All transport, discovery, short-code and
SSH relay operations run in the Rust `inkhole-core` crate through the stable C
ABI in `inkhole-ffi`.

## Native library

Build one shared library per target ABI and place it under
`mobile/native/<platform>/<abi>/` before running Flutter:

```text
native/android/arm64-v8a/libinkhole_ffi.so
native/android/armeabi-v7a/libinkhole_ffi.so
native/android/x86_64/libinkhole_ffi.so
native/ios/InkHoleCore.xcframework
```

The Android Gradle project copies `.so` files from this directory. iOS links
the XCFramework from the Runner target. The repository intentionally does not
check in generated binaries.

From a machine with Flutter and `cargo-ndk` installed:

```bash
flutter pub get
bash tool/build_native.sh
flutter build apk --release
```

On macOS, run `bash tool/build_native_ios.sh` and add the generated
`native/ios/InkHoleCore.xcframework` is linked automatically by the checked-in
`ios/Podfile` and `native/ios/InkHoleCore.podspec` before
`flutter build ios --release --no-codesign`.

Android starts a `connectedDevice` foreground service and holds a Wi-Fi
multicast lock while the app is running, so discovery continues across screen
lock. Android `SEND` and `SEND_MULTIPLE` intents are copied to an app-private
cache and are sent through the same existing target-device flow.

The Dart bridge keeps a native service handle in a dedicated isolate. Calls
are serialized there and Rust events are exposed as a stream, so a large file
or SSH reconnect never blocks the Flutter frame scheduler.
