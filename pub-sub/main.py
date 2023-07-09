from time import sleep
import pika
import os
import json
import logging

from pika.exchange_type import ExchangeType

from consumer import Consumer
from utils import Job, Manager

logger = logging.getLogger(__name__)
SERVER_NAME = os.getenv("SERVER_NAME", "localhost")


class Publisher:
    """Publish messages to an exchange. The exchange is responsible for
    recieving messages from the publisher and pushing to respective
    queues
    """

    def __init__(self, connection_string: str, server_name: str):
        self.param = pika.URLParameters(connection_string)
        self.server_name = server_name
        self._connect()

    def _connect(self):
        self.connection = pika.BlockingConnection(self.param)
        self.channel = self.connection.channel()

        self.channel.exchange_declare(
            exchange=self.server_name, exchange_type=ExchangeType.direct
        )
        print("Initialized publisher")

    def _publish(self, method: str, body: dict) -> bool:
        properties = pika.BasicProperties(method)

        self.channel.basic_publish(
            exchange=self.server_name,
            routing_key=self.server_name,
            body=json.dumps(body),
            properties=properties,
        )

        print(f"published body: {body}")
        return True

    def kimbia(self, method: str):
        """Publish bulk message wrapped in an iterator
        :param str method: This is the method to be applied to all
                            the data published.
        :param iter iterator: This is an iterator from which the
                            data is fetched from.
        """
        for data in generate_dummy_data():
            self._publish(method, data)


def generate_dummy_data():
    for i in range(100, 500):
        body = {
            "id": i,
            "name": f"Brian mumo-{i}",
            "email": f"brian.mumo.{i}@mail.com",
            "version": "1.0.0",
        }
        yield body


if __name__ == "__main__":
    # start publisher
    connection_string = os.getenv("RABBITMQ_CONN_STRING")

    pub = Publisher(connection_string, SERVER_NAME)
    consumer = Consumer(connection_string, SERVER_NAME)

    jobs = [Job(pub, True, "User_Publisher"), Job(consumer)]
    threadManager = Manager(jobs, 1)
    threadManager.run()
    # while True:
    #     try:
    #         pub.publish_all("user_published", generate_dummy_data())
    #         sleep(0.5)
    #     except KeyboardInterrupt as e:
    #         print("Shutting down consumer")
    #         sleep(2)
    #         os._exit(0)
    #     except Exception as exp:
    #         print(f"Exception occured: {exp}")
    #         sleep(2)
    #         os._exit(1)
