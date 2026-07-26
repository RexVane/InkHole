//go:build darwin

package main

/*
#cgo CFLAGS: -x objective-c
#cgo LDFLAGS: -framework AppKit
#import <AppKit/AppKit.h>

static int inkholeLeftMouseDown(void) {
    return ([NSEvent pressedMouseButtons] & 1) ? 1 : 0;
}
*/
import "C"

// leftMouseButtonDown reports the global left-button state straight from the
// window server. It stays accurate while the window manager owns the native
// drag loop and the WebView receives no mouse events at all.
func leftMouseButtonDown() bool { return C.inkholeLeftMouseDown() != 0 }

const dragEndPollSupported = true
