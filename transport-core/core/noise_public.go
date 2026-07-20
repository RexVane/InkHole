package core

import "golang.org/x/crypto/curve25519"

func generatePublicKey(privateKey []byte) []byte {
	publicKey, err := curve25519.X25519(privateKey, curve25519.Basepoint)
	if err != nil {
		return nil
	}
	return publicKey
}
