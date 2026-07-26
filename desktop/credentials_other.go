//go:build !darwin && !linux && !windows

package main

import "errors"

type systemCredentialStore struct{}

func (systemCredentialStore) Has(string) (bool, error) {
	return false, errors.New("system credential storage is unavailable")
}

func (systemCredentialStore) Get(string) (string, error) {
	return "", errors.New("system credential storage is unavailable")
}

func (systemCredentialStore) Set(string, string) error {
	return errors.New("system credential storage is unavailable")
}

func (systemCredentialStore) Delete(string) error {
	return errors.New("system credential storage is unavailable")
}
