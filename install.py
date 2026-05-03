#!/usr/bin/env python3
"""
Simple installation script for Internxt Python CLI
install.py
"""

import subprocess
import sys
from pathlib import Path

def log(message, status="INFO"):
    icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARNING": "⚠️"}
    print(f"{icons.get(status, 'ℹ️')} {message}")

def run_command(args):
    """Run a command (list of argv) and return success status"""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
        return result.returncode == 0, result.stdout, result.stderr
    except Exception as e:
        return False, "", str(e)

def install_dependencies():
    """Install required dependencies"""
    log("Installing dependencies...", "INFO")

    dependencies = [
        "requests>=2.31.0",
        "cryptography>=41.0.0",
        "mnemonic>=0.20",
        "click>=8.1.0",
        "tqdm>=4.65.0"
    ]

    for dep in dependencies:
        log(f"Installing {dep}...", "INFO")
        success, _stdout, stderr = run_command([sys.executable, "-m", "pip", "install", dep])
        if success:
            log(f"✅ {dep.split('>=')[0]} installed", "SUCCESS")
        else:
            log(f"❌ Failed to install {dep}: {stderr}", "ERROR")
            return False

    return True

def test_standalone_cli():
    """Test the standalone CLI"""
    log("Testing standalone CLI...", "INFO")

    if not Path("cli.py").exists():
        log("cli.py not found in current directory", "ERROR")
        return False

    # Test basic functionality
    success, _stdout, stderr = run_command([sys.executable, "cli.py", "test"])
    if success:
        log("Standalone CLI test passed", "SUCCESS")
        return True
    else:
        log(f"Standalone CLI test failed: {stderr}", "ERROR")
        return False

def main():
    """Main installation function"""
    log("🚀 Internxt Python CLI Installation Script", "INFO")
    log("=" * 50)
    
    # Check Python version
    python_version = sys.version_info
    if python_version.major < 3 or python_version.minor < 8:
        log(f"Python {python_version.major}.{python_version.minor} detected. Python 3.8+ required.", "ERROR")
        return False
    
    log(f"Python {python_version.major}.{python_version.minor} detected ✅", "SUCCESS")
    
    # Install dependencies
    if not install_dependencies():
        log("Dependency installation failed", "ERROR")
        return False
    
    # Test standalone CLI
    if not test_standalone_cli():
        log("CLI test failed", "WARNING")
        log("But you can still try using it manually", "INFO")
    
    log("🎉 Installation completed!", "SUCCESS")
    log("", "INFO")
    log("📋 Usage:", "INFO")
    log("  python cli.py login       # Login to your account", "INFO")
    log("  python cli.py whoami      # Check login status", "INFO")
    log("  python cli.py list        # List files", "INFO")
    log("  python cli.py config      # Show configuration", "INFO")
    log("  python cli.py test        # Test components", "INFO")
    log("", "INFO")
    log("🔧 The CLI now uses the correct API endpoints!", "SUCCESS")
    log("   - Login: https://api.internxt.com/drive/auth/signin", "SUCCESS")
    log("   - 2FA: https://api.internxt.com/drive/auth/security", "SUCCESS")
    
    return True

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        log("\n👋 Installation cancelled", "WARNING")
        sys.exit(1)
    except Exception as e:
        log(f"Unexpected error: {e}", "ERROR")
        sys.exit(1)