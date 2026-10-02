"""Fission social-processing trigger."""
from backend.common.jobs import trigger

def main():
    return trigger("social")
