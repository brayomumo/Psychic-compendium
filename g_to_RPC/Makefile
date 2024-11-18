build_proto_go:
	cd productinfo/service;\
	protoc --go_out=ecommerce --go_opt=paths=source_relative --go-grpc_out=ecommerce --go-grpc_opt=paths=source_relative  productInfo.proto


build_proto_py:
	python -m grpc_tools.protoc -I./protos --python_out=./client --pyi_out=./client --grpc_python_out=./client ./protos/productInfo.proto 

build_server:
	cd productserver;\
	go build -o ../bin/productserver 

run_server: build_server
	./bin/productserver