#!/bin/bash
# Check if CYOA system components are running
# Usage: ./health-check.sh

echo "Checking CYOA system health..."
echo ""

# Check llama.cpp
echo -n "llama.cpp (localhost:8080): "
if curl -s http://localhost:8080/v1/models > /dev/null 2>&1; then
    echo "✓ Running"
else
    echo "✗ Not responding"
fi

# Check backend
echo -n "Backend (localhost:8765): "
if HEALTH=$(curl -s http://localhost:8765/api/health 2>/dev/null); then
    case "$HEALTH" in
        *'"ok":true'*) echo "✓ Healthy" ;;
        *) echo "✗ Unhealthy" ;;
    esac
else
    echo "✗ Not responding"
fi

# Check frontend
echo -n "Frontend (localhost:5173): "
if curl -s http://localhost:5173/ > /dev/null 2>&1; then
    echo "✓ Running"
else
    echo "✗ Not responding"
fi

echo ""
echo "Note: Open http://localhost:5173 in your browser to use the app"
