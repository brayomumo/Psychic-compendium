package main

import (
	"fmt"
	"log/slog"
	"net"
	"os"
	"productserver/ecommerce"

	"google.golang.org/grpc"
)


const (
	port = ":50051"
)


func main() {
	opts := &slog.HandlerOptions{
        Level: slog.LevelDebug,
    }

    handler := slog.NewJSONHandler(os.Stdout, opts)
	logger := slog.New(handler)

	lis, err := net.Listen("tcp", port)
	if err != nil{
		msg := fmt.Sprintf("Failed to listen: %s", err)
		logger.Error(msg)
		return
	}

	// create new grpc server
	s := grpc.NewServer()
	logger.Info("Server up and ready for connections")

	// register service
	ecommerce.RegisterProductInfoServer(s, &server{logger: logger})
	if err := s.Serve(lis); err != nil{
		msg := fmt.Sprintf("server failed to serve: %s", err)
		logger.Error(msg)
		return
	}
}