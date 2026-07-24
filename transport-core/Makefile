.PHONY: test build-desktop build-android init-gomobile vendor

GOMOBILE_VERSION := v0.0.0-20260709172247-6129f5bee9d5
CORE_NAME := inkhole-core$(if $(findstring Windows_NT,$(OS)),.exe,)

test:
	go test -race ./...
	cd third_party/wormhole-william && go test ./wormhole ./rendezvous

build-desktop:
	mkdir -p bin
	go build -trimpath -ldflags="-s -w" -o bin/$(CORE_NAME) ./cmd/inkhole-core

init-gomobile:
	go install golang.org/x/mobile/cmd/gomobile@$(GOMOBILE_VERSION)
	go install golang.org/x/mobile/cmd/gobind@$(GOMOBILE_VERSION)
	$(shell go env GOPATH)/bin/gomobile init

build-android:
	mkdir -p ../android/app/libs
	# -checklinkname=0: quic-go 的 Android 网卡枚举依赖(wlynxg/anet)使用
	# go:linkname 引用标准库内部符号,Go 1.23+ 默认拒绝链接,需显式放行
	GOFLAGS=-mod=mod PATH="$$PATH:$(shell go env GOPATH)/bin" \
		$(shell go env GOPATH)/bin/gomobile bind -target=android -androidapi 24 \
		-ldflags=-checklinkname=0 \
		-o ../android/app/libs/transportcore.aar ./mobile

vendor:
	go mod vendor
