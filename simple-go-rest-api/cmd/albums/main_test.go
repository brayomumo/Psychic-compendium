package main

import (
	"bytes"
	"net"
	"os"
	"strings"
	"testing"
)

func envFrom(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

// TestMain lets the signal tests re-run this binary as the real command:
// with REST_API_TEST_RUN_MAIN=1 it behaves like main instead of running
// tests.
func TestMain(m *testing.M) {
	if os.Getenv("REST_API_TEST_RUN_MAIN") == "1" {
		os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, os.Getenv))
	}
	os.Exit(m.Run())
}

func TestRunRejectsBadConfiguration(t *testing.T) {
	withDB := map[string]string{"DATABASE_URL": "postgres://u:p@localhost/db"}
	tests := []struct {
		name string
		args []string
		env  map[string]string
		want string
	}{
		{"unknown flag", []string{"-nope"}, withDB, "flag provided but not defined"},
		{"stray argument", []string{"serve"}, withDB, "unexpected arguments"},
		{"bad store", []string{"-store", "mysql"}, withDB, "-store must be postgres or memory"},
		{"postgres without DATABASE_URL", nil, nil, "DATABASE_URL must be set"},
		{"addr without port", []string{"-addr", "localhost"}, withDB, "is not host:port"},
		{"zero timeout", []string{"-request-timeout", "0s"}, withDB, "-request-timeout must be between"},
		{"huge timeout", []string{"-shutdown-timeout", "2h"}, withDB, "-shutdown-timeout must be between"},
		{"body limit too big", []string{"-max-body-bytes", "99999999"}, withDB, "-max-body-bytes must be between"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			code := run(tt.args, &stdout, &stderr, envFrom(tt.env))
			if code != exitUsage {
				t.Errorf("exit = %d, want %d", code, exitUsage)
			}
			if !strings.Contains(stderr.String(), tt.want) {
				t.Errorf("stderr does not mention %q:\n%s", tt.want, stderr.String())
			}
			if stdout.Len() != 0 {
				t.Errorf("stdout = %q, want nothing (usage errors go to stderr)", stdout.String())
			}
		})
	}
}

func TestRunReportsEveryProblemAtOnce(t *testing.T) {
	var stderr bytes.Buffer
	run([]string{"-store", "x", "-request-timeout", "0s", "-max-body-bytes", "0"}, &bytes.Buffer{}, &stderr, envFrom(nil))
	for _, want := range []string{"-store", "-request-timeout", "-max-body-bytes"} {
		if !strings.Contains(stderr.String(), want+" must") {
			t.Errorf("stderr does not report %s:\n%s", want, stderr.String())
		}
	}
}

func TestRunHelpExitsZero(t *testing.T) {
	var stderr bytes.Buffer
	if code := run([]string{"-h"}, &bytes.Buffer{}, &stderr, envFrom(nil)); code != exitOK {
		t.Errorf("-h exit = %d, want 0", code)
	}
	if !strings.Contains(stderr.String(), "DATABASE_URL") {
		t.Errorf("usage does not document DATABASE_URL:\n%s", stderr.String())
	}
}

// Regression: the first version listened on localhost:8080, which inside a
// container is the container's own loopback, unreachable from the host.
func TestDefaultAddressListensOnAllInterfaces_RegressionLocalhostBind(t *testing.T) {
	o, _, ok := parseFlags(nil, &bytes.Buffer{}, envFrom(map[string]string{"DATABASE_URL": "x"}))
	if !ok || o.addr != ":8080" {
		t.Errorf("default addr = %q (ok=%v), want :8080", o.addr, ok)
	}
	o, _, ok = parseFlags(nil, &bytes.Buffer{}, envFrom(map[string]string{"DATABASE_URL": "x", "REST_API_ADDR": "127.0.0.1:9000"}))
	if !ok || o.addr != "127.0.0.1:9000" {
		t.Errorf("REST_API_ADDR not honoured: addr = %q", o.addr)
	}
}

func TestRunFailsWhenTheDatabaseNeverAnswers(t *testing.T) {
	var stdout, stderr bytes.Buffer
	env := envFrom(map[string]string{"DATABASE_URL": "postgres://u:p@127.0.0.1:1/db?sslmode=disable&connect_timeout=1"})
	code := run([]string{"-addr", "127.0.0.1:0", "-startup-timeout", "300ms"}, &stdout, &stderr, env)
	if code != exitFailure {
		t.Errorf("exit = %d, want %d", code, exitFailure)
	}
	if !strings.Contains(stderr.String(), "startup failed") || strings.Contains(stdout.String(), "listening") {
		t.Errorf("want a startup failure before listening; stdout %q stderr:\n%s", stdout.String(), stderr.String())
	}
}

func TestRunFailsWhenTheAddressIsTaken(t *testing.T) {
	taken, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer taken.Close()
	var stdout, stderr bytes.Buffer
	code := run([]string{"-store", "memory", "-addr", taken.Addr().String()}, &stdout, &stderr, envFrom(nil))
	if code != exitFailure || !strings.Contains(stderr.String(), "listen failed") {
		t.Errorf("exit = %d, stderr:\n%s; want a listen failure", code, stderr.String())
	}
}
