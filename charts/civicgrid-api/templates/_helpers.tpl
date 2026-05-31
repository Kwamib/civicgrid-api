{{/*
Chart name + version, truncated to 63 chars (K8s label limit).
*/}}
{{- define "civicgrid-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name.
*/}}
{{- define "civicgrid-api.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name and version.
*/}}
{{- define "civicgrid-api.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "civicgrid-api.labels" -}}
helm.sh/chart: {{ include "civicgrid-api.chart" . }}
{{ include "civicgrid-api.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "civicgrid-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "civicgrid-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name.
*/}}
{{- define "civicgrid-api.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "civicgrid-api.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Database secret name.
*/}}
{{- define "civicgrid-api.databaseSecretName" -}}
{{- if .Values.database.existingSecret }}
{{- .Values.database.existingSecret }}
{{- else }}
{{- printf "%s-database" (include "civicgrid-api.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Admin secret name. Used by /admin/keys endpoints.
*/}}
{{- define "civicgrid-api.adminSecretName" -}}
{{- if .Values.admin.existingSecret }}
{{- .Values.admin.existingSecret }}
{{- else }}
{{- printf "%s-admin" (include "civicgrid-api.fullname" .) }}
{{- end }}
{{- end }}
