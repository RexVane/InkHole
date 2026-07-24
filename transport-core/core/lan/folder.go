package lan

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
)

// WHF1 streamed folder payload, byte-compatible with p2p.py:
//
//	"WHF1" entry_count(u32 BE), then per entry:
//	  type(1B: 0=dir 1=file) path_len(u32) size(u64) mtime_ms(u64)
//	  path(utf-8, "/"-separated relative)  [file bytes when type=1]
//
// The whole stream travels as one WHPP kind=folder-v1 payload; resume,
// encryption and the SHA-256 receipt all apply to the stream itself.

const (
	folderMagic      = "WHF1"
	maxFolderEntries = 100_000
	maxFolderPath    = 4096
	maxFolderDepth   = 128
	folderEntrySize  = 1 + 4 + 8 + 8
)

var windowsReserved = func() map[string]bool {
	out := map[string]bool{"CON": true, "PRN": true, "AUX": true, "NUL": true}
	for i := 1; i < 10; i++ {
		out[fmt.Sprintf("COM%d", i)] = true
		out[fmt.Sprintf("LPT%d", i)] = true
	}
	return out
}()

var caseFolder = cases.Fold()

// portablePathParts mirrors p2p._portable_path_parts: validate one relative
// WHF1 path and derive its cross-platform collision key.
func portablePathParts(path string) ([]string, string, error) {
	if path == "" || strings.HasPrefix(path, "/") ||
		strings.ContainsAny(path, "\\\x00") {
		return nil, "", errors.New("文件夹内含不安全路径")
	}
	parts := strings.Split(path, "/")
	if len(parts) > maxFolderDepth {
		return nil, "", errors.New("文件夹目录层级过深")
	}
	keys := make([]string, 0, len(parts))
	for _, component := range parts {
		if component == "" || component == "." || component == ".." ||
			len(component) > 255 ||
			strings.TrimRight(component, ". ") != component {
			return nil, "", fmt.Errorf("文件夹内含跨平台不支持的名称：%s", component)
		}
		for _, r := range component {
			if r < 0x20 || strings.ContainsRune(`<>:"|?*`, r) {
				return nil, "", fmt.Errorf("文件夹内含跨平台不支持的名称：%s", component)
			}
		}
		stem, _, _ := strings.Cut(component, ".")
		if windowsReserved[strings.ToUpper(stem)] {
			return nil, "", fmt.Errorf("文件夹内含跨平台不支持的名称：%s", component)
		}
		if !utf8.ValidString(component) {
			return nil, "", errors.New("文件夹路径编码非法")
		}
		keys = append(keys, caseFolder.String(norm.NFC.String(component)))
	}
	return parts, strings.Join(keys, "/"), nil
}

// FolderEntry is one manifest row.
type FolderEntry struct {
	Relative  string
	PathBytes []byte
	Source    string
	IsDir     bool
	Size      int64
	MtimeMS   int64
}

// FolderManifest captures one scan of a folder for streaming.
type FolderManifest struct {
	Root        string
	Entries     []FolderEntry
	PlainSize   int64
	RootMtimeMS int64
}

