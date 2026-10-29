def pytest_configure(config):
    config.addinivalue_line(
        "markers", "hls: runs Vitis HLS (vitis-run); skipped when it is not on PATH"
    )
