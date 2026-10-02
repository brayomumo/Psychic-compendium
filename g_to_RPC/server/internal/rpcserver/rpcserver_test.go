package rpcserver

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	ecommercev1 "github.com/brayomumo/Psychic-compendium/g_to_RPC/server/gen/ecommerce/v1"
	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/catalog"
	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/interceptors"
	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/service"
)

// testTimeout guards every RPC in these tests against hanging; none should
// come close to it.
const testTimeout = 10 * time.Second

var uuidPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

// syncBuffer is a bytes.Buffer safe for the concurrent writes of a logger.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

type harness struct {
	srv    *Server
	lis    *bufconn.Listener
	client ecommercev1.ProductCatalogServiceClient
	conn   *grpc.ClientConn
	logs   *syncBuffer
	served chan error
}

// start runs a Server on an in-memory listener and connects a client to it.
// mutate may adjust the config first.
func start(t *testing.T, mutate func(*Config)) *harness {
	t.Helper()
	logs := &syncBuffer{}
	cfg := Config{
		Catalog: catalog.New(100, nil),
		Logger:  slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug})),
	}
	if mutate != nil {
		mutate(&cfg)
	}
	srv, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	h := &harness{srv: srv, lis: bufconn.Listen(1 << 20), logs: logs, served: make(chan error, 1)}
	go func() { h.served <- srv.Serve(h.lis) }()
	h.conn = h.dial(t)
	h.client = ecommercev1.NewProductCatalogServiceClient(h.conn)
	t.Cleanup(func() {
		_ = h.conn.Close()
		ctx, cancel := context.WithTimeout(context.Background(), testTimeout)
		defer cancel()
		_ = srv.Shutdown(ctx)
		if err := <-h.served; err != nil {
			t.Errorf("Serve returned %v, want nil after Shutdown", err)
		}
	})
	return h
}

func (h *harness) dial(t *testing.T) *grpc.ClientConn {
	t.Helper()
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return h.lis.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	return conn
}

func rpcContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), testTimeout)
	t.Cleanup(cancel)
	return ctx
}

func (h *harness) add(t *testing.T, name string, price int64) *ecommercev1.Product {
	t.Helper()
	resp, err := h.client.AddProduct(rpcContext(t), &ecommercev1.AddProductRequest{
		Product: &ecommercev1.NewProduct{Name: name, Description: "about " + name, PriceCents: price},
	})
	if err != nil {
		t.Fatalf("AddProduct(%q): %v", name, err)
	}
	return resp.GetProduct()
}

func wantCode(t *testing.T, err error, want codes.Code) {
	t.Helper()
	if got := status.Code(err); got != want {
		t.Fatalf("status code = %v (%v), want %v", got, err, want)
	}
}

// detail returns the first status detail of type T attached to err.
func detail[T any](t *testing.T, err error) T {
	t.Helper()
	for _, d := range status.Convert(err).Details() {
		if v, ok := d.(T); ok {
			return v
		}
	}
	var zero T
	t.Fatalf("status %v carries no %T detail", err, zero)
	return zero
}

func TestAddAndGetProduct(t *testing.T) {
	h := start(t, nil)
	added := h.add(t, "  Kettle ", 2500)
	if !uuidPattern.MatchString(added.GetId()) {
		t.Fatalf("ID = %q, want a server-generated UUIDv4", added.GetId())
	}
	if added.GetName() != "Kettle" {
		t.Fatalf("Name = %q, want trimmed %q", added.GetName(), "Kettle")
	}
	got, err := h.client.GetProduct(rpcContext(t), &ecommercev1.GetProductRequest{Id: added.GetId()})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetProduct().String() != added.String() {
		t.Fatalf("GetProduct = %v, want %v", got.GetProduct(), added)
	}
}

