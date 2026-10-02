"""Refresh all committed pip exports from the frozen uv lock without noisy stdout."""
import subprocess


def main():
    common = ["uv", "export", "--frozen", "--no-dev", "--no-emit-project"]
    for name, extra in (
        ("requirements.txt", []),
        ("requirements-model.txt", ["--extra", "model"]),
        ("requirements-experiment.txt", ["--extra", "experiment"]),
        ("requirements-fission.txt", [item for package in ("numpy", "scipy", "nltk", "vaderSentiment", "redis", "websockets")
                                     for item in ("--prune", package)]),
    ):
        subprocess.run([*common, *extra, "--output-file", name], check=True, stdout=subprocess.DEVNULL)
        print(name)


if __name__ == "__main__":
    main()
