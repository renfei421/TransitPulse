"""Apply a prepared release using the current explicit kube context and namespaces."""
import os
from pathlib import Path
import subprocess
import yaml


def call(*args):
    subprocess.run(list(args), check=True, timeout=300)


def main():
    namespace = os.environ["DEPLOY_NAMESPACE"]
    runtime_namespace = os.environ.get("FISSION_RUNTIME_NAMESPACE", namespace)
    if not os.getenv("KUBECONFIG"):
        raise ValueError("Set KUBECONFIG explicitly to the deployment file; no implicit personal context")
    # Fail before any mutation if required user-provisioned secrets do not exist.
    for ns in set([namespace, runtime_namespace]):
        for secret in ("transport-secrets", "transport-es-ca"):
            call("kubectl", "-n", ns, "get", "secret", secret, "-o", "name")
    call("kubectl", "-n", namespace, "get", "secret", "transport-migration-secrets", "-o", "name")
    call("fission", "spec", "validate")
    call("kubectl", "apply", "-f", "deploy/generated/core.yaml")
    migration = next(yaml.safe_load_all(Path("deploy/generated/migration.yaml").read_text()))
    # Kubernetes Job pod specs are immutable; a unique release Job avoids deletion.
    release = os.environ["CI_COMMIT_SHA"][:12]
    migration["metadata"]["name"] = "transport-migrate-"+release
    path = Path("artifacts/migration-release.yaml")
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(migration), encoding="utf-8")
    call("kubectl", "apply", "-f", str(path))
    call("kubectl", "-n", namespace, "wait", "--for=condition=complete",
         "job/"+migration["metadata"]["name"], "--timeout=240s")
    call("fission", "spec", "apply", "--wait")
    call("kubectl", "apply", "-f", "deploy/generated/cron.yaml")


if __name__ == "__main__":
    main()
