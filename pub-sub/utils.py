import threading
from time import sleep
from typing import Optional


class Job:
    def __init__(self, obj, is_daemon: bool = True,  *args, **kwargs):
        """
        Job is a thread that takes an obj with args and kwargs.
        obj is an object which defines a start function which is
        run when the thread starts.
        It takes an object, args and kwargs which are used to
        when running the start operation 
        """
        try:
            self.func = obj.kimbia
        except AttributeError:
            raise Exception(f"kimbia method not defined in {obj}")

        self.args = args
        self.kwargs = kwargs
        self.is_daemon = is_daemon

    def thread(self):
        """
        Returns a Thread whose target is the job's func
        args is the job's args and kwargs is the job's kwargs
        """
        return threading.Thread(
            target=self.func,
            args=self.args,
            kwargs=self.kwargs,
            daemon=self.is_daemon
        )
    

class Manager:
    def __init__(self, jobs, interval) -> None:
        self.jobs = jobs
        self.interval = interval

    def run(self):
        threads = {}
        for th in self.jobs:
            t = th.thread()
            t.start()
            threads[th] = t
        
        while True:
            try:
                sleep(self.interval)
                for t, thread in threads.items():
                    # restart threads if failed
                    if thread.is_alive():
                        new_thread = t.thread()
                        new_thread.start()
                        threads[t] = new_thread

            except KeyboardInterrupt:
                print("Terminating these babies!!")
                break

        # wait for threads to finish
        for thread in threads.values():
            thread.join()

