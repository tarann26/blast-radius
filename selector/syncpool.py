"""An in-process stand-in for ProcessPoolExecutor.

sqlglot's optimizer and executor tests send work to a spawn process pool. Spawning costs a fresh
interpreter per worker and hides the work from a tracer. This runs each task in the calling process
and round-trips arguments and results through pickle, as the pool would.
"""
import concurrent.futures as cf
import pickle


class SyncPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        self._initializer = initializer
        self._initargs = initargs
        self._started = False

    def _start(self):
        if not self._started:
            self._started = True
            if self._initializer is not None:
                self._initializer(*self._initargs)

    def submit(self, fn, /, *args, **kwargs):
        future = cf.Future()
        try:
            self._start()
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            result = fn(*args, **kwargs)
            result = pickle.loads(pickle.dumps(result))
        except BaseException as e:  # noqa: BLE001 - the pool hands every error back through the future
            future.set_exception(e)
        else:
            future.set_result(result)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futures = [self.submit(fn, *a) for a in zip(*iterables)]
        return (f.result() for f in futures)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False


def install():
    import concurrent.futures.process as process

    cf.ProcessPoolExecutor = SyncPool
    process.ProcessPoolExecutor = SyncPool
