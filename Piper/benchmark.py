import time
from coroutines import main as coroutine
from pub import dummy_publisher, consumer
from pub import manager as pipes


def benchmark(number=1000):
    # testing with 1000 records
    # coroutine
    start = time.time()
    coroutine(number)
    duration = time.time() - start
    print(f"Duration taken for coroutine - {duration:.3f} Seconds")


    # pipes
    start = time.time()
    pipes(dummy_publisher, consumer, 1, 1)
    duration = time.time() - start
    print(f"Duration taken for Pipes - {duration:.3f} Seconds")


if __name__ == "__main__":
    benchmark()