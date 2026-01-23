#!/bin/bash
# VPS Management Script for Hyperdash Monitor
# Usage: ./scripts/vps.sh <command>
#
# Commands:
#   logs      - Fetch all logs from VPS
#   data      - Fetch all data files from VPS
#   all       - Fetch logs and data
#   ssh       - Open SSH session to VPS
#   status    - Check service status
#   restart   - Restart the monitor service
#   tail      - Tail live logs

set -e

# ============================================
# CONFIGURATION - Update these after VPS setup
# ============================================
VPS_HOST="${VPS_HOST:-your-vps-ip}"
VPS_USER="${VPS_USER:-root}"
VPS_PATH="${VPS_PATH:-/opt/hyperdash_scanner}"
LOCAL_BACKUP_DIR="./vps_backup"

# ============================================
# FUNCTIONS
# ============================================

show_help() {
    echo "Hyperdash VPS Management"
    echo ""
    echo "Usage: ./scripts/vps.sh <command>"
    echo ""
    echo "Commands:"
    echo "  logs      Fetch all log files from VPS"
    echo "  data      Fetch data files (cohort, position CSVs)"
    echo "  all       Fetch both logs and data"
    echo "  ssh       Open SSH session to VPS"
    echo "  status    Check Docker container status"
    echo "  restart   Restart the monitor container"
    echo "  tail      Tail live logs from container"
    echo ""
    echo "Environment variables:"
    echo "  VPS_HOST  VPS IP or hostname (current: $VPS_HOST)"
    echo "  VPS_USER  SSH user (current: $VPS_USER)"
    echo "  VPS_PATH  App path on VPS (current: $VPS_PATH)"
}

check_config() {
    if [[ "$VPS_HOST" == "your-vps-ip" ]]; then
        echo "ERROR: VPS_HOST not configured"
        echo ""
        echo "Set it with: export VPS_HOST=your-actual-ip"
        echo "Or edit this script directly"
        exit 1
    fi
}

fetch_logs() {
    check_config
    echo "Fetching logs from $VPS_USER@$VPS_HOST..."
    mkdir -p "$LOCAL_BACKUP_DIR/logs"
    rsync -avz --progress \
        "$VPS_USER@$VPS_HOST:$VPS_PATH/logs/" \
        "$LOCAL_BACKUP_DIR/logs/"
    echo ""
    echo "Logs saved to: $LOCAL_BACKUP_DIR/logs/"
    ls -la "$LOCAL_BACKUP_DIR/logs/"
}

fetch_data() {
    check_config
    echo "Fetching data from $VPS_USER@$VPS_HOST..."
    mkdir -p "$LOCAL_BACKUP_DIR/data"
    rsync -avz --progress \
        "$VPS_USER@$VPS_HOST:$VPS_PATH/data/" \
        "$LOCAL_BACKUP_DIR/data/"
    echo ""
    echo "Data saved to: $LOCAL_BACKUP_DIR/data/"
    ls -la "$LOCAL_BACKUP_DIR/data/raw/" 2>/dev/null || true
    ls -la "$LOCAL_BACKUP_DIR/data/processed/" 2>/dev/null || true
}

open_ssh() {
    check_config
    echo "Connecting to $VPS_USER@$VPS_HOST..."
    ssh "$VPS_USER@$VPS_HOST"
}

check_status() {
    check_config
    echo "Checking container status..."
    ssh "$VPS_USER@$VPS_HOST" "docker ps -a | grep hyperdash || echo 'Container not found'"
}

restart_service() {
    check_config
    echo "Restarting monitor service..."
    ssh "$VPS_USER@$VPS_HOST" "cd $VPS_PATH && docker compose restart"
    echo "Done. Checking status..."
    sleep 2
    check_status
}

tail_logs() {
    check_config
    echo "Tailing live logs (Ctrl+C to stop)..."
    ssh "$VPS_USER@$VPS_HOST" "docker logs -f hyperdash-monitor --tail 100"
}

# ============================================
# MAIN
# ============================================

case "${1:-help}" in
    logs)
        fetch_logs
        ;;
    data)
        fetch_data
        ;;
    all)
        fetch_logs
        echo ""
        fetch_data
        ;;
    ssh)
        open_ssh
        ;;
    status)
        check_status
        ;;
    restart)
        restart_service
        ;;
    tail)
        tail_logs
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo "Unknown command: $1"
        echo ""
        show_help
        exit 1
        ;;
esac
