from collections.abc import Generator
from time import sleep


def simple_coroutine(pattern: str) -> Generator[str, str, None]:
    """
    This is a simple coroutine instantiates with a desired pattern
    and takes a line(string) via .send() function
    and checks if pattern is in line
    """
    print("Coroutine is ready!")
    while True:
        line = yield str
        if pattern in line:
            print(line)


if __name__ == "__main__":
    # get coroutine
    rtn = simple_coroutine("brian")

    # start routine
    next(rtn)

    # send random data to coroutine
    for i in range(100):
        line = f"This is random - {i}"
        if i % 2 == 0:
            line = f"This is valid line - brian - {i}"

        rtn.send(line)

    # close coroutine
    rtn.close()