// Regression: the first server overwrote whatever ID the client sent. Now
// the request has no ID field at all, and retries are made safe by an
// idempotency key instead.
func TestAddProduct_RegressionServerOverwroteClientID(t *testing.T) {
	if f := (&ecommercev1.NewProduct{}).ProtoReflect().Descriptor().Fields().ByName("id"); f != nil {
		t.Fatal("NewProduct has an id field; IDs must be server-assigned only")
	}
	h := start(t, nil)
	req := &ecommercev1.AddProductRequest{
		Product:   &ecommercev1.NewProduct{Name: "Kettle", PriceCents: 1},
		RequestId: "retry-me",
	}
	first, err := h.client.AddProduct(rpcContext(t), req)
	if err != nil {
		t.Fatal(err)
	}
	second, err := h.client.AddProduct(rpcContext(t), req)
	if err != nil {
		t.Fatalf("retry with the same request_id: %v", err)
	}
	if first.GetProduct().GetId() != second.GetProduct().GetId() {
		t.Fatalf("retry created %q, want the original %q", second.GetProduct().GetId(), first.GetProduct().GetId())
	}
}

func TestAddProduct_InvalidArgumentCarriesFieldViolations(t *testing.T) {
	h := start(t, nil)
	_, err := h.client.AddProduct(rpcContext(t), &ecommercev1.AddProductRequest{
		Product: &ecommercev1.NewProduct{Name: " ", PriceCents: -1},
	})
	wantCode(t, err, codes.InvalidArgument)
	var fields []string
	for _, v := range detail[*errdetails.BadRequest](t, err).GetFieldViolations() {
		fields = append(fields, v.GetField())
	}
	if got := strings.Join(fields, ","); got != "product.name,product.price_cents" {
		t.Fatalf("field violations = %s, want product.name,product.price_cents", got)
	}
}

func TestGetProduct_Errors(t *testing.T) {
	h := start(t, nil)
	_, err := h.client.GetProduct(rpcContext(t), &ecommercev1.GetProductRequest{Id: "missing"})
	wantCode(t, err, codes.NotFound)
	if info := detail[*errdetails.ResourceInfo](t, err); info.GetResourceName() != "missing" ||
		info.GetResourceType() != "ecommerce.v1.Product" {
		t.Fatalf("ResourceInfo = %v, want the missing product", info)
	}
	_, err = h.client.GetProduct(rpcContext(t), &ecommercev1.GetProductRequest{})
	wantCode(t, err, codes.InvalidArgument)
	if f := detail[*errdetails.BadRequest](t, err).GetFieldViolations()[0].GetField(); f != "id" {
		t.Fatalf("violation field = %q, want id", f)
	}
}

func TestAddProduct_ConflictCodes(t *testing.T) {
	h := start(t, func(c *Config) { c.Catalog = catalog.New(2, nil) })
	h.add(t, "Kettle", 1)

	_, err := h.client.AddProduct(rpcContext(t), &ecommercev1.AddProductRequest{
		Product: &ecommercev1.NewProduct{Name: "KETTLE"}})
	wantCode(t, err, codes.AlreadyExists)
	if name := detail[*errdetails.ResourceInfo](t, err).GetResourceName(); name != "KETTLE" {
		t.Fatalf("ResourceInfo name = %q, want KETTLE", name)
	}

	req := &ecommercev1.AddProductRequest{Product: &ecommercev1.NewProduct{Name: "Toaster"}, RequestId: "k1"}
	if _, err := h.client.AddProduct(rpcContext(t), req); err != nil {
		t.Fatal(err)
	}
	req.Product.Name = "Blender"
	_, err = h.client.AddProduct(rpcContext(t), req)
	wantCode(t, err, codes.FailedPrecondition)
	if v := detail[*errdetails.PreconditionFailure](t, err).GetViolations()[0]; v.GetSubject() != "k1" {
		t.Fatalf("PreconditionFailure subject = %q, want the reused key", v.GetSubject())
	}

	_, err = h.client.AddProduct(rpcContext(t), &ecommercev1.AddProductRequest{
		Product: &ecommercev1.NewProduct{Name: "Blender"}})
	wantCode(t, err, codes.ResourceExhausted)
	detail[*errdetails.QuotaFailure](t, err)
}

