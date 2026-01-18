#!/usr/bin/env python3
"""
Quick Setup Script for Hyperdash Scanner
=========================================

Run this FIRST after downloading the project.
Works on Windows, Mac, and Linux.

Usage:
    python setup.py
"""

import subprocess
import sys
import os

def run_command(cmd, description):
    """Run a command and handle errors"""
    print(f"\n{'='*50}")
    print(f"📦 {description}")
    print(f"{'='*50}")
    print(f"Running: {cmd}\n")
    
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        print(f"⚠️  Warning: {description} may have had issues")
        return False
    return True

def main():
    print("""
    ╔═══════════════════════════════════════════════════════╗
    ║     Hyperdash Large Position Scanner - Setup          ║
    ╚═══════════════════════════════════════════════════════╝
    """)
    
    # Check Python version
    if sys.version_info < (3, 8):
        print("❌ Python 3.8+ required. Please upgrade Python.")
        sys.exit(1)
    
    print(f"✅ Python {sys.version_info.major}.{sys.version_info.minor} detected")
    
    # Step 1: Install pip packages
    run_command(
        f"{sys.executable} -m pip install --upgrade pip",
        "Upgrading pip"
    )
    
    run_command(
        f"{sys.executable} -m pip install -r requirements.txt",
        "Installing Python dependencies"
    )
    
    # Step 2: Install Playwright browsers
    run_command(
        f"{sys.executable} -m playwright install chromium",
        "Installing Chromium for Playwright"
    )
    
    # Step 3: Create output directories
    os.makedirs("output", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    print("\n✅ Created output/ and logs/ directories")
    
    # Step 4: Test imports
    print(f"\n{'='*50}")
    print("🧪 Testing imports...")
    print(f"{'='*50}")
    
    try:
        import requests
        print("✅ requests")
    except ImportError:
        print("❌ requests - run: pip install requests")
    
    try:
        from playwright.sync_api import sync_playwright
        print("✅ playwright")
    except ImportError:
        print("❌ playwright - run: pip install playwright")
    
    # Step 5: Test Hyperliquid API
    print(f"\n{'='*50}")
    print("🌐 Testing Hyperliquid API connection...")
    print(f"{'='*50}")
    
    try:
        import requests
        response = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "meta"},
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            print(f"✅ API connected - {len(data.get('universe', []))} assets available")
        else:
            print(f"⚠️  API returned status {response.status_code}")
    except Exception as e:
        print(f"⚠️  API test failed: {e}")
        print("   (This is OK if you're behind a firewall)")
    
    # Done
    print(f"""
    {'='*50}
    ✅ SETUP COMPLETE!
    {'='*50}
    
    Next steps:
    
    1. Edit config.py to set your thresholds:
       - MAJOR_ASSET_THRESHOLD = 10_000_000  (n_1 for BTC/ETH/SOL)
       - DEFAULT_NOTIONAL_THRESHOLD = 1_000_000  (n_2 for others)
       - OI_PERCENTAGE_THRESHOLD = 0.05  (x = 5%)
    
    2. Test with dry run:
       python scanner.py --dry-run
    
    3. Test with visible browser:
       python scanner.py --no-headless --max-assets 3
    
    4. Run full scan:
       python scanner.py
    
    For Claude Code debugging, the CLAUDE.md file has all context.
    """)

if __name__ == "__main__":
    main()
