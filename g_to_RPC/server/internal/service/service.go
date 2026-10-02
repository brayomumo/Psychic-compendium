// Package service implements ecommerce.v1.ProductCatalogService on top of a
// catalog.Catalog. It translates between protobuf messages and the domain,
// and turns domain errors into gRPC status codes with machine-readable
// details (google.rpc.BadRequest, ResourceInfo, PreconditionFailure,
// QuotaFailure), so clients can react to the cause, not parse a message.
package service

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"unicode/utf8"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/protoadapt"

	ecommercev1 "github.com/brayomumo/Psychic-compendium/g_to_RPC/server/gen/ecommerce/v1"
	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/catalog"
)

// MaxBulkProducts bounds one BulkAddProducts call, so a client cannot make
// the server buffer an unbounded stream before validating it.
const MaxBulkProducts = 10_000

// productResource is the ResourceInfo.resource_type for products.
const productResource = "ecommerce.v1.Product"

// Service serves ProductCatalogService. Its methods are called concurrently
// by gRPC; all shared state lives in the catalog, which is safe for that.
type Service struct {
	ecommercev1.UnimplementedProductCatalogServiceServer

	catalog *catalog.Catalog
	logger  *slog.Logger
}

// New returns a Service backed by c that logs unexpected errors to logger.
func New(c *catalog.Catalog, logger *slog.Logger) *Service {
	return &Service{catalog: c, logger: logger}
}

// AddProduct is a unary RPC.
func (s *Service) AddProduct(ctx context.Context, req *ecommercev1.AddProductRequest) (*ecommercev1.AddProductResponse, error) {
	p, err := s.catalog.Add(fromProto(req.GetProduct()), req.GetRequestId())
	if err != nil {
		return nil, s.toStatus(ctx, err, "product.", req.GetProduct().GetName(), req.GetRequestId())
	}
	return &ecommercev1.AddProductResponse{Product: toProto(p)}, nil
}

// GetProduct is a unary RPC.
func (s *Service) GetProduct(ctx context.Context, req *ecommercev1.GetProductRequest) (*ecommercev1.GetProductResponse, error) {
	if req.GetId() == "" {
		return nil, badRequest("id", "must not be empty")
	}
	p, err := s.catalog.Get(req.GetId())
	if err != nil {
		return nil, s.toStatus(ctx, err, "", req.GetId(), "")
	}
	return &ecommercev1.GetProductResponse{Product: toProto(p)}, nil
}

// SearchProducts is a server-streaming RPC. It checks the stream's context
// before each send, so a client that cancels or runs out of deadline stops
// the work instead of the server streaming into the void.
func (s *Service) SearchProducts(req *ecommercev1.SearchProductsRequest, stream ecommercev1.ProductCatalogService_SearchProductsServer) error {
	if n := utf8.RuneCountInString(req.GetQuery()); n > catalog.MaxQueryLen {
		return badRequest("query", fmt.Sprintf("must be at most %d characters, got %d", catalog.MaxQueryLen, n))
	}
	for _, p := range s.catalog.Search(req.GetQuery()) {
		if err := stream.Context().Err(); err != nil {
			return status.FromContextError(err).Err()
		}
		if err := stream.Send(&ecommercev1.SearchProductsResponse{Product: toProto(p)}); err != nil {
			return err // the stream is broken; gRPC already knows why
		}
	}
	return nil
}

// BulkAddProducts is a client-streaming RPC. It reads the whole stream
// (bounded by MaxBulkProducts), then stores it atomically.
func (s *Service) BulkAddProducts(stream ecommercev1.ProductCatalogService_BulkAddProductsServer) error {
	var batch []catalog.NewProduct
	for {
		req, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err // cancelled, deadline exceeded or transport failure
		}
		if len(batch) == MaxBulkProducts {
			return badRequest("products", fmt.Sprintf("at most %d products per call", MaxBulkProducts))
		}
		batch = append(batch, fromProto(req.GetProduct()))
	}
	stored, err := s.catalog.AddAll(batch)
	if err != nil {
		return s.toStatus(stream.Context(), err, "products", "", "")
	}
	resp := &ecommercev1.BulkAddProductsResponse{Products: make([]*ecommercev1.Product, len(stored))}
	for i, p := range stored {
		resp.Products[i] = toProto(p)
	}
	return stream.SendAndClose(resp)
}

