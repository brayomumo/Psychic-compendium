package main

import (
	"context"
	"fmt"
	"log/slog"
	pb "productserver/ecommerce"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)


type server struct{
	 pb.UnimplementedProductInfoServer
	productMap map[string]*pb.Product
	logger *slog.Logger
}

// TODO: Add constructor for new server

func (s *server) AddProduct(
	ctx context.Context,
	in *pb.Product,
)(*pb.ProductID, error){
	out := uuid.New()
	in.Id = out.String()
	if s.productMap == nil{
		s.productMap = make(map[string]*pb.Product)
	}
	s.productMap[in.Id] = in
	fmt.Println(s.productMap)
	return &pb.ProductID{Value: in.Id}, status.New(codes.OK, "").Err()
}

func (s *server) GetProduct(ctx context.Context, in *pb.ProductID)(*pb.Product, error){
	value, exists := s.productMap[in.Value]

	if exists{
		return value, status.New(codes.OK, "").Err()
	}
	return nil, status.Errorf(codes.NotFound, "Product %s does not Exist.", in.Value)
}