func TestSearchProducts_StreamsMatchesInNameOrder(t *testing.T) {
	h := start(t, nil)
	for _, name := range []string{"Teapot", "Kettle", "Toaster", "Electric kettle"} {
		h.add(t, name, 1)
	}
	stream, err := h.client.SearchProducts(rpcContext(t), &ecommercev1.SearchProductsRequest{Query: "KETTLE"})
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for {
		resp, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		names = append(names, resp.GetProduct().GetName())
	}
	if got := strings.Join(names, ","); got != "Electric kettle,Kettle" {
		t.Fatalf("search results = %s, want Electric kettle,Kettle", got)
	}

	long, err := h.client.SearchProducts(rpcContext(t),
		&ecommercev1.SearchProductsRequest{Query: strings.Repeat("q", catalog.MaxQueryLen+1)})
	if err == nil {
		_, err = long.Recv()
	}
	wantCode(t, err, codes.InvalidArgument)
}

func TestBulkAddProducts_StoresAllInOrder(t *testing.T) {
	h := start(t, nil)
	stream, err := h.client.BulkAddProducts(rpcContext(t))
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"a", "b", "c"} {
		if err := stream.Send(&ecommercev1.BulkAddProductsRequest{Product: &ecommercev1.NewProduct{Name: name}}); err != nil {
			t.Fatal(err)
		}
	}
	resp, err := stream.CloseAndRecv()
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, p := range resp.GetProducts() {
		names = append(names, p.GetName())
	}
	if got := strings.Join(names, ","); got != "a,b,c" {
		t.Fatalf("bulk add stored %s, want a,b,c", got)
	}
}

func TestBulkAddProducts_IsAtomic(t *testing.T) {
	h := start(t, nil)
	stream, err := h.client.BulkAddProducts(rpcContext(t))
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"good", "", "also good"} {
		if err := stream.Send(&ecommercev1.BulkAddProductsRequest{Product: &ecommercev1.NewProduct{Name: name}}); err != nil {
			t.Fatal(err)
		}
	}
	_, err = stream.CloseAndRecv()
	wantCode(t, err, codes.InvalidArgument)
	if f := detail[*errdetails.BadRequest](t, err).GetFieldViolations()[0].GetField(); f != "products[1].name" {
		t.Fatalf("violation field = %q, want products[1].name", f)
	}
	search, err := h.client.SearchProducts(rpcContext(t), &ecommercev1.SearchProductsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := search.Recv(); !errors.Is(err, io.EOF) {
		t.Fatalf("catalog not empty after a rejected batch: Recv = %v", err)
	}
}

func TestBulkAddProducts_RejectsTooMany(t *testing.T) {
	h := start(t, func(c *Config) { c.Catalog = catalog.New(service.MaxBulkProducts+10, nil) })
	stream, err := h.client.BulkAddProducts(rpcContext(t))
	if err != nil {
		t.Fatal(err)
	}
	for i := range service.MaxBulkProducts + 1 {
		err := stream.Send(&ecommercev1.BulkAddProductsRequest{Product: &ecommercev1.NewProduct{Name: fmt.Sprint(i)}})
		if errors.Is(err, io.EOF) {
			break // the server has already rejected the stream
		}
		if err != nil {
			t.Fatal(err)
		}
	}
	_, err = stream.CloseAndRecv()
	wantCode(t, err, codes.InvalidArgument)
}

func TestQuoteProducts_AnswersEachItemAndKeepsGoing(t *testing.T) {
	h := start(t, nil)
	kettle := h.add(t, "Kettle", 2500)
	stream, err := h.client.QuoteProducts(rpcContext(t))
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		id     string
		qty    int64
		status ecommercev1.QuoteStatus
		total  int64
	}{
		{kettle.GetId(), 2, ecommercev1.QuoteStatus_QUOTE_STATUS_OK, 5000},
		{"missing", 1, ecommercev1.QuoteStatus_QUOTE_STATUS_NOT_FOUND, 0},
		{kettle.GetId(), 0, ecommercev1.QuoteStatus_QUOTE_STATUS_INVALID_QUANTITY, 0},
		{kettle.GetId(), 3, ecommercev1.QuoteStatus_QUOTE_STATUS_OK, 7500},
	}
	for _, c := range cases {
		// Bidirectional: each answer arrives before the next question is sent.
		if err := stream.Send(&ecommercev1.QuoteProductsRequest{ProductId: c.id, Quantity: c.qty}); err != nil {
			t.Fatal(err)
		}
		resp, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if resp.GetStatus() != c.status || resp.GetTotalCents() != c.total || resp.GetProductId() != c.id {
			t.Fatalf("quote(%s, %d) = %v, want status %v total %d", c.id, c.qty, resp, c.status, c.total)
		}
	}
	if err := stream.CloseSend(); err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); !errors.Is(err, io.EOF) {
		t.Fatalf("Recv after CloseSend = %v, want io.EOF", err)
	}
}

