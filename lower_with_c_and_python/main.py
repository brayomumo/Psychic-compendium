import ctypes


_sum = ctypes.CDLL('./libsum.so')
_sum.hello.argtypes = (ctypes.c_int,)

def simple_callback(number):
    """This is a simple callback function to be invoked from C"""
    print(f"Function invoked from C with {number}")

# Register the callable as `CFUNCTYPE`
callback_type = ctypes.CFUNCTYPE(None, ctypes.c_int)(simple_callback)

def hello(n=10):
    """This is the actual python logic tying everything together
        It executes some logic(print statement), then calls the C function
        with the defined callable function.
    """
    print("Calling `hello` function in C")
    result = _sum.sum(ctypes.c_int(n), callback_type) # magic of it all

    print(int(result))

if __name__ == "__main__":
    hello(n=100)