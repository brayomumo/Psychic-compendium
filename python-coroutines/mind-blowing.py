from time import sleep


class Parent:
    """
    This is parent class to manage a set of child coroutine which
    will process incoming data asynchronously. 
    """

    def __init__(self, no_of_workers: int):
        self._worker_count = no_of_workers
        self._active_worker_count = 0

        # list of active coroutines waiting to process data
        self._availabe_workers = []

        # create parent listener
        self.job = self.process_workload()

        # start up parent
        self._start()

    def _start(self):
        # create child workers
        self.__spin_up_child_workers()

        # intiate parent listener
        next(self.job)
    
    def _stop(self):
        for worker in self._availabe_workers:
            worker.stop()

        # clean up parent listener
        self.job.close()
    
    def __spin_up_child_workers(self):
        """
        This spins up new child workers.
        """
        print("spinning up new workers!!")
        self._worker_count = 0
        for i in range(self._worker_count):
            child = Child(i)
            self._availabe_workers.append(child)
            self._active_worker_count += 1

    def process_workload(self):
        """
        This listens for work Items and passes them down to child processes.
        """
        try:
            
            while self._active_worker_count > 0:
                try:
                    worker = self._availabe_workers.pop()
                    if not worker.is_available:
                        # worker is processing another item, pick another one
                        continue
                except IndexError:
                    print(f"No available worker to pick up jobs!")
                    self.__spin_up_child_workers()
                    continue


                work_item = yield 
                worker.process(work_item)

        except GeneratorExit:
            print(f"Parent exiting!")
            self._stop()


    

class Child:

    def __init__(self, count: int):
        self.is_available = True
        self.my_coroutine = operation_coroutine(count)
        next(self.my_coroutine)

    def process(self, data: int):
        self.is_available = False

        #  parse payload
        local_data = {
            "func": sleep,
            "args": data
        }
        self.my_coroutine.send(local_data)
        self.is_available = True

    def stop(self):
        """
            Cleanup coroutines attached to object
        """
        self.my_coroutine.close()
        
        





def operation_coroutine(count):
    """
        This is a coroutine to perform the actual operation
        This used as the main function in the child processes.
    """
    try:
        while True:
            line = yield
            print(f"Workload - {line} - picked by worker {count}")
            func = line['func']
            func(line["args"])

    except GeneratorExit:
        print(f"worker {count} exiting!")


if __name__ == "__main__":
    paren = Parent(5)

    for i in range(10):
        paren.job.send(i)
    
    paren._stop()
        
