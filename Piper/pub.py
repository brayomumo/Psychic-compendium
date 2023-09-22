from multiprocessing import Process, Pipe, pool

from sub import consumer


def dummy_publisher(*args, **kwargs):
    """Dummy producer to simulate user function"""
    maxer = kwargs.get("max_count") or 1000
    for i in range(maxer):
        yield i


def producer_handler(producer, pipe):
    writer_pipe, reader_pipe = pipe
    try:
        for data in producer():
            print(f"Sending - {data}")
            writer_pipe.send(data)

    except Exception as e:
        print(f"Error writing to pipe - {e}!")


def manager(publisher, consumer, no_of_publishers, no_of_workers):
    pipe = Pipe()

    # listner process
    listner = Process(target=consumer, args=(no_of_workers, pipe)).start()

    with pool.Pool(processes=no_of_publishers) as pooler:
        results = pooler.apply_async(producer_handler, (publisher, pipe))

        results.wait()


manager(dummy_publisher, consumer, 5, 2)
