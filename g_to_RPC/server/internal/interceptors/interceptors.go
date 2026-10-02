// Package interceptors holds the server's cross-cutting middleware: request
// IDs carried in metadata, one log line per RPC, and panic recovery.
//
// Chain them outermost first, so the logger sees the code recovery produced
// and every log line carries the request ID:
//
//	grpc.ChainUnaryInterceptor(UnaryRequestID(), UnaryLogging(l), UnaryRecovery(l))
package interceptors

import (
	"context"
	"log/slog"
	"runtime/debug"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/ids"
)

// RequestIDKey is the metadata key carrying the request ID, in both the
// request and the response headers. gRPC metadata keys are lowercase.
const RequestIDKey = "x-request-id"

// maxRequestIDLen bounds a client-supplied request ID, so it cannot bloat
// every log line.
const maxRequestIDLen = 128

type requestIDCtxKey struct{}

// RequestID returns the request ID stored in ctx by the request-ID
// interceptor, or "".
func RequestID(ctx context.Context) string {
	id, _ := ctx.Value(requestIDCtxKey{}).(string)
	return id
}

// UnaryRequestID adopts the client's x-request-id if it is valid, generates
// one otherwise, stores it in the context and echoes it in the response
// header, so client and server logs can be joined on it.
func UnaryRequestID() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		ctx, id := withRequestID(ctx)
		// SetHeader fails only if headers were already sent, which cannot
		// happen before the handler runs.
		_ = grpc.SetHeader(ctx, metadata.Pairs(RequestIDKey, id))
		return handler(ctx, req)
	}
}

// StreamRequestID is UnaryRequestID for streaming RPCs.
func StreamRequestID() grpc.StreamServerInterceptor {
	return func(srv any, ss grpc.ServerStream, _ *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
		ctx, id := withRequestID(ss.Context())
		_ = ss.SetHeader(metadata.Pairs(RequestIDKey, id))
		return handler(srv, &contextStream{ServerStream: ss, ctx: ctx})
	}
}

// UnaryLogging writes one line per RPC: method, status code, duration and
// request ID. Its size does not depend on the data; the first version
// printed the entire product map on every add.
func UnaryLogging(logger *slog.Logger) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		start := time.Now()
		resp, err := handler(ctx, req)
		logRPC(ctx, logger, info.FullMethod, start, err)
		return resp, err
	}
}

// StreamLogging is UnaryLogging for streaming RPCs; the line is written when
// the stream ends.
func StreamLogging(logger *slog.Logger) grpc.StreamServerInterceptor {
	return func(srv any, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
		start := time.Now()
		err := handler(srv, ss)
		logRPC(ss.Context(), logger, info.FullMethod, start, err)
		return err
	}
}

// UnaryRecovery turns a panicking handler into an INTERNAL error and logs
// the stack, instead of letting one bad request crash the whole server.
func UnaryRecovery(logger *slog.Logger) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (resp any, err error) {
		defer func() {
			if r := recover(); r != nil {
				err = recovered(ctx, logger, info.FullMethod, r)
			}
		}()
		return handler(ctx, req)
	}
}

// StreamRecovery is UnaryRecovery for streaming RPCs.
func StreamRecovery(logger *slog.Logger) grpc.StreamServerInterceptor {
	return func(srv any, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) (err error) {
		defer func() {
			if r := recover(); r != nil {
				err = recovered(ss.Context(), logger, info.FullMethod, r)
			}
		}()
		return handler(srv, ss)
	}
}

func withRequestID(ctx context.Context) (context.Context, string) {
	var id string
	if md, ok := metadata.FromIncomingContext(ctx); ok {
		if vals := md.Get(RequestIDKey); len(vals) > 0 && validRequestID(vals[0]) {
			id = vals[0]
		}
	}
	if id == "" {
		generated, err := ids.NewUUID()
		if err != nil {
			generated = "unavailable" // never fail an RPC over a log correlation ID
		}
		id = generated
	}
	return context.WithValue(ctx, requestIDCtxKey{}, id), id
}

// validRequestID accepts printable ASCII up to maxRequestIDLen, so a client
// cannot inject newlines or control characters into the logs.
func validRequestID(id string) bool {
	if id == "" || len(id) > maxRequestIDLen {
		return false
	}
	for i := range len(id) {
		if id[i] < 0x21 || id[i] > 0x7e {
			return false
		}
	}
	return true
}

func logRPC(ctx context.Context, logger *slog.Logger, method string, start time.Time, err error) {
	code := status.Code(err)
	level := slog.LevelInfo
	if serverFault(code) {
		level = slog.LevelError
	}
	logger.LogAttrs(ctx, level, "rpc",
		slog.String("method", method),
		slog.String("code", code.String()),
		slog.Duration("duration", time.Since(start)),
		slog.String("request_id", RequestID(ctx)),
	)
}

// serverFault reports codes that mean the server, not the caller, is at
// fault and deserve attention.
func serverFault(c codes.Code) bool {
	switch c {
	case codes.Internal, codes.Unknown, codes.DataLoss, codes.Unimplemented:
		return true
	default:
		return false
	}
}

func recovered(ctx context.Context, logger *slog.Logger, method string, r any) error {
	logger.ErrorContext(ctx, "handler panicked",
		"method", method, "panic", r, "request_id", RequestID(ctx), "stack", string(debug.Stack()))
	return status.Error(codes.Internal, "internal error")
}

// contextStream swaps in a context that carries the request ID.
type contextStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s *contextStream) Context() context.Context { return s.ctx }
