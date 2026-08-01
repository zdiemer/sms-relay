{{/*
Fully-qualified resource name.
*/}}
{{- define "sms-relay.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "sms-relay.labels" -}}
app.kubernetes.io/name: sms-relay
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "sms-relay.selectorLabels" -}}
app.kubernetes.io/name: sms-relay
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
In-cluster base URL. This is how sibling services (talaria, money) should call
the relay — it bypasses the ingress and the Authelia gate entirely, so they only
need an API key.
*/}}
{{- define "sms-relay.internalUrl" -}}
http://{{ include "sms-relay.fullname" . }}.{{ .Release.Namespace }}.svc.cluster.local:{{ .Values.service.port }}
{{- end -}}
