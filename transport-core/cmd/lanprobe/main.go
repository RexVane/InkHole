// lanprobe is a development tool for exercising the shared LAN stack against
// the Python and Kotlin implementations.
//
//	lanprobe serve          start a WHPC responder, print "PORT <n>"
//	lanprobe probe <host> <port>  probe a peer and print the verified answer
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
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
	case "discover":
		discover()
	case "send":
		if len(os.Args) != 5 {
			fmt.Fprintln(os.Stderr, "usage: lanprobe send <host> <port> <file>")
			os.Exit(2)
		}
		var port int
		if _, err := fmt.Sscanf(os.Args[3], "%d", &port); err != nil {
			fmt.Fprintln(os.Stderr, "bad port:", err)
			os.Exit(2)
		}
		send(os.Args[2], port, os.Args[4])
	case "recv":
		if len(os.Args) != 3 {
			fmt.Fprintln(os.Stderr, "usage: lanprobe recv <inbox>")
			os.Exit(2)
		}
		recv(os.Args[2])
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

// send probes the peer for its verified identity, then transfers one file
// over WHPP v3 (INKHOLE_SECRET enables end-to-end encryption).
func send(host string, port int, path string) {
	identity, err := lan.GenerateIdentity()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	probed, err := lan.ProbePeer(host, port, 5*time.Second, "")
	if err != nil {
		fmt.Fprintln(os.Stderr, "probe failed:", err)
		os.Exit(1)
	}
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	err = lan.SendFile(context.Background(), lan.SendTarget{
		Host:        host,
		Port:        port,
		InstanceID:  probed.InstanceID,
		Fingerprint: probed.Fingerprint,
	}, path, lan.SenderConfig{
		Secret:     os.Getenv("INKHOLE_SECRET"),
		Identity:   identity,
		InstanceID: hex.EncodeToString(raw),
		OnStatus:   func(msg string) { fmt.Println("STATUS", msg) },
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "send failed:", err)
		os.Exit(1)
	}
	fmt.Println("SENT")
}

// recv serves WHPC probes and WHPP transfers into the given inbox.
func recv(inbox string) {
	identity, err := lan.GenerateIdentity()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	instanceID := hex.EncodeToString(raw)
	receiver, err := lan.NewReceiver(lan.ReceiverConfig{
		InboxDir:   inbox,
		Secret:     os.Getenv("INKHOLE_SECRET"),
		Identity:   identity,
		InstanceID: instanceID,
		OnReceived: func(path string) { fmt.Println("RECEIVED", path) },
		OnStatus:   func(msg string) { fmt.Println("STATUS", msg) },
	})
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
	for {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		go func(conn net.Conn) {
			_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
			head := make([]byte, 4)
			if _, err := io.ReadFull(conn, head); err != nil {
				_ = conn.Close()
				return
			}
			_ = conn.SetDeadline(time.Time{})
			switch string(head) {
			case "WHPP":
				receiver.HandleWHPP(conn)
			case "WHPC":
				_ = lan.RespondProbe(conn, identity, instanceID, "Go接收节点",
					[]string{lan.CapReliable})
				_ = conn.Close()
			default:
				_ = conn.Close()
			}
		}(conn)
	}
}

// discover runs the full discovery stack (mDNS + UDP broadcast + prober)
// for ~12s alongside a WHPC responder, so real Python/Kotlin nodes on the
// same network can verify us back. Prints PEERS lines as the list changes.
func discover() {
	identity, err := lan.GenerateIdentity()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	instanceID := hex.EncodeToString(raw)
	listener, err := net.Listen("tcp", ":0")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer listener.Close()
	port := listener.Addr().(*net.TCPAddr).Port
	caps := []string{"folder-v1", "reliable-v3"}
	go func() {
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
				_ = lan.RespondProbe(conn, identity, instanceID, "Go发现节点", caps)
			}(conn)
		}
	}()
	discovery, err := lan.Start(lan.Config{
		PeerName:     "Go发现节点",
		InstanceID:   instanceID,
		Port:         port,
		Identity:     identity,
		Capabilities: caps,
		LocalIPs:     lan.LocalIPv4s(),
	}, func(peers []lan.Peer) {
		out, _ := json.Marshal(peers)
		fmt.Printf("PEERS %s\n", out)
	}, func(msg string) {
		fmt.Printf("STATUS %s\n", msg)
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "discovery failed:", err)
		os.Exit(1)
	}
	fmt.Printf("READY instance=%s port=%d\n", instanceID, port)
	time.Sleep(12 * time.Second)
	final, _ := json.Marshal(discovery.Peers())
	fmt.Printf("FINAL %s\n", final)
	discovery.Stop()
}
