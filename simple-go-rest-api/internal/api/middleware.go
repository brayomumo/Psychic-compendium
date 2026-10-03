package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"net/http"
	"runtime/debug"
	"time"
)

// RequestIDHeader carries the request ID in both directions. A valid ID from
// the client (for example one set by a proxy) is kept, so one ID follows the
// request across services; otherwise the server generates one.
const RequestIDHeader = "X-Request-ID"

const maxRequestIDLen = 64

type requestIDKey struct{}

func requestID(ctx context.Context) string {
	id, _ := ctx.Value(requestIDKey{}).(string)
	return id
}

// validRequestID accepts short IDs made of URL-safe characters. Anything
// else is replaced, so a client cannot inject newlines or megabytes into
// the logs.
func validRequestID(id string) bool {
	if id == "" || len(id) > maxRequestIDLen {
		return false
	}
	for _, c := range []byte(id) {
		ok := c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' ||
			c == '-' || c == '_' || c == '.'
		if !ok {
			return false
		}
	}
	return true
}

func newRequestID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b) // crypto/rand.Read never returns an error on supported platforms
	return hex.EncodeToString(b)
}

// instrument wraps every request: it assigns a request ID, recovers from
// panics with a 500 instead of a dropped connection, and logs one line per
// request with its status and duration.
func instrument(log *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		id := r.Header.Get(RequestIDHeader)
		if !validRequestID(id) {
			id = newRequestID()
		}
		r = r.WithContext(context.WithValue(r.Context(), requestIDKey{}, id))
		w.Header().Set(RequestIDHeader, id)
		w.Header().Set("X-Content-Type-Options", "nosniff")
		rec := &statusRecorder{ResponseWriter: w}

		defer func() {
			if v := recover(); v != nil {
				if err, ok := v.(error); ok && errors.Is(err, http.ErrAbortHandler) {
					panic(v) // net/http's documented way to abort a response; let it through
				}
				log.ErrorContext(r.Context(), "panic serving request",
					"request_id", id, "panic", v, "stack", string(debug.Stack()))
				if !rec.wroteHeader {
					writeError(rec, r, http.StatusInternalServerError, "internal", "internal server error", nil)
				}
			}
			level := slog.LevelInfo
			if rec.status >= http.StatusInternalServerError {
				level = slog.LevelError
			}
			log.LogAttrs(r.Context(), level, "request",
				slog.String("request_id", id),
				slog.String("method", r.Method),
				slog.String("path", r.URL.EscapedPath()),
				slog.Int("status", rec.statusCode()),
				slog.Int64("bytes", rec.bytes),
				slog.Duration("duration", time.Since(start)),
			)
		}()
		next.ServeHTTP(rec, r)
	})
}

// statusRecorder remembers the status and size of a response.
type statusRecorder struct {
	http.ResponseWriter
	status      int
	wroteHeader bool
	bytes       int64
}

func (s *statusRecorder) WriteHeader(code int) {
	if !s.wroteHeader {
		s.status, s.wroteHeader = code, true
	}
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusRecorder) Write(b []byte) (int, error) {
	if !s.wroteHeader {
		s.WriteHeader(http.StatusOK)
	}
	n, err := s.ResponseWriter.Write(b)
	s.bytes += int64(n)
	return n, err
}

func (s *statusRecorder) statusCode() int {
	if !s.wroteHeader {
		return http.StatusOK // net/http sends 200 for a handler that writes nothing
	}
	return s.status
}

// Unwrap lets http.ResponseController reach the underlying writer.
func (s *statusRecorder) Unwrap() http.ResponseWriter { return s.ResponseWriter }