// QuoteProducts is a bidirectional-streaming RPC. Each request is answered
// as soon as it arrives; a bad item gets an error status in its response
// rather than ending the stream for every other item.
func (s *Service) QuoteProducts(stream ecommercev1.ProductCatalogService_QuoteProductsServer) error {
	for {
		req, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			return nil // the client finished sending; we have answered everything
		}
		if err != nil {
			return err
		}
		resp := &ecommercev1.QuoteProductsResponse{ProductId: req.GetProductId(), Quantity: req.GetQuantity()}
		total, err := s.catalog.Quote(req.GetProductId(), req.GetQuantity())
		switch {
		case err == nil:
			resp.Status, resp.TotalCents = ecommercev1.QuoteStatus_QUOTE_STATUS_OK, total
		case errors.Is(err, catalog.ErrInvalidQuantity):
			resp.Status = ecommercev1.QuoteStatus_QUOTE_STATUS_INVALID_QUANTITY
		case errors.Is(err, catalog.ErrNotFound):
			resp.Status = ecommercev1.QuoteStatus_QUOTE_STATUS_NOT_FOUND
		default:
			return s.toStatus(stream.Context(), err, "", "", "")
		}
		if err := stream.Send(resp); err != nil {
			return err
		}
	}
}

// toStatus maps a domain error to a gRPC status. fieldPrefix turns domain
// field names into request paths ("product." + "name"); resource names the
// product involved; requestID is the AddProduct idempotency key, if any.
// Unexpected errors become INTERNAL with a generic message: their detail is
// logged, never sent to the client.
func (s *Service) toStatus(ctx context.Context, err error, fieldPrefix, resource, requestID string) error {
	var verr *catalog.ValidationError
	switch {
	case errors.As(err, &verr):
		br := &errdetails.BadRequest{}
		for _, v := range verr.Violations {
			field := fieldPrefix + v.Field
			if v.Index >= 0 {
				field = fmt.Sprintf("%s[%d].%s", fieldPrefix, v.Index, v.Field)
			}
			br.FieldViolations = append(br.FieldViolations,
				&errdetails.BadRequest_FieldViolation{Field: field, Description: v.Description})
		}
		return withDetails(codes.InvalidArgument, err.Error(), br)
	case errors.Is(err, catalog.ErrNotFound):
		return withDetails(codes.NotFound, err.Error(),
			&errdetails.ResourceInfo{ResourceType: productResource, ResourceName: resource})
	case errors.Is(err, catalog.ErrAlreadyExists):
		return withDetails(codes.AlreadyExists, err.Error(),
			&errdetails.ResourceInfo{ResourceType: productResource, ResourceName: resource})
	case errors.Is(err, catalog.ErrRequestIDReused):
		return withDetails(codes.FailedPrecondition, err.Error(), &errdetails.PreconditionFailure{
			Violations: []*errdetails.PreconditionFailure_Violation{{
				Type: "REQUEST_ID_REUSED", Subject: requestID,
				Description: "send a new request_id for a different product",
			}},
		})
	case errors.Is(err, catalog.ErrFull):
		return withDetails(codes.ResourceExhausted, err.Error(), &errdetails.QuotaFailure{
			Violations: []*errdetails.QuotaFailure_Violation{{Subject: "catalog", Description: err.Error()}},
		})
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		return status.FromContextError(err).Err()
	default:
		s.logger.ErrorContext(ctx, "unexpected error", "error", err)
		return status.Error(codes.Internal, "internal error")
	}
}

func badRequest(field, description string) error {
	return withDetails(codes.InvalidArgument, field+": "+description, &errdetails.BadRequest{
		FieldViolations: []*errdetails.BadRequest_FieldViolation{{Field: field, Description: description}},
	})
}

// withDetails builds a status with one detail message. Attaching details
// only fails if the detail cannot be marshalled, which would be a bug; the
// plain status is still correct, so fall back to it.
func withDetails(code codes.Code, msg string, detail protoadapt.MessageV1) error {
	st := status.New(code, msg)
	if withDetail, err := st.WithDetails(detail); err == nil {
		return withDetail.Err()
	}
	return st.Err()
}

func fromProto(p *ecommercev1.NewProduct) catalog.NewProduct {
	return catalog.NewProduct{Name: p.GetName(), Description: p.GetDescription(), PriceCents: p.GetPriceCents()}
}

func toProto(p catalog.Product) *ecommercev1.Product {
	return &ecommercev1.Product{Id: p.ID, Name: p.Name, Description: p.Description, PriceCents: p.PriceCents}
}
