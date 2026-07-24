//go:build !windows

package lan

import "golang.org/x/sys/unix"

func setReuseSocketOptions(fd uintptr) error {
	if err := unix.SetsockoptInt(
		int(fd), unix.SOL_SOCKET, unix.SO_REUSEADDR, 1); err != nil {
		return err
	}
	return unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_REUSEPORT, 1)
}
