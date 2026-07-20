package wormhole

import (
	"bufio"
	"context"
	"encoding/base64"
	"io"
	"net"
	"net/http"
	"testing"
	"time"
)

func TestDialTransitRelayThroughHTTPConnectProxy(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()

	requestSeen := make(chan *http.Request, 1)
	go func() {
		conn, acceptErr := listener.Accept()
		if acceptErr != nil {
			return
		}
		defer conn.Close()
		request, readErr := http.ReadRequest(bufio.NewReader(conn))
		if readErr != nil {
			return
		}
		requestSeen <- request
		_, _ = io.WriteString(conn, "HTTP/1.1 200 Connection Established\r\n\r\n")
		_, _ = io.Copy(conn, conn)
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	conn, err := dialTransitRelay(
		ctx, "http://user:pass@"+listener.Addr().String(), "relay.example:4001")
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	if _, err := conn.Write([]byte("transit")); err != nil {
		t.Fatal(err)
	}
	received := make([]byte, len("transit"))
	if _, err := io.ReadFull(conn, received); err != nil {
		t.Fatal(err)
	}
	if string(received) != "transit" {
		t.Fatalf("unexpected payload %q", received)
	}

	select {
	case request := <-requestSeen:
		if request.Method != http.MethodConnect || request.Host != "relay.example:4001" {
			t.Fatalf("unexpected CONNECT request: %s %s", request.Method, request.Host)
		}
		expectedAuth := "Basic " + base64.StdEncoding.EncodeToString([]byte("user:pass"))
		if request.Header.Get("Proxy-Authorization") != expectedAuth {
			t.Fatalf("proxy credentials were not forwarded")
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
}

func TestDialTransitRelayRejectsUnsupportedProxy(t *testing.T) {
	_, err := dialTransitRelay(context.Background(), "socks5://127.0.0.1:1080", "relay.example:4001")
	if err == nil {
		t.Fatal("unsupported proxy was accepted")
	}
}
