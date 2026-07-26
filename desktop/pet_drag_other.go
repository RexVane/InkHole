//go:build !darwin

package main

func leftMouseButtonDown() bool { return false }

const dragEndPollSupported = false