// ScanFolder walks path once, applying every portability and safety rule
// the Python scanner enforces, and computes the exact stream size.
func ScanFolder(path string) (*FolderManifest, error) {
	root, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	rootInfo, err := os.Lstat(root)
	if err != nil {
		return nil, err
	}
	if !rootInfo.IsDir() || rootInfo.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("不支持发送符号链接、联接或特殊目录")
	}
	manifest := &FolderManifest{
		Root:        root,
		RootMtimeMS: rootInfo.ModTime().UnixMilli(),
	}
	manifest.PlainSize = 8 // magic + entry count
	collisions := make(map[string]bool)

	type frame struct{ dir, relative string }
	stack := []frame{{dir: root}}
	for len(stack) > 0 {
		current := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		entries, err := os.ReadDir(current.dir)
		if err != nil {
			return nil, err
		}
		sort.Slice(entries, func(i, j int) bool {
			return caseFolder.String(entries[i].Name()) <
				caseFolder.String(entries[j].Name())
		})
		var directories []frame
		for _, child := range entries {
			relative := child.Name()
			if current.relative != "" {
				relative = current.relative + "/" + child.Name()
			}
			pathBytes := []byte(relative)
			if len(pathBytes) > maxFolderPath {
				return nil, fmt.Errorf("文件夹内路径过长：%s", relative)
			}
			if _, key, err := portablePathParts(relative); err != nil {
				return nil, err
			} else if collisions[key] {
				return nil, fmt.Errorf("文件夹内存在跨平台重名路径：%s", relative)
			} else {
				collisions[key] = true
			}
			info, err := child.Info()
			if err != nil {
				return nil, err
			}
			if info.Mode()&os.ModeSymlink != 0 {
				return nil, fmt.Errorf("不支持发送符号链接或联接：%s", relative)
			}
			mtime := info.ModTime().UnixMilli()
			if mtime < 0 {
				mtime = 0
			}
			childPath := filepath.Join(current.dir, child.Name())
			var entry FolderEntry
			switch {
			case info.IsDir():
				entry = FolderEntry{Relative: relative, PathBytes: pathBytes,
					Source: childPath, IsDir: true, MtimeMS: mtime}
				directories = append(directories, frame{dir: childPath, relative: relative})
			case info.Mode().IsRegular():
				entry = FolderEntry{Relative: relative, PathBytes: pathBytes,
					Source: childPath, Size: info.Size(), MtimeMS: mtime}
			default:
				return nil, fmt.Errorf("不支持发送特殊文件：%s", relative)
			}
			manifest.Entries = append(manifest.Entries, entry)
			if len(manifest.Entries) > maxFolderEntries {
				return nil, errors.New("文件夹条目过多")
			}
			overhead := int64(folderEntrySize+len(pathBytes)) + entry.Size
			if manifest.PlainSize > maxFileSize-overhead {
				return nil, errors.New("文件夹总大小超过 1TB")
			}
			manifest.PlainSize += overhead
		}
		for i := len(directories) - 1; i >= 0; i-- {
			stack = append(stack, directories[i])
		}
	}
	return manifest, nil
}

// folderPayloadReader streams the WHF1 payload without materializing it.
type folderPayloadReader struct {
	manifest *FolderManifest
	buffer   []byte
	index    int
	file     *os.File
	fileLeft int64
	err      error
}

// NewFolderPayloadReader returns an io.ReadCloser over the WHF1 stream.
func NewFolderPayloadReader(manifest *FolderManifest) io.ReadCloser {
	head := make([]byte, 0, 8)
	head = append(head, folderMagic...)
	head = binary.BigEndian.AppendUint32(head, uint32(len(manifest.Entries)))
	return &folderPayloadReader{manifest: manifest, buffer: head}
}

func (r *folderPayloadReader) Read(out []byte) (int, error) {
	for len(r.buffer) == 0 {
		if r.err != nil {
			return 0, r.err
		}
		if r.file != nil {
			if r.fileLeft == 0 {
				// The scanned size must still be exact at read time.
				var probe [1]byte
				if n, _ := r.file.Read(probe[:]); n != 0 {
					r.err = errors.New("发送时文件大小发生变化")
					_ = r.file.Close()
					r.file = nil
					return 0, r.err
				}
				_ = r.file.Close()
				r.file = nil
				continue
			}
			chunk := make([]byte, min64(r.fileLeft, recvBuffer))
			read, err := r.file.Read(chunk)
			if read > 0 {
				r.fileLeft -= int64(read)
				r.buffer = chunk[:read]
				break
			}
			if err != nil {
				r.err = errors.New("发送时文件发生变化")
				_ = r.file.Close()
				r.file = nil
				return 0, r.err
			}
		}
		if r.index >= len(r.manifest.Entries) {
			r.err = io.EOF
			return 0, io.EOF
		}
		entry := r.manifest.Entries[r.index]
		r.index++
		head := make([]byte, 0, folderEntrySize+len(entry.PathBytes))
		kind := byte(1)
		if entry.IsDir {
			kind = 0
		}
		head = append(head, kind)
		head = binary.BigEndian.AppendUint32(head, uint32(len(entry.PathBytes)))
		head = binary.BigEndian.AppendUint64(head, uint64(entry.Size))
		head = binary.BigEndian.AppendUint64(head, uint64(entry.MtimeMS))
		head = append(head, entry.PathBytes...)
		r.buffer = head
		if !entry.IsDir {
			file, err := os.Open(entry.Source)
			if err != nil {
				r.err = err
				return 0, err
			}
			r.file = file
			r.fileLeft = entry.Size
		}
	}
	n := copy(out, r.buffer)
	r.buffer = r.buffer[n:]
	return n, nil
}

