//go:build windows

package lan

import "golang.org/x/sys/windows"

func setReuseSocketOptions(fd uintptr) error {
	return windows.SetsockoptInt(
		windows.Handle(fd), windows.SOL_SOCKET, windows.SO_REUSEADDR, 1)
}
