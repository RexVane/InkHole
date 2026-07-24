package lan

import (
	"bytes"
	"context"
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
)

func buildSampleFolder(t *testing.T) (string, map[string][]byte) {
	t.Helper()
	root := filepath.Join(t.TempDir(), "样本目录")
	files := map[string][]byte{
		"顶层说明.txt":       []byte("hello folder"),
		"子目录/文件A.txt":    []byte("内容A"),
		"子目录/深层/数据B.bin": bytes.Repeat([]byte{0xAB, 0x01}, 60_000),
	}
	for relative, payload := range files {
		full := filepath.Join(root, filepath.FromSlash(relative))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, payload, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.MkdirAll(filepath.Join(root, "空目录"), 0o755); err != nil {
		t.Fatal(err)
	}
	return root, files
}

func assertFolderDelivered(t *testing.T, h *transferHarness,
	files map[string][]byte) {
	t.Helper()
	delivered := waitReceived(t, h)
	info, err := os.Stat(delivered)
	if err != nil || !info.IsDir() {
		t.Fatalf("delivered folder missing: %v", err)
	}
	for relative, want := range files {
		got, err := os.ReadFile(filepath.Join(delivered, filepath.FromSlash(relative)))
		if err != nil {
			t.Fatalf("missing %s: %v", relative, err)
		}
		if !bytes.Equal(got, want) {
			t.Fatalf("content mismatch for %s", relative)
		}
	}
	if info, err := os.Stat(filepath.Join(delivered, "空目录")); err != nil || !info.IsDir() {
		t.Fatal("empty directory was not recreated")
	}
}

func TestFolderSendReceive(t *testing.T) {
	h := startTransferHarness(t, "")
	root, files := buildSampleFolder(t)
	if err := SendFolder(context.Background(), h.target(), root,
		h.senderConfig("")); err != nil {
		t.Fatalf("SendFolder: %v", err)
	}
	assertFolderDelivered(t, h, files)
}

func TestFolderSendReceiveEncrypted(t *testing.T) {
	h := startTransferHarness(t, "目录口令")
	root, files := buildSampleFolder(t)
	if err := SendFolder(context.Background(), h.target(), root,
		h.senderConfig("目录口令")); err != nil {
		t.Fatalf("SendFolder encrypted: %v", err)
	}
	assertFolderDelivered(t, h, files)
}

func TestScanFolderRejectsSymlink(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "real.txt"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "real.txt"),
		filepath.Join(root, "link.txt")); err != nil {
		t.Skip("symlinks unavailable")
	}
	if _, err := ScanFolder(root); err == nil {
		t.Fatal("symlink accepted")
	}
}

func TestExtractRejectsHostileStreams(t *testing.T) {
	build := func(entries ...[]byte) []byte {
		var out []byte
		out = append(out, folderMagic...)
		out = binary.BigEndian.AppendUint32(out, uint32(len(entries)))
		for _, entry := range entries {
			out = append(out, entry...)
		}
		return out
	}
	entry := func(kind byte, path string, size uint64, body []byte) []byte {
		var out []byte
		out = append(out, kind)
		out = binary.BigEndian.AppendUint32(out, uint32(len(path)))
		out = binary.BigEndian.AppendUint64(out, size)
		out = binary.BigEndian.AppendUint64(out, 0)
		out = append(out, path...)
		return append(out, body...)
	}
	cases := map[string][]byte{
		"traversal":      build(entry(1, "../escape.txt", 1, []byte("x"))),
		"absolute":       build(entry(1, "/etc/oops", 1, []byte("x"))),
		"backslash":      build(entry(1, `a\b.txt`, 1, []byte("x"))),
		"dir-with-size":  build(entry(0, "dir", 3, []byte("abc"))),
		"dup-collision":  build(entry(1, "A.txt", 1, []byte("x")), entry(1, "a.txt", 1, []byte("y"))),
		"reserved-name":  build(entry(1, "CON.txt", 1, []byte("x"))),
		"trailing-bytes": append(build(entry(1, "ok.txt", 1, []byte("x"))), 0xFF),
	}
	for name, stream := range cases {
		staging := t.TempDir()
		if err := extractFolderStream(bytes.NewReader(stream), staging); err == nil {
			t.Fatalf("hostile stream %q accepted", name)
		}
	}
}

func TestFolderPayloadSizeMatchesScan(t *testing.T) {
	root, _ := buildSampleFolder(t)
	manifest, err := ScanFolder(root)
	if err != nil {
		t.Fatal(err)
	}
	reader := NewFolderPayloadReader(manifest)
	defer reader.Close()
	var total int64
	buffer := make([]byte, 4096)
	for {
		n, err := reader.Read(buffer)
		total += int64(n)
		if err != nil {
			break
		}
	}
	if total != manifest.PlainSize {
		t.Fatalf("stream size %d != scanned %d", total, manifest.PlainSize)
	}
}
