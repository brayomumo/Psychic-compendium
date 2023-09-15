""""
Threaded coroutines for advanced Async workflows
"""
from queue import Queue
from threading import Thread, current_thread
from time import sleep
import string
import random

from pub_sub import coroutine


class ThreadObject:
    def __init__(self, name: str, value: int) -> None:
        self._name = name
        self._value = value

    def handle_shit(self):
        print(f"This is {self._name} -> {self._value}")

@coroutine
def threaded(target):
    # this is the msg bar used to share data between threads
    msg_bar = Queue()

    def run_target():
        while True:
            item = msg_bar.get()
            if item == GeneratorExit:
                target.close()
                return
            target.send(item)

    Thread(target=run_target, name="T-func-thread").start()

    try:
        while True:
            item = yield
            msg_bar.put(item)
    except (GeneratorExit, KeyboardInterrupt):
        msg_bar.put(GeneratorExit)


@coroutine
def subscriber():
    thread = current_thread()
    print(f"This is the target function in thread - {thread.name}")
    while True:
        try:
            data = yield
            if data == GeneratorExit:
                print(f"Target function on - {thread.name} Recieved SIGTERM")
                break
            data.handle_shit()
        except GeneratorExit:
            print(f"Target func exiting - {thread.name}")
            break

if __name__ == "__main__":
    # initialize threaded coroutine
    reciever = threaded(subscriber())
    for i in range(100):
        # generate random string
        letters = string.ascii_letters
        name = ''.join(random.choice(letters) for i in range(10))
        # build thread object
        obj = ThreadObject(name, i)
        reciever.send(obj)

    reciever.send(GeneratorExit)
