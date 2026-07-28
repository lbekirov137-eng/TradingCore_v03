from datetime import datetime


class MarketCache:

    def __init__(self):
        self.cache = {}

    def set(self, key, value):
        self.cache[key] = {
            "value": value,
            "time": datetime.utcnow(),
        }

    def get(self, key):
        item = self.cache.get(key)

        if item is None:
            return None

        return item["value"]

    def clear(self):
        self.cache.clear()