#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
manifest="$repo/rust/Cargo.toml"
out="$repo/mobile/native/ios"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

command -v xcodebuild >/dev/null || {
  echo "xcodebuild is required to package the iOS XCFramework" >&2
  exit 1
}
command -v lipo >/dev/null || {
  echo "lipo is required to merge iOS simulator architectures" >&2
  exit 1
}
mkdir -p "$out"

for target in aarch64-apple-ios aarch64-apple-ios-sim x86_64-apple-ios; do
  rustup target add "$target"
  cargo build --manifest-path "$manifest" -p inkhole-ffi --release --target "$target"
done

rm -rf "$out/InkHoleCore.xcframework"
lipo -create \
  "$repo/rust/target/aarch64-apple-ios-sim/release/libinkhole_ffi.a" \
  "$repo/rust/target/x86_64-apple-ios/release/libinkhole_ffi.a" \
  -output "$tmp/libinkhole_ffi_sim.a"

xcodebuild -create-xcframework \
  -library "$repo/rust/target/aarch64-apple-ios/release/libinkhole_ffi.a" \
  -headers "$repo/rust/crates/inkhole-ffi/include" \
  -library "$tmp/libinkhole_ffi_sim.a" \
  -headers "$repo/rust/crates/inkhole-ffi/include" \
  -output "$out/InkHoleCore.xcframework"
