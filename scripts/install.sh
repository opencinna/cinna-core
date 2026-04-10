#!/usr/bin/env bash
#
# Cinna Core — First-time setup wizard
# Creates .env files, builds containers, runs migrations, seeds admin user.
#
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}ℹ ${NC}$*"; }
ok()    { echo -e "${GREEN}✔ ${NC}$*"; }
warn()  { echo -e "${YELLOW}⚠ ${NC}$*"; }
err()   { echo -e "${RED}✖ ${NC}$*"; }
header(){ echo -e "\n${BOLD}── $* ──${NC}\n"; }

# ── Resolve project root (one level up from scripts/) ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ── Prerequisites ─────────────────────────────────────────────────────────────
header "Checking prerequisites"

missing=0
for cmd in docker git; do
    if ! command -v "$cmd" &>/dev/null; then
        err "$cmd is not installed"
        missing=1
    else
        ok "$cmd found"
    fi
done

if ! docker compose version &>/dev/null; then
    err "docker compose plugin is not installed (need Docker Compose V2)"
    missing=1
else
    ok "docker compose found"
fi

if [ "$missing" -eq 1 ]; then
    echo ""
    err "Please install missing prerequisites and re-run."
    exit 1
fi

# ── Guard: already running? ───────────────────────────────────────────────────
if docker compose ps --status running 2>/dev/null | grep -q "backend"; then
    warn "Containers are already running."
    read -rp "$(echo -e "${YELLOW}Stop them and start fresh? [y/N]: ${NC}")" stop_choice
    if [[ "$stop_choice" =~ ^[Yy]$ ]]; then
        info "Stopping containers..."
        docker compose down
    else
        info "Keeping existing containers. Exiting."
        exit 0
    fi
fi

# ── .env setup ────────────────────────────────────────────────────────────────
header "Environment configuration"

generate_secret() {
    # 32-byte URL-safe token, matching Python's secrets.token_urlsafe(32)
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null \
        || openssl rand -base64 32 | tr -d '=/+' | head -c 43
}

if [ -f .env ]; then
    warn ".env already exists."
    read -rp "$(echo -e "${YELLOW}Overwrite with fresh config? [y/N]: ${NC}")" overwrite
    if [[ ! "$overwrite" =~ ^[Yy]$ ]]; then
        info "Keeping existing .env"
        SKIP_ENV=1
    else
        SKIP_ENV=0
        cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
        ok "Backed up current .env"
    fi
else
    SKIP_ENV=0
fi

