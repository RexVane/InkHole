package core

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"testing"
	"time"
)

func TestSSHRelayRealDataPath(t *testing.T) {
	host := os.Getenv("INKHOLE_TEST_SSH_HOST")
	user := os.Getenv("INKHOLE_TEST_SSH_USER")
	keyPath := os.Getenv("INKHOLE_TEST_SSH_KEY")
	hostKey := os.Getenv("INKHOLE_TEST_SSH_HOST_KEY")
	if host == "" || user == "" || keyPath == "" || hostKey == "" {
		t.Skip("real SSH relay test is not configured")
	}
	privateKey, err := os.ReadFile(keyPath)
	if err != nil {
		t.Fatal(err)
	}
	profile := SSHProfile{
		Host: host, Port: 22, User: user, PrivateKey: string(privateKey),
		HostKeySHA256: hostKey,
	}

	for _, endToEnd := range []bool{false, true} {
		endToEnd := endToEnd
		t.Run(fmt.Sprintf("end_to_end_%t", endToEnd), func(t *testing.T) {
			targetA, tokenA := newEchoTarget(t)
			targetB, tokenB := newEchoTarget(t)
			serviceA := NewService()
			t.Cleanup(func() { _ = serviceA.Close() })
			serviceB := NewService()
			t.Cleanup(func() { _ = serviceB.Close() })
			startServiceForSSHTest(t, serviceA, "relay-a", targetA, tokenA)
			startServiceForSSHTest(t, serviceB, "relay-b", targetB, tokenB)

			sessionA, privateA := startRealSSHSession(t, serviceA, profile)
			sessionB, privateB := startRealSSHSession(t, serviceB, profile)
			keyA, err := decodeNoiseKey(privateA)
			if err != nil {
				t.Fatal(err)
			}
			keyB, err := decodeNoiseKey(privateB)
			if err != nil {
				t.Fatal(err)
			}
			peerB, err := sessionA.addPeer(SSHPeer{
				InstanceID: "relay-b", Name: "relay-b", RemotePort: sessionB.remotePort,
				NoisePublic: encodeNoisePublic(keyB.Public), EndToEnd: endToEnd,
			})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := sessionB.addPeer(SSHPeer{
				InstanceID: "relay-a", Name: "relay-a", RemotePort: sessionA.remotePort,
				NoisePublic: encodeNoisePublic(keyA.Public), EndToEnd: endToEnd,
			}); err != nil {
				t.Fatal(err)
			}

			conn, err := net.DialTimeout("tcp", peerB.Endpoint, 10*time.Second)
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			_ = conn.SetDeadline(time.Now().Add(30 * time.Second))
			payload := []byte("inkhole-ssh-data-path")
			if _, err := conn.Write(append([]byte("IKAT"+peerB.EndpointToken), payload...)); err != nil {
				t.Fatal(err)
			}
			got := make([]byte, len(payload))
			if _, err := io.ReadFull(conn, got); err != nil {
				t.Fatal(err)
			}
			if string(got) != string(payload) {
				t.Fatalf("echo payload = %q", got)
			}
		})
	}
}

func startServiceForSSHTest(t *testing.T, service *Service, instanceID, target, token string) {
	t.Helper()
	params, _ := json.Marshal(StartParams{
		LocalTarget: target, LocalToken: token, DeviceName: instanceID, InstanceID: instanceID,
	})
	if _, err := service.handle("start", params); err != nil {
		t.Fatal(err)
	}
}

func startRealSSHSession(t *testing.T, service *Service, profile SSHProfile) (*sshListenerSession, string) {
	t.Helper()
	params, _ := json.Marshal(sshListenParams{Profile: profile})
	result, err := service.listenSSH(params)
	if err != nil {
		t.Fatal(err)
	}
	values := result.(map[string]any)
	session := service.getSession(values["session_id"].(string)).(*sshListenerSession)
	return session, values["noise_private"].(string)
}

func newEchoTarget(t *testing.T) (string, string) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	token := newCapabilityToken()
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				header := make([]byte, len("IKCI"+token))
				if _, err := io.ReadFull(conn, header); err != nil || string(header) != "IKCI"+token {
					return
				}
				_, _ = io.Copy(conn, conn)
			}()
		}
	}()
	return listener.Addr().String(), token
}
