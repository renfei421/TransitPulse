"""Compatibility CLI for the packaged article collector."""
from backend.news_harvester.articles import *  # noqa: F403

if __name__=="__main__":
    print(main())
