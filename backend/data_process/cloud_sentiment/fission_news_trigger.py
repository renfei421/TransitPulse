"""Fission news-processing trigger."""
from backend.common.jobs import trigger

def main():
    return trigger("news")
