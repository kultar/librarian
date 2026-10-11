{{- define "librarian.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "librarian.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "librarian.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "librarian.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "librarian.selectorLabels" -}}
app.kubernetes.io/name: {{ include "librarian.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "librarian.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "librarian.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "librarian.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "librarian.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "librarian.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{/*
librarian's config.yaml: the user's `config`, plus the settings the chart manages
(listeners, paths, approver headers). Chart-managed keys always win; path and
database defaults apply only when the user did not set them.
*/}}
{{- define "librarian.config" -}}
{{- $c := deepCopy (.Values.config | default dict) -}}
{{- $_ := set $c "host" "0.0.0.0" -}}
{{- $_ := set $c "port" 8080 -}}
{{- $_ := set $c "pages_port" 8081 -}}
{{- $_ := set $c "state_dir" "/data" -}}
{{- if .Values.oauth2Proxy.enabled -}}
{{- /* Only the proxy sidecar can reach the pages; it sets the identity headers. */ -}}
{{- $_ := set $c "pages_host" "127.0.0.1" -}}
{{- $approvers := deepCopy (get $c "approvers" | default dict) -}}
{{- $_ := set $approvers "mode" "trusted_header" -}}
{{- $_ := set $approvers "user_header" "X-Forwarded-Email" -}}
{{- $_ := set $approvers "groups_header" "X-Forwarded-Groups" -}}
{{- if .Values.oauth2Proxy.approverGroup -}}
{{- $_ := set $approvers "group" .Values.oauth2Proxy.approverGroup -}}
{{- end -}}
{{- $_ := set $c "approvers" $approvers -}}
{{- else -}}
{{- $_ := set $c "pages_host" "0.0.0.0" -}}
{{- end -}}
{{- if .Values.publicUrl -}}
{{- $_ := set $c "public_url" .Values.publicUrl -}}
{{- end -}}
{{- /* Agents connect here; people use public_url (the pages). */ -}}
{{- $_ := set $c "a2a_url" (default (printf "http://%s.%s.svc:%v" (include "librarian.fullname" .) .Release.Namespace .Values.service.a2aPort) .Values.a2aUrl) -}}
{{- if .Values.registry.gitSync.enabled -}}
{{- $_ := set $c "registry_dir" (printf "/git/current/%s" (.Values.registry.gitSync.path | trimAll "/")) -}}
{{- $_ := set $c "registry_overlay_dir" "/data/registry" -}}
{{- else if not (hasKey $c "registry_dir") -}}
{{- $_ := set $c "registry_dir" "/data/registry" -}}
{{- end -}}
{{- if not (hasKey $c "secrets_dir") -}}
{{- $_ := set $c "secrets_dir" "/data/secrets" -}}
{{- end -}}
{{- if not (hasKey $c "database_url") -}}
{{- $_ := set $c "database_url" "sqlite+aiosqlite:////data/librarian.db" -}}
{{- end -}}
{{- if .Values.otel.enabled -}}
{{- $_ := set $c "otel_enabled" true -}}
{{- end -}}
{{- if .Values.githubApp.enabled -}}
{{- /* Review pull requests go to the registry repo git-sync reads, as the same App. */ -}}
{{- $git := .Values.registry.gitSync -}}
{{- $publish := deepCopy (get $c "publish" | default dict) -}}
{{- $_ := set $publish "type" "github" -}}
{{- if not (hasKey $publish "repo") -}}
{{- $_ := set $publish "repo" (regexFind "[^/:]+/[^/]+$" (trimSuffix ".git" $git.repo)) -}}
{{- end -}}
{{- if not (hasKey $publish "directory") -}}
{{- $_ := set $publish "directory" ($git.path | trimAll "/") -}}
{{- end -}}
{{- if not (hasKey $publish "base_branch") -}}
{{- $_ := set $publish "base_branch" $git.ref -}}
{{- end -}}
{{- $_ := set $publish "github_app" (dict "app_id" (toString .Values.githubApp.appId) "installation_id" (int .Values.githubApp.installationId) "private_key_file" (printf "/etc/librarian-github-app/%s" .Values.githubApp.secretKey)) -}}
{{- $_ := set $c "publish" $publish -}}
{{- end -}}
{{- toYaml $c -}}
{{- end -}}
