# Runbook: Pod in ImagePullBackOff / ErrImagePull

## Symptoms
* Pod phase is `Pending` or `Waiting` with reason `ImagePullBackOff` or `ErrImagePull`.
* Kubernetes events show `Failed to pull image`, `rpc error: code = Unknown desc = Error response from daemon: manifest unknown`, or `authentication required`.

## Root Causes
1. **Typo in Image Tag:** Deployment manifest references a nonexistent version (e.g. `v2.4.99` instead of `v2.4.1`).
2. **Missing ImagePullSecret:** Pod lacks credentials to pull from a private registry (GHCR, ECR, DockerHub).
3. **Registry Outage / Rate Limit:** Docker Hub rate limit exceeded (HTTP 429) or private container registry down.

## Diagnostic Steps
1. Inspect image tag with `kubectl describe pod <pod_name>`.
2. Inspect events for the exact daemon error message.
3. Verify if `imagePullSecrets` are defined on the ServiceAccount or Pod spec.

## Remediation
* If image tag is invalid, roll back to previous stable revision: `kubectl rollout undo deployment/<deployment_name>`.
* If secret is missing, link or patch secret to the pod ServiceAccount.
