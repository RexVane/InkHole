.PHONY: test test-rust test-flutter fmt lint build-desktop build-android clean

RUST_DIR := rust
MOBILE_DIR := mobile

test: test-rust test-flutter

test-rust:
	cargo test --workspace --manifest-path $(RUST_DIR)/Cargo.toml

test-flutter:
	cd $(MOBILE_DIR) && flutter test

fmt:
	cargo fmt --all --manifest-path $(RUST_DIR)/Cargo.toml

lint:
	cargo clippy --workspace --all-targets --manifest-path $(RUST_DIR)/Cargo.toml -- -D warnings
	cd $(MOBILE_DIR) && flutter analyze

build-desktop:
	cd $(RUST_DIR)/apps/inkhole-desktop && cargo tauri build

build-android:
	cd $(MOBILE_DIR) && bash tool/build_native.sh
	cd $(MOBILE_DIR) && flutter build apk --release

clean:
	cargo clean --manifest-path $(RUST_DIR)/Cargo.toml
	if [ -d $(MOBILE_DIR) ]; then cd $(MOBILE_DIR) && flutter clean; fi
