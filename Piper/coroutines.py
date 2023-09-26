import time
import typing
import logging
import datetime
from multiprocessing import Process
from concurrent.futures import ThreadPoolExecutor


LOGGER = logging.getLogger(__name__)


def coroutine(func):
    """This is a decorator to prime coroutine functions"""

    def wrapper(*args, **kwargs):
        f = func(*args, **kwargs)
        value = next(f)
        return f, value

    return wrapper


@coroutine
def consumer_handler(
    consumer_func: typing.Callable, consumer_name: str, num_of_consumers: int = 2
):
    """ "
    This is a coroutine used to pass data from the producer process to
    the consumer function. This eliminates the need for a shared storage
    between the producer process and consumer process and optmizes performance
    by about 200%, allowing for object sharing without serialization(e.g sharing
    of model objects without pickling).

    Args:
        consumer_func: callable - This is the function to be applied to the
                    incoming data.
        consumer_name: str - This is the name of the consumer. it's mainly used
                to identify consumer in logs.
        num_of_consumers: int - Number of consumer processes to server the
                consumer function. Default is 2
    """
    with ThreadPoolExecutor(
        max_workers=num_of_consumers, thread_name_prefix="consumer-"
    ) as executor:
        timer = time.time()
        while True:
            try:
                data = yield timer
                timer = data[-1]
                args = data[0]
                LOGGER.info(f"{consumer_name} handling {args}")
                # handle data in a threads to allow for multiple consumers
                # without blocking.
                executor.submit(consumer_func, *args)
            except (KeyboardInterrupt, SystemExit):
                LOGGER.warning(f"{consumer_name} Consumer Exiting")
                break
            except Exception as e:
                LOGGER.error(f"{consumer_name} Consumer got some error - {e}")
                break


def producer_handler(
    producer_name: str,
    producer_func: typing.Generator,
    producer_opts: dict,
    consumer_gen,
):
    """
    This enhances the producer function to add functionality for communication with
    consumer handler via a coroutine, the core part which abstracts the communication
    between the producer and the consumer.

    Args:
        producer_name: str - This is the name for the producer function, used mainly
                in the logs
        producer_func: callable - This is the generator function which should be used
            as the producer function.
        producer_opt: dict - These are the options used by the producer function.
        consumer_gen: Generator/Coroutine - This is the coroutine used as the
            communication link between the producer and the consumer function,
            it reduces the memory used between the 2 process and lets us share
            custom Data objects(e.g Model objects or Querysets) without pickling.
    """
    try:
        for data in producer_func(**producer_opts):
            consumer_gen.send(data)
    except Exception as e:
        LOGGER.error(f"{producer_name}: Error occured - {e}")


def advanced_manager(
    producer_name: str,
    producer_func: typing.Callable,
    consumer_name: str,
    consumer_func: typing.Callable,
    producer_wait_time: int = 5,
    producer_opts: typing.Dict = {},
):
    """
    This is the manager running on the main process and provides shared resources/
    functionality between the producer process and the consumer. These resources
    include the consumer generator which should be primed.
    This manager also spins up the producer process which runs in the background
    wrapped around the `producer_handler`. The producer handler adds the logic for
    sharing data to consumer workers via a generator.On top of that, the manager
    ensures that the process handling the producer_handler is always running.
    This makes sure producer is always sending data to consumers for processing.

    Args:
        producer_name: str - This is the name for the  producer function to identify
            it in logs
        producer_func: Callable - This is the producer function which is supposed to
                yield data to be passed to the consumer.
        consumer_name: str - This is the name of the consumer method to identify it
            in logs
        consumer_func: Callable - This is the function handling the data the producer
            function yields.
        producer_wait_time: int - This is the amount of time before a producer process
            is restarted to make sure it's always running.
            The default value is 5 seconds.
        producer_opts: dict - These are keyword arguments passed when starting up the
            producer function.
    """
    cons_gen, _ = consumer_handler(
        consumer_func=consumer_func, consumer_name=consumer_name
    )

    # producer runs on a separate Process in the background
    producer_process = Process(
        target=producer_handler,
        args=(producer_name, producer_func, producer_opts, cons_gen),
    )
    producer_process.start()

    while True:
        try:
            time.sleep(producer_wait_time)
            if not producer_process.is_alive():
                producer_process = Process(
                    target=producer_handler,
                    args=(producer_name, producer_func, producer_opts, cons_gen),
                )
                producer_process.start()
        except (KeyboardInterrupt, SystemExit):
            logging.warning(f"Producer {producer_name} Exiting!")
            break
        except Exception as e:
            logging.error(f"{producer_name} got error - {e}")
            break

    # cleanup
    cons_gen.close()
    producer_process.join()


# DUMMY WORKLOADS
class Complex:
    def __init__(self, name: str, number: int):
        self._name = name
        self._index = number
        self.__created = datetime.datetime.now()  # can't serialize date automatically

    def get_created(self):
        return self.__created

    def __str__(self):
        return f"Smoucha's complex Mind - {self._name} - {self._index}"


def main():
    def create_complex_data(num: int):
        for i in range(num or 100):
            yield ((Complex(f"Jina-{i}", i), i), time.now())

    def process_complex_data(data, index):
        if data._index == index:
            valid = type(data.get_created()) == datetime.datetime
            if not valid:
                print("Wacha story za jaba bwana!")
        else:
            print("This ain't it bro!")

    advanced_manager(
        producer_name="create_complex_objs",
        producer_func=create_complex_data,
        consumer_name="process_complex_obj",
        consumer_func=process_complex_data,
        producer_wait_time=5,
        producer_opts={"num": 100},
    )


# # Using as a Management Command in Django
# class BasePiper(BaseCommand):
#     def handle(self, *args, **kwargs):
#         advanced_manager(
#             producer_name=self.producer_name,
#             producer_func=self.producer.__func__,
#             consumer_name=self.consumer_name,
#             consumer_func=self.consumer.__func__,
#             producer_wait_time=getattr(self, "producer_wait_time", 5),
#             producer_opts=getattr(self, "producer_opts", {}),
#         )

if __name__ == "__main__":
    main()