if [ "${SKIP_ENV:-0}" -eq 0 ]; then
    if [ ! -f .env.example ]; then
        err ".env.example not found in project root. Cannot continue."
        exit 1
    fi

    echo ""
    info "Let's configure your instance. Press Enter to accept defaults shown in [brackets]."
    echo ""

    # ── Project name ──────────────────────────────────────────────────────────
    read -rp "$(echo -e "${BOLD}Project name${NC} [Cinna Core]: ")" PROJECT_NAME
    PROJECT_NAME="${PROJECT_NAME:-Cinna Core}"

    # ── Admin credentials ─────────────────────────────────────────────────────
    echo ""
    info "Admin account (first superuser):"
    read -rp "$(echo -e "${BOLD}Admin email${NC} [admin@example.com]: ")" ADMIN_EMAIL
    ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"

    while true; do
        read -srp "$(echo -e "${BOLD}Admin password${NC} (min 8 chars): ")" ADMIN_PASS
        echo ""
        if [ ${#ADMIN_PASS} -lt 8 ]; then
            warn "Password must be at least 8 characters."
            continue
        fi
        read -srp "$(echo -e "${BOLD}Confirm password${NC}: ")" ADMIN_PASS_CONFIRM
        echo ""
        if [ "$ADMIN_PASS" != "$ADMIN_PASS_CONFIRM" ]; then
            warn "Passwords do not match. Try again."
            continue
        fi
        break
    done

    # ── Postgres ──────────────────────────────────────────────────────────────
    echo ""
    info "PostgreSQL (runs inside Docker):"
    read -rp "$(echo -e "${BOLD}DB name${NC} [app]: ")" PG_DB
    PG_DB="${PG_DB:-app}"

    read -rp "$(echo -e "${BOLD}DB user${NC} [postgres]: ")" PG_USER
    PG_USER="${PG_USER:-postgres}"

    read -srp "$(echo -e "${BOLD}DB password${NC} [postgres]: ")" PG_PASS
    echo ""
    PG_PASS="${PG_PASS:-postgres}"

    # ── Generate secrets ──────────────────────────────────────────────────────
    SECRET_KEY="$(generate_secret)"
    ENCRYPTION_KEY="$(generate_secret)"

    # ── Copy .env.example → .env and replace values ───────────────────────────
    cp .env.example .env

    # Helper: replace a KEY=value line in .env (handles values with special chars)
    set_env() {
        local key="$1" value="$2"
        # Escape sed-special chars in value
        local escaped
        escaped=$(printf '%s' "$value" | sed 's/[&/\]/\\&/g')
        sed -i '' "s|^${key}=.*|${key}=${escaped}|" .env
    }

    set_env "PROJECT_NAME"              "\"${PROJECT_NAME}\""
    set_env "SECRET_KEY"                "$SECRET_KEY"
    set_env "ENCRYPTION_KEY"            "$ENCRYPTION_KEY"
    set_env "FIRST_SUPERUSER"           "$ADMIN_EMAIL"
    set_env "FIRST_SUPERUSER_PASSWORD"  "$ADMIN_PASS"
    set_env "POSTGRES_DB"               "$PG_DB"
    set_env "POSTGRES_USER"             "$PG_USER"
    set_env "POSTGRES_PASSWORD"         "$PG_PASS"
    set_env "GOOGLE_API_KEY"            ""
    set_env "DOCKER_IMAGE_BACKEND"      "cinna-core-backend"
    set_env "DOCKER_IMAGE_FRONTEND"     "cinna-core-frontend"

    ok ".env created (from .env.example)"
fi

# ── Frontend .env ─────────────────────────────────────────────────────────────
header "Frontend environment"

if [ ! -f frontend/.env ]; then
    cat > frontend/.env <<FEOF
VITE_API_URL=http://localhost:8000
VITE_APP_NAME="Cinna"
FEOF
    ok "frontend/.env created"
else
    ok "frontend/.env already exists"
fi

# ── Build & start ─────────────────────────────────────────────────────────────
header "Building Docker images"

info "This may take a few minutes on first run..."
docker compose build

header "Starting services"

docker compose up -d
ok "Containers started"

# ── Wait for healthy backend ──────────────────────────────────────────────────
header "Waiting for services to be ready"

# Source .env so we have DB creds for health checks regardless of wizard path
# shellcheck disable=SC1091
set +u; source .env 2>/dev/null || true; set -u

info "Waiting for database..."
retries=0
max_retries=30
until docker compose exec -T db pg_isready -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-app}" &>/dev/null; do
    retries=$((retries + 1))
    if [ "$retries" -ge "$max_retries" ]; then
        err "Database did not become ready in time."
        echo ""
        info "Check logs with: docker compose logs db"
        exit 1
    fi
    sleep 2
done
ok "Database is ready"

info "Waiting for backend..."
retries=0
until docker compose exec -T backend curl -sf http://localhost:8000/api/v1/utils/health-check/ &>/dev/null; do
    retries=$((retries + 1))
    if [ "$retries" -ge "$max_retries" ]; then
        err "Backend did not become ready in time."
        echo ""
        info "Check logs with: docker compose logs backend"
        exit 1
    fi
    sleep 2
done
ok "Backend is ready"

# ── Migrations & seed ────────────────────────────────────────────────────────
header "Database setup"

info "Running migrations..."
docker compose exec -T backend alembic upgrade head
ok "Migrations applied"

info "Seeding initial data..."
docker compose exec -T backend python /app/app/initial_data.py
ok "Admin user created"

# ── Done ──────────────────────────────────────────────────────────────────────
header "Setup complete!"

echo -e "${GREEN}${BOLD}Cinna Core is running!${NC}"
echo ""
echo -e "  Frontend:      ${CYAN}http://localhost:5173${NC}"
echo -e "  Backend API:   ${CYAN}http://localhost:8000${NC}"
echo -e "  Swagger UI:    ${CYAN}http://localhost:8000/docs${NC}"
echo -e "  Adminer (DB):  ${CYAN}http://localhost:8099${NC}"
echo -e "  MailCatcher:   ${CYAN}http://localhost:1080${NC}"
echo ""

# Source .env to read admin email for display
# shellcheck disable=SC1091
source .env 2>/dev/null || true
echo -e "  Admin login:   ${BOLD}${FIRST_SUPERUSER:-admin@example.com}${NC}"
echo ""
echo -e "  ${YELLOW}Tip:${NC} Run ${BOLD}make logs${NC} to follow all service logs."
echo ""
