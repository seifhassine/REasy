import os
import sys
import time
import psutil
import argparse
import statistics
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from rsz.rsz_file import ScnFile

def format_size(size):
    """Convert bytes to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

def get_memory_usage():
    """Get current memory usage of the process"""
    process = psutil.Process()
    return process.memory_info().rss

def benchmark_scn(file_path):
    """
    Benchmark SCN file parsing performance
    
    Args:
        file_path: Path to SCN file
        iterations: Number of iterations to measure
    """
    print(f"Benchmarking {file_path}")
    print("-" * 50)

    with open(file_path, "rb") as f:
        data = f.read()
    file_size = len(data)
    
    times = []
    memory_before = get_memory_usage()
    
    scn = ScnFile()
    
    start = time.perf_counter()
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    scn.read(data)
    end = time.perf_counter()
    
    total_reads = 50 
    real_time = (end - start) / total_reads
    
    min_time = real_time
    max_time = real_time
    avg_time = real_time
    std_dev = 0
    
    memory_after = get_memory_usage()
    memory_delta = memory_after - memory_before

    # Print results
    print("\nResults:")
    print("-" * 50)
    print(f"File size: {format_size(file_size)}")
    print(f"Memory usage before: {format_size(memory_before)}")
    print(f"Memory usage after: {format_size(memory_after)}")
    print(f"Memory increase: {format_size(memory_delta)}")
    print(f"\nParsing times:")
    print(f"Average: {avg_time*20:.2f}ms")
    print(f"Std Dev: {std_dev*20:.2f}ms")
    print(f"\nFile processing speed:")
    print(f"Average: {format_size(file_size/avg_time)}/s")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark SCN file parsing")
    parser.add_argument("file_path", help="Path to the SCN file")

    args = parser.parse_args()

    if not os.path.exists(args.file_path):
        print(f"Error: File {args.file_path} not found")
        sys.exit(1)

    benchmark_scn(args.file_path)
