import cProfile
import pstats

from coroutines import main

if __name__ == "__main__":
    # create profiler
    profiler = cProfile.Profile()
    profiler.enable() # start profiling
    main(limit=100_000)
    profiler.disable() # end profiling
    # process stats
    stats = pstats.Stats(profiler).sort_stats("cumtime")
    stats.dump_stats("profile_data") # Write stats to file
    stats.print_stats()
