#!/usr/bin/env python3
"""
Security management module for llama-shift server.
Handles PIN security, rate limiting, and account lockout logic.
"""

import hashlib
import json
import os
import threading
import time
from collections import defaultdict

# ==================== SECURITY CONFIGURATION ====================
PIN_MAX_ATTEMPTS = 5  # Maximum failed attempts before lockout
PIN_LOCKOUT_DURATION = 300  # Lockout duration in seconds (5 minutes)
PIN_SESSION_TIMEOUT = 1800  # Session timeout in seconds (30 minutes)
RATE_LIMIT_WINDOW = 60  # Rate limit window in seconds (1 minute)
RATE_LIMIT_MAX_REQUESTS = 10  # Maximum requests per window

# ==================== IN-MEMORY SECURITY STATE ====================
# Stores failed attempts: {ip_address: [(timestamp, success), ...]}
_pin_failed_attempts = defaultdict(list)
# Stores lockout times: {ip_address: lockout_until_timestamp}
_pin_lockouts = {}
# Track request timestamps for rate limiting: {ip_address: [timestamps]}
_rate_limit_requests = defaultdict(list)

# PIN session tracking: token -> {created_at, ip}
_PIN_SESSIONS = {}
_PIN_SESSION_LOCK = threading.Lock()

# Security logging
_security_log_file = None
_security_log_lock = threading.Lock()


# ─── PIN Security Functions ────────────────────────────────────────────────
def _hash_pin(pin, salt):
    """Hash a PIN with salt using SHA-256."""
    return hashlib.sha256(f"{salt}{pin}".encode()).hexdigest()


def _is_rate_limited(client_ip):
    """Check if client is rate limited based on request frequency."""
    current_time = time.time()
    # Clean up old requests outside the window
    _rate_limit_requests[client_ip] = [
        req_time for req_time in _rate_limit_requests[client_ip]
        if current_time - req_time < RATE_LIMIT_WINDOW
    ]
    
    # Check if limit exceeded
    if len(_rate_limit_requests[client_ip]) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    
    # Add current request
    _rate_limit_requests[client_ip].append(current_time)
    return False


def _is_account_locked(client_ip):
    """Check if account is locked due to failed PIN attempts."""
    current_time = time.time()
    # Clean up expired lockouts
    expired_lockouts = []
    for ip, lockout_time in _pin_lockouts.items():
        if current_time > lockout_time:
            expired_lockouts.append(ip)
    
    for ip in expired_lockouts:
        del _pin_lockouts[ip]
        # Also clear failed attempts for this IP
        _pin_failed_attempts[ip] = []
    
    # Check if account is currently locked
    if client_ip in _pin_lockouts:
        if current_time < _pin_lockouts[client_ip]:
            return True  # Still locked
        else:
            # Lockout expired, remove lockout
            del _pin_lockouts[client_ip]
            _pin_failed_attempts[client_ip] = []
            return False
    
    return False


def _record_failed_attempt(client_ip, success=False):
    """Record a failed PIN attempt."""
    current_time = time.time()
    _pin_failed_attempts[client_ip].append((current_time, success))
    
    # Clean up old attempts outside the window
    # Keep only attempts within 24 hours for account lockout logic
    _pin_failed_attempts[client_ip] = [
        (at_time, success) for at_time, success in _pin_failed_attempts[client_ip]
        if current_time - at_time < 86400  # 24 hours
    ]
    
    # Check if we should lock the account
    failed_attempts = [
        (at_time, success) for at_time, success in _pin_failed_attempts[client_ip]
        if not success
    ]
    
    if len(failed_attempts) >= PIN_MAX_ATTEMPTS:
        # Lock account for PIN_LOCKOUT_DURATION seconds
        _pin_lockouts[client_ip] = current_time + PIN_LOCKOUT_DURATION
        
        # Log security event
        _log_security_event("account_locked", {
            "ip": client_ip,
            "attempts": len(failed_attempts),
            "locked_until": _pin_lockouts[client_ip]
        })
        return True
    
    return False


def _log_security_event(event_type, details):
    """Log security events to a dedicated security log file."""
    global _security_log_file
    try:
        with _security_log_lock:
            if _security_log_file is None:
                from config import LOG_DIR
                security_log_path = os.path.join(LOG_DIR, "security.log")
                _security_log_file = open(security_log_path, "a", buffering=1)
            
            log_entry = {
                "timestamp": time.time(),
                "event": event_type,
                "details": details
            }
            
            _security_log_file.write(json.dumps(log_entry) + "\n")
    except Exception as e:
        # Don't let logging errors break the main application
        print(f"[security] Failed to log security event {event_type}: {e}")


# ─── Server Health Check ───────────────────────────────────────────────────
_SERVER_READY = False


def set_server_ready():
    """Mark the server as fully initialized and ready to accept requests."""
    global _SERVER_READY
    _SERVER_READY = True


def is_server_ready():
    """Check if the server has completed initialization."""
    return _SERVER_READY