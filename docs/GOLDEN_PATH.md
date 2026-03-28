# Orbital Golden Path (Aktueller Stand)

Diese Dokumentation beschreibt den aktuell implementierten End-to-End-Flow im Projekt.

## 1. Zielbild

Der Golden Path ist:

1. Hetzner konfigurieren
2. Projekt im Dashboard anlegen
3. Deployment starten
4. Reverse Proxy (nginx) konfigurieren
5. DNS pruefen
6. SSL mit certbot aktivieren (nur bei gueltigem DNS)
7. HTTPS verifizieren
8. Healthcheck ausfuehren

Die Pipeline ist als deterministische Step-Kette umgesetzt.

## 2. Voraussetzungen

## 2.1 Infrastruktur

- Linux-Server (Hetzner Cloud)
- SSH-Zugriff als root (oder konfigurierter SSH-User)
- Offene Ports fuer HTTP/HTTPS
- Domain mit A-Record auf die Ziel-IP

## 2.2 Laufzeit und Abhaengigkeiten

- Python 3.12+ empfohlen
- Docker und docker-compose auf Zielserver (werden im Step `prepare_host` installiert)
- nginx und certbot auf Zielserver (werden im Step `prepare_host` installiert)

## 2.3 Datenbank

- Standard lokal: SQLite
- Produktiv empfohlen: Postgres
- Migrationen werden beim App-Start automatisch versucht (konfigurierbar)

## 3. Environment-Variablen

Aktuell relevante Variablen:

- `SECRET_KEY`
- `DATABASE_URL` (Default: `sqlite:///orbital.db`)
- `REDIS_URL` (Broker + Result Backend fuer Celery)
- `ORBITAL_APP_NAME`
- `ORBITAL_ENV`
- `ORBITAL_DRY_RUN` (Default: `false`)
- `ORBITAL_AUTO_DB_UPGRADE` (Default: `true`)
- `ORBITAL_INLINE_DEPLOY_ON_QUEUE_ERROR` (Default: `true`)
- `HETZNER_API_TOKEN`
- `ORBITAL_SSH_KEY_PATH`
- `ORBITAL_SSH_USER` (Default: `root`)
- `ORBITAL_REPO_CLONE_ROOT`

Hinweis:

- Wenn Queue/Broker nicht verfuegbar ist und `ORBITAL_INLINE_DEPLOY_ON_QUEUE_ERROR=true`, wird der Deploy-Task asynchron im Hintergrund gestartet.

## 4. Hetzner-Konfiguration

Die Konfiguration erfolgt im Dashboard unter Hetzner-Settings.

Gespeichert werden:

- API Token
- Default Location
- Default Server Type
- Default Image
- SSH-Key Name
- Optional SSH Public Key

Projektbezogene Infrastruktur-Overrides koennen im Projekt gesetzt werden:

- Domain
- desired_server_type
- desired_location
- desired_image

## 5. Projekt anlegen

Im Dashboard unter Projekte:

1. Name vergeben
2. Optional Slug setzen (sonst automatisch)
3. Framework waehlen (aktuell sinnvoll: `flask` oder `node`)
4. Optional Domain setzen
5. Repository hinterlegen
6. Optional Environment-Variablen setzen

Unterstuetzte Projektkomponenten (aktueller Stand):

- Framework-Template: Flask
- Framework-Template: Node
- Dockerfile-Generierung
- docker-compose-Generierung
- nginx vHost-Generierung
- Hetzner Server-Provisionierung oder Reuse vorhandener Server

## 6. Deployment starten

Deployment wird im Projekt-Detail gestartet.

Die aktuelle Step-Reihenfolge:

1. `provision_server`
2. `wait_for_ssh`
3. `prepare_host`
4. `render_files`
5. `upload_and_deploy`
6. `configure_reverse_proxy`
7. `check_dns`
8. `run_certbot`
9. `verify_https`
10. `healthcheck`

## 6.1 Persistenz je Step

Jeder Step speichert konsistent:

- `name`
- `status`
- `started_at`
- `finished_at`
- `stdout`
- `stderr`
- `exit_code`
- `json_details` (optional)

Die Legacy-Felder `output` und `error_message` bleiben aus Kompatibilitaetsgruenden vorhanden und werden synchron gefuellt.

## 7. Domain konfigurieren

Voraussetzung fuer SSL:

