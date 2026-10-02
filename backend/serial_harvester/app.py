"""Legacy serial entrypoint. New deployments use independent platform functions."""
from backend.parallel_harvester.app import main_bluesky, main_mastodon

def main():
    first, code_a = main_bluesky()
    second, code_b = main_mastodon()
    return {"bluesky":first,"mastodon":second}, max(code_a,code_b)