// observeStreams returns a config mutator that reports each streaming
// handler's result, seen from inside the server.
func observeStreams(results chan<- error) func(*Config) {
	return func(c *Config) {
		c.ExtraStream = append(c.ExtraStream,
			func(srv any, ss grpc.ServerStream, _ *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
				err := handler(srv, ss)
				results <- err
				return err
			})
	}
}

func TestQuoteProducts_ClientCancellationReachesServer(t *testing.T) {
	results := make(chan error, 1)
	h := start(t, observeStreams(results))
	kettle := h.add(t, "Kettle", 1)
	ctx, cancel := context.WithCancel(rpcContext(t))
	stream, err := h.client.QuoteProducts(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&ecommercev1.QuoteProductsRequest{ProductId: kettle.GetId(), Quantity: 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatal(err)
	}
	cancel() // the server is now blocked waiting for the next item
	select {
	case err := <-results:
		wantCode(t, err, codes.Canceled)
	case <-time.After(testTimeout):
		t.Fatal("server handler never saw the cancellation")
	}
}

func TestQuoteProducts_DeadlineEndsIdleStream(t *testing.T) {
	results := make(chan error, 1)
	deadlines := make(chan time.Duration, 1)
	h := start(t, func(c *Config) {
		observeStreams(results)(c)
		c.ExtraStream = append(c.ExtraStream,
			func(srv any, ss grpc.ServerStream, _ *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
				if d, ok := ss.Context().Deadline(); ok {
					deadlines <- time.Until(d)
				} else {
					deadlines <- -1
				}
				return handler(srv, ss)
			})
	})
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	stream, err := h.client.QuoteProducts(ctx)
	if err != nil {
		t.Fatal(err)
	}
	// The client's deadline travels in the grpc-timeout header and becomes
	// the server handler's context deadline.
	if left := <-deadlines; left <= 0 || left > 200*time.Millisecond {
		t.Fatalf("server context deadline in %v, want the client's deadline (0, 200ms]", left)
	}
	_, err = stream.Recv() // nothing sent: only the deadline can end this
	wantCode(t, err, codes.DeadlineExceeded)
	select {
	case err := <-results:
		// Two correct outcomes race: the server's own copy of the deadline
		// fires (DEADLINE_EXCEEDED), or the client's RST_STREAM, sent when
		// its deadline fires, arrives first (CANCELLED).
		if c := status.Code(err); c != codes.DeadlineExceeded && c != codes.Canceled {
			t.Fatalf("server handler ended with %v, want DeadlineExceeded or Canceled", err)
		}
	case <-time.After(testTimeout):
		t.Fatal("deadline never reached the server handler")
	}
}

func TestExpiredDeadlineFailsWithoutReachingServer(t *testing.T) {
	h := start(t, nil)
	ctx, cancel := context.WithTimeout(context.Background(), -time.Second)
	defer cancel()
	_, err := h.client.GetProduct(ctx, &ecommercev1.GetProductRequest{Id: "x"})
	wantCode(t, err, codes.DeadlineExceeded)
}

func TestOversizedRequestIsResourceExhausted(t *testing.T) {
	h := start(t, func(c *Config) { c.MaxRecvMsgBytes = 4 << 10 })
	_, err := h.client.AddProduct(rpcContext(t), &ecommercev1.AddProductRequest{
		Product: &ecommercev1.NewProduct{Name: "big", Description: strings.Repeat("x", 8<<10)}})
	wantCode(t, err, codes.ResourceExhausted)
}

func TestRequestIDIsEchoedOrGenerated(t *testing.T) {
	h := start(t, nil)
	tests := []struct {
		name     string
		incoming string
		want     func(string) bool
	}{
		{"client id adopted", "abc-123", func(s string) bool { return s == "abc-123" }},
		{"missing id generated", "", uuidPattern.MatchString},
		// gRPC transports already refuse control characters in ASCII
		// metadata, so a space is the unsafe value a client can still send.
		{"unsafe id replaced", "two words", uuidPattern.MatchString},
		{"oversized id replaced", strings.Repeat("a", 129), uuidPattern.MatchString},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ctx := rpcContext(t)
			if tt.incoming != "" {
				ctx = metadata.AppendToOutgoingContext(ctx, interceptors.RequestIDKey, tt.incoming)
			}
			var header metadata.MD
			_, _ = h.client.GetProduct(ctx, &ecommercev1.GetProductRequest{Id: "x"}, grpc.Header(&header))
			if got := header.Get(interceptors.RequestIDKey); len(got) != 1 || !tt.want(got[0]) {
				t.Fatalf("unary response header %s = %q", interceptors.RequestIDKey, got)
			}
			stream, err := h.client.SearchProducts(ctx, &ecommercev1.SearchProductsRequest{})
			if err != nil {
				t.Fatal(err)
			}
			streamHeader, err := stream.Header()
			if err != nil {
				t.Fatal(err)
			}
			if got := streamHeader.Get(interceptors.RequestIDKey); len(got) != 1 || !tt.want(got[0]) {
				t.Fatalf("stream response header %s = %q", interceptors.RequestIDKey, got)
			}
		})
	}
}

