//go:build unix

package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

// startServer runs this test binary as a real productserver process on a
// free port and waits for its readiness line. The process is started
// directly by exec, never through a shell `&`, which would start it with
// SIGINT ignored.
func startServer(t *testing.T) (*exec.Cmd, string, *bytes.Buffer) {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-addr", "127.0.0.1:0", "-shutdown-timeout", "5s")
	cmd.Env = append(os.Environ(), runMainEnv+"=1", "GORACE=atexit_sleep_ms=0")
	stderr := &bytes.Buffer{}
	cmd.Stderr = stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	// Kill the child if the test fails or hangs before it exits on its own.
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
	timer := time.AfterFunc(10*time.Second, func() { _ = cmd.Process.Kill() })
	t.Cleanup(func() { timer.Stop() })

	line, err := bufio.NewReader(stdout).ReadString('\n')
	if err != nil {
		t.Fatalf("reading readiness line: %v; stderr:\n%s", err, stderr)
	}
	addr, ok := strings.CutPrefix(strings.TrimSpace(line), "listening on ")
	if !ok {
		t.Fatalf("readiness line = %q, want %q", line, "listening on <addr>")
	}
	return cmd, addr, stderr
}

func exitCode(t *testing.T, cmd *exec.Cmd) int {
	t.Helper()
	err := cmd.Wait()
	var exitErr *exec.ExitError
	switch {
	case err == nil:
		return 0
	case errors.As(err, &exitErr):
		return exitErr.ExitCode()
	default:
		t.Fatalf("waiting for server: %v", err)
		return -1
	}
}

func TestSignalStopsServerCleanly(t *testing.T) {
	tests := []struct {
		sig  syscall.Signal
		want int
	}{
		{syscall.SIGINT, 130},
		{syscall.SIGTERM, 143},
	}
	for _, tt := range tests {
		t.Run(tt.sig.String(), func(t *testing.T) {
			cmd, addr, stderr := startServer(t)

			// Prove it is really serving before stopping it.
			conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			resp, err := healthpb.NewHealthClient(conn).Check(ctx, &healthpb.HealthCheckRequest{})
			if err != nil || resp.GetStatus() != healthpb.HealthCheckResponse_SERVING {
				t.Fatalf("health Check = %v, %v; want SERVING", resp, err)
			}

			if err := cmd.Process.Signal(tt.sig); err != nil {
				t.Fatal(err)
			}
			if got := exitCode(t, cmd); got != tt.want {
				t.Fatalf("exit code = %d, want %d; stderr:\n%s", got, tt.want, stderr)
			}
			for _, want := range []string{"shutting down", "signal=" + tt.sig.String(), "stopped"} {
				if !strings.Contains(stderr.String(), want) {
					t.Errorf("stderr missing %q:\n%s", want, stderr)
				}
			}
		})
	}
}
