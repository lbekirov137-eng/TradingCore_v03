from concurrent.futures import ThreadPoolExecutor


class Performance:

    EXECUTOR = ThreadPoolExecutor(max_workers=8)