// Regression: the first server printed the whole product map on every add,
// so each request cost O(catalog size) in I/O. Now every RPC logs one line
// whose size does not grow with the data.
func TestLoggingWritesOneBoundedLinePerRPC_RegressionPrintedWholeMap(t *testing.T) {
	const adds = 300
	h := start(t, func(c *Config) { c.Catalog = catalog.New(adds, nil) })
	for i := range adds {
		h.add(t, fmt.Sprintf("product-%03d", i), 1)
	}
	var rpcLines int
	for _, line := range strings.Split(strings.TrimSpace(h.logs.String()), "\n") {
		if strings.Contains(line, "product-") {
			t.Fatalf("log line contains catalog data: %s", line)
		}
		if len(line) > 300 {
			t.Fatalf("log line is %d bytes, want a bounded size: %s", len(line), line)
		}
		if strings.Contains(line, "msg=rpc") {
			rpcLines++
		}
	}
	if rpcLines != adds {
		t.Fatalf("%d rpc log lines for %d RPCs, want one each", rpcLines, adds)
	}
}

func TestPanicInHandlerIsRecoveredAsInternal(t *testing.T) {
	h := start(t, func(c *Config) {
		c.ExtraUnary = append(c.ExtraUnary,
			func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
				if info.FullMethod == ecommercev1.ProductCatalogService_GetProduct_FullMethodName {
					panic("boom")
				}
				return handler(ctx, req)
			})
	})
	_, err := h.client.GetProduct(rpcContext(t), &ecommercev1.GetProductRequest{Id: "x"})
	wantCode(t, err, codes.Internal)
	if msg := status.Convert(err).Message(); strings.Contains(msg, "boom") {
		t.Fatalf("panic detail leaked to the client: %q", msg)
	}
	if !strings.Contains(h.logs.String(), "handler panicked") {
		t.Fatal("panic was not logged")
	}
	h.add(t, "still serving", 1) // one bad request must not take the server down
}

func TestConcurrentRPCs_RegressionUnguardedMapRace(t *testing.T) {
	const goroutines, perGoroutine = 16, 25
	h := start(t, nil)
	var wg sync.WaitGroup
	for g := range goroutines {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range perGoroutine {
				_, err := h.client.AddProduct(context.Background(), &ecommercev1.AddProductRequest{
					Product: &ecommercev1.NewProduct{Name: fmt.Sprintf("p-%d-%d", g, i)}})
				if err != nil && status.Code(err) != codes.ResourceExhausted {
					t.Errorf("AddProduct: %v", err)
				}
			}
		}()
	}
	wg.Wait()
}

