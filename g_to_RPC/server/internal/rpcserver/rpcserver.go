// Package rpcserver assembles the production-shaped gRPC server: the catalog
// service, the standard health service, interceptors, resource limits,
// keepalive policy, and a graceful shutdown with a hard deadline.
package rpcserver

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/reflection"

	ecommercev1 "github.com/brayomumo/Psychic-compendium/g_to_RPC/server/gen/ecommerce/v1"
	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/catalog"
	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/interceptors"
	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/service"
)

// Defaults and bounds for Config.
const (
	DefaultMaxRecvMsgBytes      = 1 << 20 // 1 MiB; gRPC's own default is 4 MiB
	MinMaxRecvMsgBytes          = 1 << 10
	MaxMaxRecvMsgBytes          = 64 << 20
	DefaultMaxConcurrentStreams = 100

	// KeepaliveMinTime is the most often a client may ping. A client pinging
	// more often is disconnected with GOAWAY "too_many_pings", so clients
	// must be configured to ping no more often than this.
	KeepaliveMinTime = 10 * time.Second
)

// Config configures a Server. Zero numeric fields take the defaults above.
type Config struct {
	// Catalog backs the service. Required.
	Catalog *catalog.Catalog
	// Logger receives one line per RPC and every unexpected error. Required.
	Logger *slog.Logger
	// MaxRecvMsgBytes is the largest request message accepted; larger ones
	// fail with RESOURCE_EXHAUSTED before reaching the handler.
	MaxRecvMsgBytes int
	// MaxConcurrentStreams caps concurrent RPCs per client connection.
	MaxConcurrentStreams uint32
	// Reflection registers the server reflection service, which lets tools
	// such as grpcurl discover the API. It exposes the schema, so it is off
	// unless asked for.
	Reflection bool
	// ExtraUnary and ExtraStream run inside the standard interceptors,
	// closest to the handler. Tests use them to observe handler results.
	ExtraUnary  []grpc.UnaryServerInterceptor
	ExtraStream []grpc.StreamServerInterceptor
}

// Validate reports every invalid field at once.
func (c Config) Validate() error {
	var errs []error
	if c.Catalog == nil {
		errs = append(errs, errors.New("catalog is required"))
	}
	if c.Logger == nil {
		errs = append(errs, errors.New("logger is required"))
	}
	if c.MaxRecvMsgBytes != 0 && (c.MaxRecvMsgBytes < MinMaxRecvMsgBytes || c.MaxRecvMsgBytes > MaxMaxRecvMsgBytes) {
		errs = append(errs, fmt.Errorf("max receive message size must be in [%d, %d] bytes, got %d",
			MinMaxRecvMsgBytes, MaxMaxRecvMsgBytes, c.MaxRecvMsgBytes))
	}
	return errors.Join(errs...)
}

// Server is a gRPC server for ProductCatalogService plus health checking.
type Server struct {
	grpc   *grpc.Server
	health *health.Server
}

// New builds a Server. It does not listen; call Serve.
func New(cfg Config) (*Server, error) {
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("invalid config: %w", err)
	}
	if cfg.MaxRecvMsgBytes == 0 {
		cfg.MaxRecvMsgBytes = DefaultMaxRecvMsgBytes
	}
	if cfg.MaxConcurrentStreams == 0 {
		cfg.MaxConcurrentStreams = DefaultMaxConcurrentStreams
	}
	unary := append([]grpc.UnaryServerInterceptor{
		interceptors.UnaryRequestID(),
		interceptors.UnaryLogging(cfg.Logger),
		interceptors.UnaryRecovery(cfg.Logger),
	}, cfg.ExtraUnary...)
	stream := append([]grpc.StreamServerInterceptor{
		interceptors.StreamRequestID(),
		interceptors.StreamLogging(cfg.Logger),
		interceptors.StreamRecovery(cfg.Logger),
	}, cfg.ExtraStream...)

	gs := grpc.NewServer(
		grpc.ChainUnaryInterceptor(unary...),
		grpc.ChainStreamInterceptor(stream...),
		grpc.MaxRecvMsgSize(cfg.MaxRecvMsgBytes),
		grpc.MaxConcurrentStreams(cfg.MaxConcurrentStreams),
		grpc.KeepaliveParams(keepalive.ServerParameters{
			// Close connections idle this long, so abandoned clients do not
			// hold resources forever.
			MaxConnectionIdle: 5 * time.Minute,
			// Ping an idle client after this long, and drop it if the ping is
			// not answered in time: this detects dead peers behind NAT.
			Time:    2 * time.Minute,
			Timeout: 20 * time.Second,
		}),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             KeepaliveMinTime,
			PermitWithoutStream: true,
		}),
	)
	ecommercev1.RegisterProductCatalogServiceServer(gs, service.New(cfg.Catalog, cfg.Logger))

	hs := health.NewServer()
	hs.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	hs.SetServingStatus(ecommercev1.ProductCatalogService_ServiceDesc.ServiceName, healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(gs, hs)

	if cfg.Reflection {
		reflection.Register(gs)
	}
	return &Server{grpc: gs, health: hs}, nil
}

// Serve accepts connections on lis until Shutdown. It returns nil after a
// shutdown and the accept error otherwise.
func (s *Server) Serve(lis net.Listener) error {
	if err := s.grpc.Serve(lis); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
		return err
	}
	return nil
}

// Shutdown stops the server gracefully: health checks report NOT_SERVING (so
// load balancers stop routing here), new RPCs are refused, and in-flight
// RPCs run to completion. If ctx ends first, the remaining RPCs are
// cancelled and Shutdown returns ctx.Err(). Either way the server is
// stopped when Shutdown returns.
func (s *Server) Shutdown(ctx context.Context) error {
	s.health.Shutdown()
	done := make(chan struct{})
	go func() {
		s.grpc.GracefulStop()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		s.grpc.Stop() // cancels in-flight RPCs and closes every connection
		<-done
		return ctx.Err()
	}
}
