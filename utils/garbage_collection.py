import gc


def configure_garbage_collector() -> None:
    """The collector thresholds of every REasy process: the app, its pool workers and the tests."""
    gc.set_threshold(50_000, 20, 20)
