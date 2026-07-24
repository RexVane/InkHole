// lanprobe is a development tool for exercising the shared LAN stack against
// the Python and Kotlin implementations.
//
//	lanprobe serve          start a WHPC responder, print "PORT <n>"
//	lanprobe probe <host> <port>  probe a peer and print the verified answer
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"time"

	"github.com/rexvane/inkhole/transport-core/core/lan"
)

const serveInstanceID = "1234567890abcdef1234567890abcdef"

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: lanprobe serve | lanprobe probe <host> <port>")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "serve":
		serve()
	case "probe":
		if len(os.Args) != 4 {
			fmt.Fprintln(os.Stderr, "usage: lanprobe probe <host> <port>")
			os.Exit(2)
		}
		var port int
		if _, err := fmt.Sscanf(os.Args[3], "%d", &port); err != nil {
			fmt.Fprintln(os.Stderr, "bad port:", err)
			os.Exit(2)
		}
		probe(os.Args[2], port)
	default:
		fmt.Fprintln(os.Stderr, "unknown command:", os.Args[1])
		os.Exit(2)
	}
}

func serve() {
	identity, err := lan.GenerateIdentity()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Printf("PORT %d\n", listener.Addr().(*net.TCPAddr).Port)
	fmt.Printf("FINGERPRINT %s\n", identity.Fingerprint)
	for {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		go func(conn net.Conn) {
			defer conn.Close()
			_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
			head := make([]byte, 4)
			if _, err := io.ReadFull(conn, head); err != nil || string(head) != "WHPC" {
				return
			}
			if err := lan.RespondProbe(conn, identity, serveInstanceID,
				"Go测试节点", []string{"folder-v1", "reliable-v3"}); err != nil {
				fmt.Fprintln(os.Stderr, "respond:", err)
			}
		}(conn)
	}
}

func probe(host string, port int) {
	result, err := lan.ProbePeer(host, port, 5*time.Second, "")
	if err != nil {
		fmt.Fprintln(os.Stderr, "probe failed:", err)
		os.Exit(1)
	}
	out, _ := json.Marshal(result)
	fmt.Println(string(out))
}