func TestHealthTurnsNotServingOnShutdown(t *testing.T) {
	h := start(t, nil)
	health := healthpb.NewHealthClient(h.conn)
	resp, err := health.Check(rpcContext(t), &healthpb.HealthCheckRequest{
		Service: ecommercev1.ProductCatalogService_ServiceDesc.ServiceName})
	if err != nil || resp.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("Check = %v, %v; want SERVING", resp, err)
	}
	ctx, cancel := context.WithCancel(rpcContext(t))
	watch, err := health.Watch(ctx, &healthpb.HealthCheckRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if first, err := watch.Recv(); err != nil || first.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("first Watch update = %v, %v; want SERVING", first, err)
	}
	shutdown := make(chan error, 1)
	go func() { shutdown <- h.srv.Shutdown(context.Background()) }()
	// The open Watch stream is an in-flight RPC: graceful stop waits for it,
	// and meanwhile it delivers the status change.
	if next, err := watch.Recv(); err != nil || next.GetStatus() != healthpb.HealthCheckResponse_NOT_SERVING {
		t.Fatalf("Watch update during shutdown = %v, %v; want NOT_SERVING", next, err)
	}
	cancel()
	if err := <-shutdown; err != nil {
		t.Fatalf("Shutdown = %v, want nil once the last stream ended", err)
	}
}

func TestGracefulShutdownFinishesInFlightRPC(t *testing.T) {
	h := start(t, nil)
	kettle := h.add(t, "Kettle", 10)
	stream, err := h.client.QuoteProducts(rpcContext(t))
	if err != nil {
		t.Fatal(err)
	}
	quote := func() {
		t.Helper()
		if err := stream.Send(&ecommercev1.QuoteProductsRequest{ProductId: kettle.GetId(), Quantity: 1}); err != nil {
			t.Fatal(err)
		}
		if resp, err := stream.Recv(); err != nil || resp.GetTotalCents() != 10 {
			t.Fatalf("quote = %v, %v", resp, err)
		}
	}
	quote()

	shutdown := make(chan error, 1)
	go func() { shutdown <- h.srv.Shutdown(context.Background()) }()

	// New connections are refused once shutdown starts...
	late := ecommercev1.NewProductCatalogServiceClient(h.dial(t))
	lateCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if _, err := late.GetProduct(lateCtx, &ecommercev1.GetProductRequest{Id: kettle.GetId()}); err == nil {
		t.Fatal("a new connection was served after shutdown began")
	}
	// ...but the RPC already in flight keeps working,
	quote()
	select {
	case err := <-shutdown:
		t.Fatalf("Shutdown returned %v while an RPC was still in flight", err)
	default:
	}
	// and shutdown completes as soon as it ends.
	if err := stream.CloseSend(); err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); !errors.Is(err, io.EOF) {
		t.Fatalf("Recv after CloseSend = %v, want io.EOF", err)
	}
	if err := <-shutdown; err != nil {
		t.Fatalf("Shutdown = %v, want nil", err)
	}
}

func TestShutdownCancelsRPCsAfterDeadline(t *testing.T) {
	h := start(t, nil)
	kettle := h.add(t, "Kettle", 1)
	stream, err := h.client.QuoteProducts(rpcContext(t))
	if err != nil {
		t.Fatal(err)
	}
	// One round trip proves the RPC is in flight on the server, which is now
	// blocked waiting for the next item that never comes.
	if err := stream.Send(&ecommercev1.QuoteProductsRequest{ProductId: kettle.GetId(), Quantity: 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := h.srv.Shutdown(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Shutdown = %v, want context.DeadlineExceeded", err)
	}
	if _, err := stream.Recv(); status.Code(err) != codes.Unavailable && status.Code(err) != codes.Canceled {
		t.Fatalf("in-flight Recv after forced stop = %v, want Unavailable or Canceled", err)
	}
}

func TestConfigValidate(t *testing.T) {
	err := Config{MaxRecvMsgBytes: 1}.Validate()
	for _, want := range []string{"catalog is required", "logger is required", "max receive message size"} {
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("Validate() = %v, want it to mention %q", err, want)
		}
	}
	if _, err := New(Config{}); err == nil {
		t.Fatal("New(Config{}) succeeded, want an invalid config error")
	}
}
