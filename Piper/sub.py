"""
Consumer:
    - Read data from  a pipe
    - spin up threads to run worker

Worker:
    - Work on the data
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
from time import sleep


def dummy_wroker(data):
    # mock job
    # sleeper = random.randint(0, 5)
    # sleep(sleeper)
    print(f"Recieved  - {data}")


def consumer(worker_count, pipe):
    writer, reader = pipe

    # close writer pipe when reading
    # writer.close()

    # handle workload in threads
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        while True:
            try:
                data = reader.recv()
                executor.submit(dummy_wroker, data)
            except (KeyboardInterrupt, SystemExit) as e:
                print(f"Exiting this pool")
                break

            except Exception as e:
                print(f"Some Exception occured - {e}")
                break
