# Garmin Observability Runbook

## Alerts

| Alert | Condition | What to do |
|-------|-----------|------------|
| `GarminExporterStale` | No successful exporter run in 6h | Check `make logs`. If the token expired, run `make login` then `make sync`. |
| `GarminExporterAPIErrors` | Garmin API errors in 5m | Likely rate limiting. The exporter backs off automatically; check if Garmin Connect is down. |
| `GarminDailyStepsBelowGoal` | Steps below goal for 3+ days | No action required — informational. |
| `GarminRestingHeartRateHigh` | RHR > 70 bpm for 3+ days | No action required — informational. Consider rest/hydration. |

## WhatsApp notifications

Alertmanager has no native WhatsApp receiver. Alerts are sent via a webhook to a small gateway that calls the WhatsApp API.

### Option 1: CallMeBot (free, quickest)

1. Save the CallMeBot WhatsApp number and send it "Hello" to register
2. You'll get an API key reply
3. Deploy the gateway:
   ```bash
   kubectl --context minikube -n garmin apply -f deploy/helper/whatsapp-gateway.yaml
   ```
4. Set env vars on the gateway deployment:
   ```bash
   kubectl --context minikube -n garmin set env deployment/whatsapp-gateway \
     WHATSAPP_PHONE=+1XXXXXXXXXX \
     WHATSAPP_APIKEY=your_api_key
   ```
5. Point Alertmanager at the gateway:
   ```yaml
   # deploy/config/alertmanager.yaml
   - name: 'whatsapp'
     webhook_configs:
       - url: 'http://whatsapp-gateway.garmin.svc.cluster.local:8080/send'
   ```

### Option 2: Twilio / MessageBird

Replace the webhook URL in `deploy/config/alertmanager.yaml` with your gateway endpoint.

## Backfill history

```bash
make cluster-backfill FROM=2026-10-01 TO=2026-10-31
```

## Force a sync

```bash
make sync
```

## Stop / restart

```bash
make down   # deletes namespace (PVC data lost)
make up     # bring it back
```

## Troubleshooting

- **Grafana auth fails**: The admin password is in the `grafana-credentials` Secret. Reset with `make secret` if needed.
- **vmalert not evaluating rules**: Check `kubectl logs deployment/vmalert` — it reads `/etc/vmalert/rules/alert-rules.yml` from the ConfigMap.
- **Alertmanager not receiving**: Verify `kubectl logs deployment/alertmanager` and port-forward to `:9093` to check the UI.
- **Exporter not running**: Check `kubectl logs -l app.kubernetes.io/name=garmin-exporter`. Tokens live on the `garmin-tokens` PVC.
