# Helm chart

`helm/garmin` renders the same stack as `deploy/` (kustomize). deploy/ remains
the contract: if the chart cannot reproduce those manifests, the chart is wrong.

Local install:

    helm install garmin helm -n garmin --create-namespace \
      --set-string grafana.adminSecretName=grafana-credentials

The Grafana admin password must already exist in the referenced Secret; the
chart never renders a password itself (same rule as deploy/).
