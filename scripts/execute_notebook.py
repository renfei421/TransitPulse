"""Execute in this project's interpreter without installing a global Jupyter kernel."""
import json
import os
from pathlib import Path
import sys
import tempfile
import nbformat
from nbclient import NotebookClient
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpecManager


def main():
    root = Path(__file__).resolve().parents[1]
    defaults = {"API_BASE": "http://127.0.0.1:9090/api/v1", "ANALYSIS_FROM": "2026-08-01",
                "ANALYSIS_TO": "2026-09-29", "DATASET_KIND": "synthetic",
                "ANALYSIS_HTML": str(root/"artifacts/analysis.html")}
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    with tempfile.TemporaryDirectory(prefix="transport-kernel-") as directory:
        kernel = Path(directory)/"transport"
        kernel.mkdir()
        (kernel/"kernel.json").write_text(json.dumps({"argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
            "display_name": "Transport project", "language": "python"}))
        manager = KernelManager(kernel_name="transport", kernel_spec_manager=KernelSpecManager(kernel_dirs=[directory]))
        notebook = nbformat.read(root/"frontend/hormuz_analysis.ipynb", as_version=4)
        try:
            NotebookClient(notebook, km=manager, timeout=120, resources={"metadata": {"path": str(root/"frontend")}}).execute()
        finally:
            if manager.has_kernel:
                manager.shutdown_kernel(now=True)
        output = root/"artifacts/hormuz_analysis.executed.ipynb"
        nbformat.write(notebook, output)
    print(json.dumps({"executed_code_cells": sum(c.cell_type == "code" for c in notebook.cells),
                      "notebook": str(output), "html": defaults["ANALYSIS_HTML"]}))


if __name__ == "__main__":
    main()
