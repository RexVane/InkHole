// Package lan implements the InkHole LAN identity and discovery protocols.
// Every wire format here is byte-compatible with the Python stack (p2p.py,
// device_identity.py) and the Kotlin stack (InkHoleNode.kt, DeviceIdentity.kt);
// cross-implementation vectors are pinned in the tests. This package is the
// first stage of collapsing the three hand-written protocol stacks into the
// shared Go core.
package lan

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"strings"
)

const (
	capDomain      = "INKHOLE-WHPC3\x00"
	transferDomain = "INKHOLE-WHPP3-AUTH\x00"
	receiverDomain = "INKHOLE-WHPP3-RECEIVER\x00"

	maxIdentityField = 0xFFFF
	nonceSize        = 32
)

// Identity is an ECDSA P-256 device identity. The private key serializes as
// base64(PKCS#8 DER) and the public key as base64(X9.62 uncompressed point),
// matching what the desktop keeps in the system credential store and Android
// keeps in the Keystore-encrypted preferences.
type Identity struct {
	key *ecdsa.PrivateKey

	// PublicBytes is the 65-byte X9.62 uncompressed point.
	PublicBytes []byte
	// PublicKey is base64(PublicBytes) as exchanged over WHPC.
	PublicKey string
	// Fingerprint is hex(sha256(PublicBytes)) as shown in device subtitles.
	Fingerprint string
}

func newIdentity(key *ecdsa.PrivateKey) (*Identity, error) {
	if key.Curve != elliptic.P256() {
		return nil, errors.New("device identity must use P-256")
	}
	point := elliptic.Marshal(elliptic.P256(), key.PublicKey.X, key.PublicKey.Y)
	sum := sha256.Sum256(point)
	return &Identity{
		key:         key,
		PublicBytes: point,
		PublicKey:   base64.StdEncoding.EncodeToString(point),
		Fingerprint: hex.EncodeToString(sum[:]),
	}, nil
}

func GenerateIdentity() (*Identity, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	return newIdentity(key)
}

// IdentityFromPrivateKey loads a base64(PKCS#8 DER) private key produced by
// any of the three stacks.
func IdentityFromPrivateKey(encoded string) (*Identity, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(encoded))
	if err != nil {
		return nil, fmt.Errorf("device identity is not valid base64: %w", err)
	}
	parsed, err := x509.ParsePKCS8PrivateKey(raw)
	if err != nil {
		return nil, fmt.Errorf("device identity is not valid PKCS#8: %w", err)
	}
	key, ok := parsed.(*ecdsa.PrivateKey)
	if !ok {
		return nil, errors.New("device identity is not an EC private key")
	}
	return newIdentity(key)
}

func (id *Identity) ExportPrivateKey() (string, error) {
	raw, err := x509.MarshalPKCS8PrivateKey(id.key)
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(raw), nil
}

// Sign returns base64(ASN.1 DER ECDSA-SHA256 signature), the encoding the
// Python cryptography library and Android java.security both produce.
func (id *Identity) Sign(message []byte) (string, error) {
	digest := sha256.Sum256(message)
	signature, err := ecdsa.SignASN1(rand.Reader, id.key, digest[:])
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(signature), nil
}

// PublicFingerprint validates an encoded public key and returns its
// fingerprint, mirroring device_identity.public_fingerprint.
func PublicFingerprint(encoded string) (string, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(encoded))
	if err != nil {
		return "", fmt.Errorf("device public key is not valid base64: %w", err)
	}
	if len(raw) != 65 || raw[0] != 4 {
		return "", errors.New("device public key is not an uncompressed P-256 point")
	}
	x, y := elliptic.Unmarshal(elliptic.P256(), raw)
	if x == nil || y == nil {
		return "", errors.New("device public key is not on the P-256 curve")
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

// Verify checks base64(ASN.1 DER) ECDSA-SHA256 signatures from any stack.
func Verify(encodedPublicKey string, message []byte, encodedSignature string) bool {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(encodedPublicKey))
	if err != nil || len(raw) != 65 || raw[0] != 4 {
		return false
	}
	x, y := elliptic.Unmarshal(elliptic.P256(), raw)
	if x == nil || y == nil {
		return false
	}
	signature, err := base64.StdEncoding.DecodeString(strings.TrimSpace(encodedSignature))
	if err != nil {
		return false
	}
	digest := sha256.Sum256(message)
	public := &ecdsa.PublicKey{Curve: elliptic.P256(), X: x, Y: y}
	return ecdsa.VerifyASN1(public, digest[:], signature)
}

func encodedText(value string, limit int) ([]byte, error) {
	encoded := []byte(value)
	if len(encoded) > limit {
		return nil, errors.New("identity field is too long")
	}
	return encoded, nil
}

// CapabilityMessage builds the signed WHPC v3 challenge payload:
//
//	"INKHOLE-WHPC3\0" nonce(32) version(u16 BE) instance_id
//	len(name)(u16 BE) name count(u16 BE) [len(u16 BE) cap]...
//
// with capabilities deduplicated and sorted bytewise, exactly like
// device_identity.capability_message.
func CapabilityMessage(nonce []byte, instanceID, peerName string,
	version int, capabilities []string) ([]byte, error) {
	if len(nonce) != nonceSize {
		return nil, errors.New("capability nonce must be 32 bytes")
	}
	if version < 0 || version > 0xFFFF {
		return nil, errors.New("capability version is invalid")
	}
	name, err := encodedText(peerName, maxIdentityField)
	if err != nil {
		return nil, err
	}
	unique := make(map[string]struct{}, len(capabilities))
	for _, capability := range capabilities {
		if len(capability) > maxIdentityField {
			return nil, errors.New("identity field is too long")
		}
		unique[capability] = struct{}{}
	}
	caps := make([]string, 0, len(unique))
	for capability := range unique {
		caps = append(caps, capability)
	}
	sort.Strings(caps)
	if len(caps) > 0xFFFF {
		return nil, errors.New("too many capabilities")
	}

	var out []byte
	out = append(out, capDomain...)
	out = append(out, nonce...)
	out = binary.BigEndian.AppendUint16(out, uint16(version))
	out = append(out, strings.ToLower(instanceID)...)
	out = binary.BigEndian.AppendUint16(out, uint16(len(name)))
	out = append(out, name...)
	out = binary.BigEndian.AppendUint16(out, uint16(len(caps)))
	for _, capability := range caps {
		out = binary.BigEndian.AppendUint16(out, uint16(len(capability)))
		out = append(out, capability...)
	}
	return out, nil
}

// ValidInstanceID reports whether value is a 32-char lowercase hex id.
func ValidInstanceID(value string) bool {
	if len(value) != 32 {
		return false
	}
	for _, r := range value {
		if (r < '0' || r > '9') && (r < 'a' || r > 'f') {
			return false
		}
	}
	return true
}
