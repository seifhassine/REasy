"""Bindings for native scalar arrays without changing the shared motion model."""
class ArrayValue:
    def __init__(self, values, index):
        self.values, self.index = values, index

    @property
    def value(self):
        return self.values[self.index]

    @value.setter
    def value(self, value):
        self.values[self.index] = value
