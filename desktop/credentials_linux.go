//go:build linux

package main

import (
	"bytes"
	"errors"
	"os/exec"
	"strings"
)

type systemCredentialStore struct{}

func (store systemCredentialStore) Has(name string) (bool, error) {
	value, err := store.Get(name)
	return value != "", err
}

func (systemCredentialStore) Get(name string) (string, error) {
	output, err := exec.Command("secret-tool", "lookup", "service", credentialService,
		"account", name).Output()
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) && exitErr.ExitCode() == 1 {
			return "", nil
		}
		return "", err
	}
	return strings.TrimSuffix(string(output), "\n"), nil
}

func (systemCredentialStore) Set(name, value string) error {
	if value == "" {
		return systemCredentialStore{}.Delete(name)
	}
	command := exec.Command("secret-tool", "store", "--label=InkHole", "service",
		credentialService, "account", name)
	command.Stdin = bytes.NewBufferString(value)
	return command.Run()
}

func (systemCredentialStore) Delete(name string) error {
	return exec.Command("secret-tool", "clear", "service", credentialService,
		"account", name).Run()
}
