import logging
import logging.config
import sys
import uuid
import grpc
from faker import Faker
import productInfo_pb2 as pb
import productInfo_pb2_grpc as pb_rpc

# because in a fake world, no ones needs realiness
chocha = Faker()

# beause we treasure beautiful terminals
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
LOGGER.addHandler(logging.StreamHandler(sys.stdout))


def _build_product(id):
    """
    Given an id, this returns a valid Product RPC message"""
    return pb.Product(
        id= id,
        name= chocha.name(),
        description=chocha.sentence()
    )


def create_products(stub, num=100):
    """abstracts creation of a number of products(num)"""
    for _ in range(num):
        try:
            id  = str(uuid.uuid4())
            product = _build_product(id)
            response = stub.addProduct(product)
            yield response.value
        except grpc.RpcError as rpc_error:
            LOGGER.error(f"RPC error: {rpc_error.code()}")
            continue

        except Exception as e:
            LOGGER.error(f"Failed to create product - {e}", exc_info=True)
            break


def product_details(stub, guid):
    """Get details of a product using guid"""
    try:
        product = stub.getProduct(pb.ProductID(
            value=guid
        ))
        LOGGER.info(f"Found product {product}")
    except Exception as e:
        LOGGER.error(f"Failed to get product - {e}", exc_info=True)


def main():
    """since we're used to GO :)"""
    try:
        channel = grpc.insecure_channel("localhost:50051")
        stub = pb_rpc.ProductInfoStub(channel)
        LOGGER.info("Client connected to server successfully")

        n = 1000
        LOGGER.debug(f"Creating {n} products")
        products = list(create_products(stub, num=n))
        assert len(products) == n, "Failed to create all products"

        for i in range(0, n, 10):
            product_id = products[i]
            product_details(stub, product_id)
    except Exception as e:
        LOGGER.error(f"Client Failed to connect to Server: {e}", exc_info=True)


    
if __name__ == "__main__":
    main()