"""Compatibility CLI for the packaged GDELT volume collector."""
from backend.news_harvester.volume import *  # noqa: F403

if __name__ == "__main__":
    print(main())
