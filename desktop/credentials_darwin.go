//go:build darwin

package main

import (
	"context"
	"errors"
	"os/exec"
	"strings"
	"time"
)

type systemCredentialStore struct{}

const credentialCommandTimeout = 15 * time.Second

func runSecurityCommand(args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), credentialCommandTimeout)
	defer cancel()
	output, err := exec.CommandContext(ctx, "security", args...).Output()
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	return output, err
}

func (systemCredentialStore) Has(name string) (bool, error) {
	_, err := runSecurityCommand("find-generic-password", "-a", name,
		"-s", credentialService)
	if credentialMissing(err) {
		return false, nil
	}
	return err == nil, err
}

func (systemCredentialStore) Get(name string) (string, error) {
	output, err := runSecurityCommand("find-generic-password", "-a", name,
		"-s", credentialService, "-w")
	if credentialMissing(err) {
		return "", nil
	}
	return strings.TrimSuffix(string(output), "\n"), err
}

func (systemCredentialStore) Set(name, value string) error {
	if value == "" {
		return systemCredentialStore{}.Delete(name)
	}
	_, err := runSecurityCommand("add-generic-password", "-U", "-a", name,
		"-s", credentialService, "-w", value)
	return err
}

func (systemCredentialStore) Delete(name string) error {
	_, err := runSecurityCommand("delete-generic-password", "-a", name,
		"-s", credentialService)
	if credentialMissing(err) {
		return nil
	}
	return err
}

func credentialMissing(err error) bool {
	var exitErr *exec.ExitError
	return errors.As(err, &exitErr) && exitErr.ExitCode() == 44
}
