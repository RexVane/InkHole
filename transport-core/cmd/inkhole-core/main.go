package main

import (
	"bufio"
	"fmt"
	"os"
	"os/signal"
	"sync"
	"syscall"

	"github.com/rexvane/inkhole/transport-core/core"
)

func main() {
	service := core.NewService()
	defer service.Close()

	var outputMu sync.Mutex
	writeLine := func(line string) {
		outputMu.Lock()
		defer outputMu.Unlock()
		_, _ = fmt.Fprintln(os.Stdout, line)
	}
	go func() {
		for event := range service.Events() {
			writeLine(core.EncodeEvent(event))
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-stop
		_ = service.Close()
		os.Exit(0)
	}()

	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 64*1024), 8*1024*1024)
	var requests sync.WaitGroup
	for scanner.Scan() {
		line := scanner.Text()
		requests.Add(1)
		go func() {
			defer requests.Done()
			writeLine(service.HandleJSON(line))
		}()
	}
	requests.Wait()
	if err := scanner.Err(); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, "control input:", err)
	}
}
