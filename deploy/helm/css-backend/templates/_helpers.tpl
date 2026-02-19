{{- define "css-backend.name" -}}
css-backend
{{- end -}}

{{- define "css-backend.labels" -}}
app.kubernetes.io/name: {{ include "css-backend.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
