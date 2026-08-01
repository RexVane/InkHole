# InkHole 2.0 architecture

InkHole 2.0 has one transport implementation. The core is Rust 1.93 with
Tokio for asynchronous execution, Quinn for authenticated QUIC streams, and
BLAKE3 for transfer and resume integrity. LAN discovery, direct transfers,
folder manifests, one-time Magic Wormhole sessions, and SSH relay sessions
all use the same `inkhole-core` service.

## Hosts

```text
Tauri 2 desktop UI  ──┐
                       ├── JsonService (Tokio) ── QUIC / LAN / Wormhole / SSH
Flutter mobile UI ────┘
                       └── inkhole-ffi C ABI
```

The desktop host calls `JsonService` directly from the Tauri runtime. The
mobile host loads `inkhole-ffi` and keeps one native service handle in a
dedicated Dart isolate. Requests are serialized in that isolate and native
events are polled without blocking Flutter's frame scheduler. The FFI ABI is
versioned independently from the JSON protocol; strings returned by native
code are released through `inkhole_string_free`.

## Mobile build

Flutter sources live in `mobile/`. The generated Android/iOS host projects are
not allowed to carry transport logic. Build Rust libraries first, then let
Flutter package them:

```text
mobile/native/android/arm64-v8a/libinkhole_ffi.so
mobile/native/android/armeabi-v7a/libinkhole_ffi.so
mobile/native/android/x86_64/libinkhole_ffi.so
mobile/native/ios/InkHoleCore.xcframework
```

`mobile/tool/build_native.ps1` and `mobile/tool/build_native.sh` build the
Android libraries with `cargo-ndk`. `mobile/tool/bootstrap_flutter.ps1`
generates the Xcode Runner project on a machine with Flutter installed. The
iOS `Podfile` mounts the generated `InkHoleCore.xcframework` through the local
`InkHoleCore.podspec` and force-loads the static Rust symbols used by Dart FFI.

## Lifetime and cancellation

`JsonService` owns LAN, short-code, and SSH sessions. Every transfer gets a
`CancellationToken`; `lan.send.cancel` and `session.cancel` stop work without
discarding resumable checkpoints. The FFI worker closes the service before
destroying its pointer, and the Flutter state stops the LAN session before the
isolate exits.

## Security boundary

Device identities and transfer secrets are not sent through the UI bridge as
long-lived state. The mobile UI stores both the Rust identity export and the
optional LAN secret in platform secure storage. Every
discovered peer is verified by its signed challenge and pinned QUIC
certificate; SSH relay setup requires a confirmed host-key fingerprint.
