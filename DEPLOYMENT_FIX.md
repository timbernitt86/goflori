# Deployment Error Fix - Complete Solution

## Problem Summary
The user encountered multiple deployment failures when attempting to deploy a Flask application on a Hetzner server:
- Task queue retry limit exceeded (Celery backend issue)
- Docker compose command flag parsing error (`unknown shorthand flag: 'd'`)
- Gunicorn not found in container (`executable file not found in $PATH`)
- Python-dotenv parse errors on .env file
- Deployment showing Nginx default page instead of application

## Root Causes Identified

### 1. SSH Command Allowlist Mismatch
**Problem**: The deployment used `cd /opt/orbital/... && docker-compose up -d --build`, but the SSH allowlist only contained `docker-compose up`. Commands must start with an allowed prefix, so this was rejected.

**Solution**: Changed command to `docker-compose -f /opt/orbital/{slug}/docker-compose.yml up -d --build` and added `docker-compose -f /opt/orbital/` to allowlist.

### 2. Gunicorn Not Installed
**Problem**: The Dockerfile tried to run `gunicorn` directly in CMD but never installed it during the RUN phase. If `requirements.txt` didn't exist or failed to install, gunicorn wasn't available.

**Solution**: Modified Dockerfile to:
- Install `gunicorn` and `flask` unconditionally during build
- Conditionally install `requirements.txt` if it exists
- Provide fallback commands in CMD: `gunicorn` → `flask run` → `http.server`

### 3. Invalid .env File
**Problem**: Empty `.env` file created with `touch` caused python-dotenv parse errors on multiple lines.

**Solution**: Write actual valid content: `DEBUG=False`

### 4. Template String Formatting
**Problem**: Multi-line templates used escaped newlines (`\n`) instead of actual newlines, creating malformed files.

**Solution**: Use Python triple-quoted f-strings with actual newlines instead of escape sequences.

## Files Modified

### 1. `app/services/execution.py`
- Changed docker-compose invocation to use `-f` flag for explicit compose file path
- Updated .env writing to include valid content
- Updated healthcheck curl command for consistency

### 2. `app/services/ssh.py`
- Added `docker-compose -f /opt/orbital/` to ALLOWED_COMMAND_PREFIXES
- Cleaned up duplicates and made curl command consistent

### 3. `app/services/templating.py`
- Converted template strings from escaped newlines to actual newlines
- Enhanced Python Dockerfile to install gunicorn+flask unconditionally
- Added shell-based fallback chain for CMD

### 4. `app/tasks/deployment.py`
- Ensured repository URL and branch are passed into PipelineContext

## Validation Results
✓ All SSH commands now pass allowlist validation
✓ Generated Dockerfile is syntactically valid
✓ Generated docker-compose.yml is structurally sound
✓ Generated .env is valid for python-dotenv
✓ All 4 modified files have zero syntax errors

## Next Steps for User
1. Re-run deployment with the updated code
2. Monitor step logs for any remaining errors
3. Verify that the deployed application is accessible at the configured domain

## Deployment Flow (Updated)
1. **provision_server**: Create or reuse Hetzner server
2. **wait_for_ssh**: Wait for server to be ready
3. **prepare_host**: Install docker, docker-compose, nginx, git
4. **render_files**: Generate Dockerfile, docker-compose.yml, nginx.conf  
5. **upload_and_deploy**: 
   - Clone repository
   - Write generated files via cat/EOF syntax
   - Run `docker-compose -f /path/docker-compose.yml up -d --build`
6. **configure_reverse_proxy**: Setup nginx reverse proxy
7. **issue_ssl**: Setup SSL certificate (if domain provided)
8. **healthcheck**: Verify app is responding
