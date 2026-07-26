module github.com/rexvane/inkhole/desktop

go 1.25.0

require (
	github.com/google/uuid v1.6.0
	github.com/rexvane/inkhole/transport-core v0.0.0-00010101000000-000000000000
	github.com/wailsapp/wails/v3 v3.0.0-alpha2.117
)

require (
	github.com/adrg/xdg v0.5.3 // indirect
	github.com/cenkalti/backoff v2.2.1+incompatible // indirect
	github.com/coder/websocket v1.8.14 // indirect
	github.com/flynn/noise v1.1.0 // indirect
	github.com/go-ole/go-ole v1.3.0 // indirect
	github.com/godbus/dbus/v5 v5.2.2 // indirect
	github.com/grandcat/zeroconf v1.0.0 // indirect
	github.com/hashicorp/yamux v0.1.2 // indirect
	github.com/jchv/go-winloader v0.0.0-20250406163304-c1995be93bd1 // indirect
	github.com/klauspost/compress v1.18.3 // indirect
	github.com/mattn/go-colorable v0.1.14 // indirect
	github.com/mattn/go-isatty v0.0.20 // indirect
	github.com/miekg/dns v1.1.27 // indirect
	github.com/pion/dtls/v3 v3.1.4 // indirect
	github.com/pion/logging v0.2.4 // indirect
	github.com/pion/stun/v3 v3.1.6 // indirect
	github.com/pion/transport/v4 v4.0.2 // indirect
	github.com/psanford/wormhole-william v1.0.8 // indirect
	github.com/quic-go/quic-go v0.60.0 // indirect
	github.com/wlynxg/anet v0.0.5 // indirect
	golang.org/x/crypto v0.54.0 // indirect
	golang.org/x/net v0.57.0 // indirect
	golang.org/x/sys v0.47.0 // indirect
	golang.org/x/text v0.40.0 // indirect
	nhooyr.io/websocket v1.8.17 // indirect
	salsa.debian.org/vasudev/gospake2 v0.0.0-20210510093858-d91629950ad1 // indirect
)

replace github.com/rexvane/inkhole/transport-core => ../transport-core

replace github.com/psanford/wormhole-william => ../transport-core/third_party/wormhole-william