func (r *folderPayloadReader) Close() error {
	if r.file != nil {
		_ = r.file.Close()
		r.file = nil
	}
	if r.err == nil {
		r.err = errors.New("closed")
	}
	return nil
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

// extractFolderStream mirrors p2p._receive_folder_stream: unpack a verified
// WHF1 checkpoint into staging with every structural safety rule.
func extractFolderStream(source io.Reader, staging string) error {
	head := make([]byte, 8)
	if _, err := io.ReadFull(source, head); err != nil {
		return err
	}
	if string(head[:4]) != folderMagic {
		return errors.New("文件夹流标识非法")
	}
	entryCount := binary.BigEndian.Uint32(head[4:])
	if entryCount > maxFolderEntries {
		return errors.New("文件夹条目过多")
	}
	stagingAbs, err := filepath.Abs(staging)
	if err != nil {
		return err
	}
	// Resolve both sides the same way (macOS /var -> /private/var etc.),
	// mirroring the realpath-vs-realpath comparison in p2p.py.
	if resolved, err := filepath.EvalSymlinks(stagingAbs); err == nil {
		stagingAbs = resolved
	}
	seen := make(map[string]bool)
	fileKeys := make(map[string]bool)
	ancestorKeys := make(map[string]bool)
	type dirStamp struct {
		path  string
		mtime int64
		depth int
	}
	var directories []dirStamp
	entryHead := make([]byte, folderEntrySize)
	for index := uint32(0); index < entryCount; index++ {
		if _, err := io.ReadFull(source, entryHead); err != nil {
			return err
		}
		entryType := entryHead[0]
		pathSize := binary.BigEndian.Uint32(entryHead[1:5])
		fileSize := int64(binary.BigEndian.Uint64(entryHead[5:13]))
		mtimeMS := int64(binary.BigEndian.Uint64(entryHead[13:21]))
		if pathSize == 0 || pathSize > maxFolderPath {
			return errors.New("文件夹路径长度非法")
		}
		pathRaw := make([]byte, pathSize)
		if _, err := io.ReadFull(source, pathRaw); err != nil {
			return err
		}
		if !utf8.Valid(pathRaw) {
			return errors.New("文件夹路径编码非法")
		}
		relative := string(pathRaw)
		parts, collisionKey, err := portablePathParts(relative)
		if err != nil {
			return err
		}
		if seen[collisionKey] {
			return fmt.Errorf("文件夹内存在重名路径：%s", relative)
		}
		normalized := strings.Split(collisionKey, "/")
		parents := make([]string, 0, len(parts)-1)
		for i := 1; i < len(parts); i++ {
			parents = append(parents, strings.Join(normalized[:i], "/"))
		}
		for _, parent := range parents {
			if fileKeys[parent] {
				return fmt.Errorf("文件与目录结构冲突：%s", relative)
			}
		}
		if entryType == 1 && ancestorKeys[collisionKey] {
			return fmt.Errorf("文件与目录结构冲突：%s", relative)
		}
		if entryType > 1 || (entryType == 0 && fileSize != 0) {
			return errors.New("文件夹条目类型非法")
		}
		if fileSize > maxFileSize {
			return errors.New("文件夹内文件大小非法")
		}
		target := filepath.Join(stagingAbs, filepath.Join(parts...))
		resolved, err := filepath.EvalSymlinks(filepath.Dir(target))
		if err == nil {
			target = filepath.Join(resolved, filepath.Base(target))
		}
		if target != stagingAbs &&
			!strings.HasPrefix(target, stagingAbs+string(os.PathSeparator)) {
			return errors.New("文件夹路径越界")
		}
		seen[collisionKey] = true
		for _, parent := range parents {
			ancestorKeys[parent] = true
		}
		if entryType == 0 {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			directories = append(directories, dirStamp{
				path: target, mtime: mtimeMS, depth: len(parts)})
			continue
		}
		fileKeys[collisionKey] = true
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		output, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err != nil {
			return err
		}
		if _, err := io.CopyN(output, source, fileSize); err != nil {
			_ = output.Close()
			return err
		}
		if err := output.Close(); err != nil {
			return err
		}
		applyMtime(target, mtimeMS)
	}
	// Trailing bytes mean a forged stream.
	var probe [1]byte
	if n, _ := source.Read(probe[:]); n != 0 {
		return errors.New("文件夹流尾部有多余数据")
	}
	sort.Slice(directories, func(i, j int) bool {
		return directories[i].depth > directories[j].depth
	})
	for _, directory := range directories {
		applyMtime(directory.path, directory.mtime)
	}
	return nil
}
