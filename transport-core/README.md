# InkHole transport core

This Go module owns the transports that must behave identically on desktop and
Android: one-time Magic Wormhole sessions, SSH reverse forwarding, PAKE pairing,
Noise encryption, and yamux stream multiplexing.

The existing WHPP/WHF1 implementation is intentionally not duplicated here.
Every connected transport is exposed as a loopback TCP endpoint, so the Python
and Kotlin applications continue to send and receive the same protocol bytes.

The desktop application starts `inkhole-core` as a sidecar and exchanges JSON
objects over stdin/stdout. Android binds the `mobile` package with `gomobile` and
uses the same JSON request dispatcher in-process.

## Security boundaries

- Magic Wormhole uses AppID `com.rexvane.inkhole/transport-v1` and exposes a
  raw encrypted session. WHPP/WHF1 remains the file protocol. Desktop passes
  its detected HTTP proxy; Android forwards the active system HTTP proxy when
  one is configured.
- SSH device pairing uses SPAKE2 under
  `com.rexvane.inkhole/ssh-pair-v1`, then pins Noise static keys.
- Loopback endpoints require per-session `IKAT` capability tokens. Traffic
  forwarded into the application node carries its runtime-only `IKCI` token.
- SSH reverse forwards bind only to the VPS loopback interface.

## Build

Desktop sidecar:

```bash
make build-desktop
```

Android AAR requires Go 1.25+, Java 17, Android SDK 34, and NDK
`27.2.12479018`:

```bash
make init-gomobile
ANDROID_HOME=/path/to/android-sdk \
ANDROID_NDK_HOME=/path/to/android-sdk/ndk/27.2.12479018 \
make build-android
```

The AAR is written to `android/app/libs/transportcore.aar`. It is a build
artifact and is generated locally or in CI rather than committed.

Run the core, race, pairing, and local-fork rendezvous tests with:

```bash
make test
```

The generated AAR contains `armeabi-v7a`, `arm64-v8a`, `x86`, and `x86_64`
native libraries. `transportcore.aar` is intentionally ignored by Git and is
rebuilt locally or in CI.
