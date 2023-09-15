"""
Python Pubsub wannabe using coroutines
"""
from datetime import datetime
from time import sleep

# utils
def coroutine(func):
    def wrapper(*args):
        f = func(*args)
        next(f)
        return f
    return wrapper

# workloads
def producer():
    for i in range(1000_000):
        now = datetime.now()
        sleep(1)
        yield((i, now), now)

def consumer_func(*args):
    print(args)


# Base workflow
@coroutine
def run_job(random_func, start_time = None):
    while running := True:
        try:
            data = yield
            if not data:
                running = False
                continue
            random_func(data)
        except (KeyboardInterrupt, SystemExit):
            running = False
            print("Exiting this baby!!")
    else:
        print("cleaning workloads!!")


def main():
    consumer = run_job(consumer_func)
    for data in producer():
        consumer.send(data)

# main()