- A-Record der Domain zeigt auf die Server-IP.

Der Step `check_dns` vergleicht:

- `resolved_ip`
- `expected_ip`
- `matches`

Bei `matches=false`:

- `check_dns` wird `failed`
- `run_certbot` wird `failed` (skip-begruendet)
- `verify_https` wird `failed` (skip-begruendet)
- Deployment wird `failed`

## 8. SSL aktivieren

SSL erfolgt ueber certbot im nginx-Workflow:

- Step `run_certbot`
- Befehl: `certbot --nginx ...`

Nur wenn DNS-Check erfolgreich war.

## 8.1 HTTPS-Verifikation

Nach certbot:

- Step `verify_https`
- Pruefung per HTTPS-Request auf die Domain

Danach folgt `healthcheck` gegen die interne App-Route.

## 9. Logs und Sichtbarkeit im Dashboard

## 9.1 Deployment-Detail

Im Deployment-Detail sind sichtbar:

- Live-Status
- Fortschrittsbalken
- Step-Liste mit Status
- stdout/stderr/exit_code je Step
- started_at/finished_at je Step
- Live-Log-Ansicht waehrend laufendem Deployment

## 9.2 Projekt-Detail

Im Projekt-Detail gibt es ein Runtime-Log-Panel:

- Server auswaehlbar
- Web-Container-Logs per Dashboard abrufbar
- Ausgabe von command, exit_code, stdout, stderr

Damit ist Standard-Debugging ohne manuelles SSH moeglich.

## 10. Typische Fehlerbilder

## 10.1 Hetzner API Fehler

Symptome:

- Provisionierung scheitert frueh
- `provision_server` auf failed

Ursachen:

- Token ungueltig
- API nicht erreichbar
- Quota/Validierungsfehler

Ablage:

- `stderr` im Step
- `json_details.error_category=hetzner_api_error`

## 10.2 SSH Fehler

Symptome:

- `wait_for_ssh` failed
- Timeout/Key/Auth-Probleme

Ablage:

- `stderr`
- `json_details.error_category=ssh_error`

## 10.3 Upload/Remote Command Fehler

Symptome:

- `upload_and_deploy` oder `prepare_host` failed
- Einzelne Remote-Befehle mit rc != 0

Ablage:

- `stdout`/`stderr`
- `json_details.failed_commands`
- `json_details.error_category=upload_error` oder `remote_command_error`

## 10.4 nginx Fehler

Symptome:

- `configure_reverse_proxy` failed
- `nginx -t` oder reload fehlgeschlagen

Ablage:

- `stderr`
- `json_details.error_category=nginx_error`

## 10.5 DNS Fehler

Symptome:

- `check_dns` failed
- Mismatch zwischen `resolved_ip` und `expected_ip`

Ablage:

- `stdout` mit `resolved_ip/expected_ip/matches`
- `json_details.error_category=dns_error`

## 10.6 certbot Fehler

Symptome:

- `run_certbot` failed
- Zertifikat kann nicht ausgestellt werden

Ablage:

- `stderr`
- `json_details.error_category=certbot_error`

## 10.7 Healthcheck/HTTPS Fehler

Symptome:

- `verify_https` oder `healthcheck` failed

Ablage:

- `stderr`
- `json_details.error_category=healthcheck_error`

## 11. Cleanup-Flow (Projekt vom Server entfernen)

Im Projekt-Detail existiert ein Cleanup-Flow:

- docker-compose down inkl. Volumes
- nginx site symlink entfernen
- nginx reload
- Deploy-Verzeichnis loeschen

Zweck:

- denselben Server sauber fuer ein neues Projekt freimachen.

## 12. Grenzen im aktuellen Stand

- Kein vollwertiges Secret-Management (nur App-Storage)
- SQLite in Produktivbetrieb ist technisch moeglich, aber nicht empfohlen
- Security-Scanner-Traffic auf offene HTTP-Endpunkte ist normal und muss infrastrukturell gefiltert werden (Firewall/Rate-Limits/WAF)

## 13. Empfohlene Betriebs-Checks pro Deployment

1. Deployment-Detail: alle Steps success
2. `check_dns` zeigt `matches=true`
3. `run_certbot` success
4. `verify_https` success
5. `healthcheck` success
6. Runtime-Logs im Projekt-Detail ohne neue Exceptions
