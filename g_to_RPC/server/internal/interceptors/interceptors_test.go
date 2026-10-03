package interceptors

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestValidRequestID(t *testing.T) {
	tests := []struct {
		id   string
		want bool
	}{
		{"abc-123", true},
		{"a", true},
		{strings.Repeat("x", maxRequestIDLen), true},
		{"", false},
		{strings.Repeat("x", maxRequestIDLen+1), false},
		{"two words", false},
		{"new\nline", false},
		{"tab\tinside", false},
		{"ünïcode", false},
	}
	for _, tt := range tests {
		if got := validRequestID(tt.id); got != tt.want {
			t.Errorf("validRequestID(%q) = %v, want %v", tt.id, got, tt.want)
		}
	}
}

func newLogger() (*slog.Logger, *bytes.Buffer) {
	var buf bytes.Buffer
	return slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})), &buf
}

func TestUnaryRecoveryTurnsPanicIntoInternal(t *testing.T) {
	logger, logs := newLogger()
	info := &grpc.UnaryServerInfo{FullMethod: "/test/Panics"}
	_, err := UnaryRecovery(logger)(context.Background(), nil, info, func(context.Context, any) (any, error) {
		panic("boom")
	})
	if status.Code(err) != codes.Internal || strings.Contains(err.Error(), "boom") {
		t.Fatalf("err = %v, want INTERNAL without the panic value", err)
	}
	if out := logs.String(); !strings.Contains(out, "handler panicked") || !strings.Contains(out, "boom") ||
		!strings.Contains(out, "goroutine") {
		t.Fatalf("log = %q, want the panic value and a stack trace", out)
	}
}

type fakeStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (f fakeStream) Context() context.Context { return f.ctx }

func TestStreamRecoveryTurnsPanicIntoInternal(t *testing.T) {
	logger, logs := newLogger()
	info := &grpc.StreamServerInfo{FullMethod: "/test/StreamPanics"}
	err := StreamRecovery(logger)(nil, fakeStream{ctx: context.Background()}, info, func(any, grpc.ServerStream) error {
		panic("stream boom")
	})
	if status.Code(err) != codes.Internal {
		t.Fatalf("err = %v, want INTERNAL", err)
	}
	if !strings.Contains(logs.String(), "stream boom") {
		t.Fatalf("log = %q, want the panic value", logs.String())
	}
}

func TestLoggingLevelFollowsWhoIsAtFault(t *testing.T) {
	tests := []struct {
		err       error
		wantLevel string
	}{
		{nil, "level=INFO"},
		{status.Error(codes.NotFound, "x"), "level=INFO"},        // the caller's mistake
		{status.Error(codes.InvalidArgument, "x"), "level=INFO"}, // the caller's mistake
		{status.Error(codes.Internal, "x"), "level=ERROR"},       // ours
		{status.Error(codes.Unimplemented, "x"), "level=ERROR"},  // ours
	}
	for _, tt := range tests {
		logger, logs := newLogger()
		info := &grpc.UnaryServerInfo{FullMethod: "/test/Method"}
		_, _ = UnaryLogging(logger)(context.Background(), nil, info, func(context.Context, any) (any, error) {
			return nil, tt.err
		})
		out := logs.String()
		if !strings.Contains(out, tt.wantLevel) || !strings.Contains(out, "method=/test/Method") ||
			strings.Count(out, "\n") != 1 {
			t.Errorf("for %v: log = %q, want one %s line naming the method", tt.err, out, tt.wantLevel)
		}
	}
}
