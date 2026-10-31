{{/*
Common helpers. Labels mirror deploy/'s app.kubernetes.io usage so the chart
output and the kustomize output are diffable.
*/}}

{{- define "garmin.labels" -}}
app.kubernetes.io/part-of: garmin-observability
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "garmin.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "garmin.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{- define "garmin.otlpEndpoint" -}}
http://{{ .Release.Name }}-otel-collector.{{ .Release.Namespace }}.svc.cluster.local:4317
{{- end -}}

{{- define "garmin.vmUrl" -}}
http://{{ .Release.Name }}-victoriametrics.{{ .Release.Namespace }}.svc.cluster.local:8428
{{- end -}}
