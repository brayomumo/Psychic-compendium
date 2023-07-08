import os
import json
from time import sleep
import pika


SERVER_NAME = os.getenv("SERVER_NAME", "localhost")


class Consumer:
    def __init__(self, conn_string: str, server_name: str):
        self.param = pika.URLParameters(conn_string)
        self.server_name = server_name
        self.queue = server_name

    def process_body(self, channel, method, properties, body):
        print(f"recieved body: {body}")
        print(f"Method: {method} \nChannel: {channel} \nProperties: {properties}")

    def kimbia(self):
        # create connection
        self.connection = pika.BlockingConnection(self.param)
        self.channel = self.connection.channel()

        # declare queue
        self.channel.queue_declare(queue=self.queue)

        # marry queue to the exchange
        self.channel.queue_bind(
            exchange=self.server_name,
            queue=self.server_name,
            routing_key=self.server_name,
        )

        # consume data
        self.channel.basic_consume(
            queue=self.queue, on_message_callback=self.process_body, auto_ack=True
        )

        self.channel.start_consuming()
        self.channel.close()


if __name__ == "__main__":
    connection_string = os.getenv("RABBITMQ_CONN_STRING")
    consumer = Consumer(connection_string)
    while True:
        try:
            consumer.consume()
        except KeyboardInterrupt as exit:
            print("Shutting down Consumer!")
            sleep(1)
            break
        except Exception as exc:
            print(f"Exception occured: {exc}")
            os._exit(1)
