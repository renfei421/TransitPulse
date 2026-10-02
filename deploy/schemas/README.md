# Pinned upstream schemas

fission-v1.23.0.json contains the openAPIV3Schema fields from the Environment,
Function, HTTPTrigger, Package and TimeTrigger CRDs at:
https://github.com/fission/fission/tree/v1.23.0/crds/v1

Source license: Apache-2.0, https://github.com/fission/fission/blob/v1.23.0/LICENSE
These schemas supplement local validation; cluster admission, name conflicts,
RBAC, storage classes and actual scaling still require a reachable cluster.
