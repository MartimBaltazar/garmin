# Helm chart

`helm/` renders the same stack as `deploy/` (kustomize). `deploy/` is the
contract: if the chart cannot reproduce those manifests, the chart is wrong.

**Config files are not edited here.** Helm cannot read files outside the chart
directory, so `helm/config/` holds byte-identical copies of `deploy/config/`
(plus the WhatsApp gateway script). Edit the originals and run
`make chart-sync`; CI's `make chart-check` fails on any drift. The only
per-packaging differences are Service names, which carry the release prefix:

- the Collector gets its remote-write endpoint from `VM_REMOTE_WRITE_ENDPOINT`,
  set by each Deployment;
- the Alertmanager template rewrites the gateway hostname.

Local install:

    helm install garmin helm -n garmin --create-namespace \
      --set-string grafana.adminSecretName=grafana-credentials

Secrets are referenced by name and never rendered by the chart:
`grafana-credentials`, `alert-webhook-token` (`make secret`) and
`whatsapp-credentials` (`make whatsapp-secret`).
