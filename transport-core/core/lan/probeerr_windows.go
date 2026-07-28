//go:build windows

package lan

import (
	"errors"
	"syscall"
)

// Winsock reports the same conditions under its own numbering; Go surfaces
// these as syscall.Errno values from the connect call.
const (
	wsaenetdown     = syscall.Errno(10050)
	wsaenetunreach  = syscall.Errno(10051)
	wsaeconnreset   = syscall.Errno(10054)
	wsaeconnrefused = syscall.Errno(10061)
	wsaehostdown    = syscall.Errno(10064)
	wsaehostunreach = syscall.Errno(10065)
)

// isDefiniteRefusal separates "this device answered and said no" from "this
// device said nothing"; see the comment on the Unix build for why the
// distinction decides how fast a peer may be dropped.
func isDefiniteRefusal(err error) bool {
	return errors.Is(err, wsaeconnrefused) ||
		errors.Is(err, wsaeconnreset) ||
		errors.Is(err, wsaehostunreach) ||
		errors.Is(err, wsaenetunreach) ||
		errors.Is(err, wsaenetdown) ||
		errors.Is(err, wsaehostdown)
}
