# Third-party notices

InkHole includes and vendors open-source dependencies. Their copyright and license terms remain with their respective authors.

## wormhole-william

`wormhole-william` is the upstream project name, not an InkHole product name.

- Upstream: `github.com/psanford/wormhole-william`
- Copyright: Peter Sanford and contributors
- License: MIT
- Local source and license: `transport-core/third_party/wormhole-william/`

InkHole carries a local fork for raw encrypted tunnel sessions, proxy support, and connection reliability changes. InkHole does not use the dependency's ZIP/file-transfer workflow; it runs WHPP/WHF1 over the tunnel.

## Vendored Go modules

License files for the Go modules shipped with the shared transport core are retained beside their source under `transport-core/vendor/`. This includes Noise, yamux, gospake2, `golang.org/x/*`, websocket, and compression dependencies.

## Android QR encoding

The Android client uses ZXing Core (`com.google.zxing:core`) under the Apache License 2.0 to render pairing QR codes.
