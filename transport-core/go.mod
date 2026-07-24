module github.com/rexvane/inkhole/transport-core

go 1.25.0

replace github.com/psanford/wormhole-william => ./third_party/wormhole-william

require (
	github.com/flynn/noise v1.1.0
	github.com/hashicorp/yamux v0.1.2
	github.com/psanford/wormhole-william v1.0.8
	golang.org/x/crypto v0.54.0
	salsa.debian.org/vasudev/gospake2 v0.0.0-20210510093858-d91629950ad1
)

require (
	github.com/klauspost/compress v1.17.11 // indirect
	github.com/pion/dtls/v3 v3.1.4 // indirect
	github.com/pion/logging v0.2.4 // indirect
	github.com/pion/stun/v3 v3.1.6 // indirect
	github.com/pion/transport/v4 v4.0.2 // indirect
	github.com/quic-go/quic-go v0.60.0 // indirect
	github.com/wlynxg/anet v0.0.5 // indirect
	golang.org/x/mobile v0.0.0-20260709172247-6129f5bee9d5 // indirect
	golang.org/x/mod v0.38.0 // indirect
	golang.org/x/net v0.57.0 // indirect
	golang.org/x/sync v0.22.0 // indirect
	golang.org/x/sys v0.47.0 // indirect
	golang.org/x/tools v0.48.0 // indirect
	nhooyr.io/websocket v1.8.17 // indirect
)

tool golang.org/x/mobile/cmd/gobind
