//go:build !windows

package lan

import (
	"errors"
	"syscall"
)

// isDefiniteRefusal separates "this device answered and said no" from "this
// device said nothing". A refusal or an unreachable route is knowledge: the
// peer is not serving here any more, so discovery may drop it at once. A
// timeout is not — a phone with its screen off produces exactly the same
// silence as a phone that left the network, and treating the two alike is
// what made devices flicker in and out of the list before.
func isDefiniteRefusal(err error) bool {
	return errors.Is(err, syscall.ECONNREFUSED) ||
		errors.Is(err, syscall.ECONNRESET) ||
		errors.Is(err, syscall.EHOSTUNREACH) ||
		errors.Is(err, syscall.ENETUNREACH) ||
		errors.Is(err, syscall.ENETDOWN) ||
		errors.Is(err, syscall.EHOSTDOWN)
}
