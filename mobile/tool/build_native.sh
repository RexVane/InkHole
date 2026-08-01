#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
out="$repo/mobile/native/android"
command -v cargo-ndk >/dev/null || {
  echo "cargo-ndk is required: cargo install cargo-ndk" >&2
  exit 1
}
command -v rustup >/dev/null || {
  echo "rustup is required to install Android Rust targets" >&2
  exit 1
}
rustup target add aarch64-linux-android armv7-linux-androideabi x86_64-linux-android
mkdir -p "$out"
cargo ndk -t arm64-v8a -t armeabi-v7a -t x86_64 -o "$out" \
  --manifest-path "$repo/rust/Cargo.toml" build --release -p inkhole-ffi
