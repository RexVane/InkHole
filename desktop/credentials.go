package main

const credentialService = "com.rexvane.inkhole"

type credentialStore interface {
	Has(string) (bool, error)
	Get(string) (string, error)
	Set(string, string) error
	Delete(string) error
}

func sshCredentialName(profileID, kind string) string {
	return "ssh:" + profileID + ":" + kind
}
