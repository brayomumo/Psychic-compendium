from time import sleep


def coroutine(func):
    """Decorator to initialize coroutines
    """
    def wrapper(*args):
        f = func(*args)
        next(f)
        return f

    return wrapper


class Simple:
    def __init__(self, name):
        self._name = name
        self._running = False

    @coroutine
    def run(self, wait_time):
        print("Routine initialized!")
        while self._running:
            sleep(wait_time)
            method, data = yield
            if not method and not data:
                continue
            print(f"Publishing - {method} --> {data}!")
        else:
            print("Quiting this shit!")


def main():
    simp = Simple("Testing")
    simp._running = True
    prod = simp.run(1)
    for i in range(100):
        prod.send(("User Create", f"User - {i}"))


main()
