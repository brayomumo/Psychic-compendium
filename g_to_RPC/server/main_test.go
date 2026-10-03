package main

import (
	"bytes"
	"net"
	"os"
	"strings"
	"testing"
)

// runMainEnv makes the test binary act as productserver itself, so signal
// tests can drive a real process (see signal_test.go).
const runMainEnv = "PRODUCTSERVER_TEST_RUN_MAIN"

func TestMain(m *testing.M) {
	if os.Getenv(runMainEnv) == "1" {
		os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
	}
	os.Exit(m.Run())
}

func TestRunExitCodes(t *testing.T) {
	tests := []struct {
		name       string
		args       []string
		want       int
		wantStderr []string
	}{
		{"help", []string{"-h"}, exitOK, []string{"-addr"}},
		{"unknown flag", []string{"-nope"}, exitUsage, []string{"flag provided but not defined"}},
		{"stray argument", []string{"extra"}, exitUsage, []string{`unexpected argument "extra"`}},
		{"every invalid value reported at once",
			[]string{"-shutdown-timeout", "0s", "-max-products", "0", "-max-recv-bytes", "1"}, exitUsage,
			[]string{"-shutdown-timeout must be", "-max-products must be", "-max-recv-bytes must be"}},
		{"huge values rejected", []string{"-shutdown-timeout", "1h", "-max-products", "99999999"}, exitUsage,
			[]string{"-shutdown-timeout must be", "-max-products must be"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			if got := run(tt.args, &stdout, &stderr); got != tt.want {
				t.Fatalf("run(%q) = %d, want %d; stderr:\n%s", tt.args, got, tt.want, stderr.String())
			}
			for _, want := range tt.wantStderr {
				if !strings.Contains(stderr.String(), want) {
					t.Errorf("stderr missing %q:\n%s", want, stderr.String())
				}
			}
			if stdout.Len() != 0 {
				t.Errorf("stdout = %q, want nothing", stdout.String())
			}
		})
	}
}

func TestRunFailsWhenAddressIsTaken(t *testing.T) {
	taken, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer taken.Close()
	var stdout, stderr bytes.Buffer
	if got := run([]string{"-addr", taken.Addr().String()}, &stdout, &stderr); got != exitFailure {
		t.Fatalf("run on a taken address = %d, want %d; stderr:\n%s", got, exitFailure, stderr.String())
	}
	if !strings.Contains(stderr.String(), "listen") {
		t.Fatalf("stderr = %q, want the listen error", stderr.String())
	}
}
