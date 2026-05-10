#!/usr/bin/env python3
"""
internxt_cli/services/webdav_server.py
"""

import os
import sys
import signal
import socket
import atexit
from typing import Optional, Dict, Any

try:
    from wsgidav.wsgidav_app import WsgiDAVApp
except ImportError:
    print("❌ Missing WebDAV dependencies. Install with:")
    print("   pip install WsgiDAV")
    sys.exit(1)

# Try different WSGI servers
WSGI_SERVER: Optional[str] = None
serve: Any = None
wsgi: Any = None
try:
    from waitress import serve  # type: ignore[import-untyped, no-redef, unused-ignore]  # noqa: F811
    WSGI_SERVER = 'waitress'
except ImportError:
    try:
        from cheroot import wsgi  # type: ignore[import-untyped, no-redef, unused-ignore]  # noqa: F811
        WSGI_SERVER = 'cheroot'
    except ImportError:
        print("❌ No suitable WSGI server found. Install one of:")
        print("   pip install waitress")
        print("   pip install cheroot")
        sys.exit(1)

from config.config import config_service  # noqa: E402
from services.webdav_provider import InternxtDAVProvider  # noqa: E402


class WebDAVServer:
    """WebDAV Server for Internxt Drive"""
    
    def __init__(self):
        self.server = None
        self.server_thread = None
        self.is_running = False
        self.is_stopping = False
        self.config = self._load_config()
        self.app = None
        
        # Register cleanup function
        atexit.register(self._cleanup_on_exit)
        
    def _load_config(self) -> Dict[str, Any]:
        """Load WebDAV server configuration"""
        webdav_config = config_service.read_webdav_config()
        
        return {
            'host': webdav_config.get('host', 'localhost'),
            'port': int(webdav_config.get('port', 8080)),
            'timeout_minutes': int(webdav_config.get('timeoutMinutes', 30)),
            'verbose': int(webdav_config.get('verbose', 1)),
        }
    
    def _check_port_available(self, port: int) -> bool:
        """Check if port is available"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                result = sock.connect_ex(('localhost', port))
                return result != 0
        except Exception:
            return False
    
    def _find_available_port(self, start_port: int) -> int:
        """Find next available port starting from given port"""
        port = start_port
        while port < start_port + 100:
            if self._check_port_available(port):
                return port
            port += 1
        raise RuntimeError(f"No available ports found starting from {start_port}")
    
    def _create_wsgidav_app(self) -> WsgiDAVApp:
        """Create WsgiDAV application"""
        
        # WebDAV configuration optimized for compatibility
        config = {
            'provider_mapping': {
                '/': InternxtDAVProvider(),
            },
            'simple_dc': {
                'user_mapping': {
                    '*': {
                        'internxt': {
                            'password': 'internxt-webdav',
                            'description': 'Internxt Drive User',
                        }
                    }
                }
            },
            'http_authenticator': {
                'domain_controller': 'wsgidav.dc.simple_dc.SimpleDomainController',
                'accept_basic': True,
                'accept_digest': True,
                'default_to_digest': False,
            },
            'verbose': self.config['verbose'],
            'property_manager': True,
            'lock_storage': True,
            'dir_browser': {
                'enable': True,
                'response_trailer': '<p>Internxt WebDAV Server</p>',
            },
            # Compatibility options
            'block_size': 8192,
            'add_header_MS_Author_Via': True,
            # Don't expose server version
            'server_header': 'Internxt WebDAV Server',
        }
        
        return WsgiDAVApp(config)
    
    def start(self, port: Optional[int] = None, background: bool = False, 
            preserve_timestamps: bool = True, server_choice: str = 'auto') -> Dict[str, Any]: 
        """
        Start the WebDAV server
        
        Args:
            port: Port to run on (default from config)
            background: Run in background mode
            preserve_timestamps: Preserve file timestamps (default: True)
        
        Returns:
            Dict with success status, message, and URL
        """
        try:
            from wsgidav.wsgidav_app import WsgiDAVApp
            from services.webdav_provider import InternxtDAVProvider
            # Import for SSL
            from services.network_utils import NetworkUtils
            
            # Get configuration
            webdav_config = config_service.read_webdav_config()
            
            if port is None:
                port = int(webdav_config.get('port', 8080))

            protocol = webdav_config.get('protocol', 'http')

            # Default to loopback only. Users can override with `host` in
            # webdav config (e.g. "0.0.0.0" for LAN access).
            host = webdav_config.get('host', '127.0.0.1')

            # Create provider with timestamp preservation setting
            provider = InternxtDAVProvider(preserve_timestamps=preserve_timestamps)

            # Configure WsgiDAV
            config = {
                "host": host,
                "port": port,
                "provider_mapping": {"/": provider},
                "simple_dc": {"user_mapping": {"*": {"internxt": {"password": "internxt-webdav"}}}},
                "verbose": 1,
                "logging": {
                    "enable": True,
                    "enable_loggers": ["wsgidav"],
                },
            }
            
            # Create WSGI application
            # Use self.app so it's available to other methods if needed
            self.app = WsgiDAVApp(config) 
            
            # Determine server URL
            server_url = f"{protocol}://localhost:{port}/"
            
            if background:
                # Background mode handled by CLI command
                pass
            
            # ---ssl_adapter = None SERVER AND SSL LOGIC ---

            # WSGI_SERVER (global) is our 'auto' default
            # We only override if the user forces a choice.
            
            active_server: Optional[str] = None
            if server_choice == 'waitress':
                try:
                    from waitress import serve  # noqa: F401  (kept in scope below)
                    active_server = 'waitress'
                except ImportError as exc:
                    raise ImportError(
                        "Server choice 'waitress' is not installed. Run: pip install waitress"
                    ) from exc

            elif server_choice == 'cheroot':
                try:
                    from cheroot import wsgi  # noqa: F401
                    active_server = 'cheroot'
                except ImportError as exc:
                    raise ImportError(
                        "Server choice 'cheroot' is not installed. Run: pip install cheroot"
                    ) from exc

            elif server_choice == 'auto':
                active_server = WSGI_SERVER  # Use the globally-detected one
            else:
                raise ValueError(
                    f"Unknown server choice '{server_choice}'. "
                    "Use 'auto', 'waitress', or 'cheroot'."
                )

            if active_server is None:
                raise ImportError("No suitable WSGI server found. Install 'waitress' or 'cheroot'")
            
            # SSL Configuration
            ssl_adapter = None
            if protocol.lower() == 'https':
                try:
                    if active_server == 'waitress':
                        print("⚠️  Warning: SSL (HTTPS) is not supported with the 'waitress' server.")
                        print("   Serving over HTTP instead.")
                        protocol = 'http'
                        server_url = f"http://localhost:{port}/" # Fallback to HTTP
                    elif active_server == 'cheroot':
                        cert_path = NetworkUtils.WEBDAV_SSL_CERT_FILE
                        key_path = NetworkUtils.WEBDAV_SSL_KEY_FILE
                        if not cert_path.exists() or not key_path.exists():
                            print("🔐 SSL certs not found, generating new ones...")
                            NetworkUtils.generate_new_selfsigned_certs()
                        
                        from cheroot.ssl.builtin import BuiltinSSLAdapter
                        ssl_adapter = BuiltinSSLAdapter(str(cert_path), str(key_path))
                        print(f"🔐 SSL (Cheroot) enabled: {cert_path}")
                    
                except Exception as e:
                    print(f"⚠️  SSL failed to initialize, falling back to HTTP: {e}")
                    server_url = f"http://localhost:{port}/" # Fallback
                    protocol = 'http'
                    ssl_adapter = None
            
            print("✅ WebDAV server starting...")
            print(f"🌐 URL: {server_url}")
            print("👤 Username: internxt")
            print("🔑 Password: internxt-webdav")
            print(f"🕐 Timestamp Preservation: {'Enabled' if preserve_timestamps else 'Disabled'}")
            print(f"🚀 Using server: {active_server}")

            if active_server == 'waitress':
                from waitress import serve  # noqa: F811

                # Waitress SSL is passed as arguments
                if protocol.lower() == 'https':
                    serve(
                        self.app,
                        host=host,
                        port=port,
                        ssl_certificate=str(NetworkUtils.WEBDAV_SSL_CERT_FILE),
                        ssl_private_key=str(NetworkUtils.WEBDAV_SSL_KEY_FILE),
                    )
                else:
                    serve(
                        self.app,
                        host=host,
                        port=port,
                    )

            elif active_server == 'cheroot':
                from cheroot import wsgi  # noqa: F811
                server = wsgi.Server(
                    bind_addr=(host, port),
                    wsgi_app=self.app,
                )
                
                # Apply SSL adapter if it was created
                if ssl_adapter:
                    server.ssl_adapter = ssl_adapter
                    
                self._server = server # Store server instance
                server.start() # Start serving
            
            else:
                # This case should be caught by the import check at the top
                raise RuntimeError("No valid WSGI server found.")
            
            return {
                "success": True,
                "message": "WebDAV server started",
                "url": server_url,
                "preserve_timestamps": preserve_timestamps
            }
            
        except Exception as e:
            print(f"❌ Error starting WebDAV server: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "message": f"Failed to start server: {e}",
                "url": None
            }
    
    def _setup_signal_handlers(self):
        """Setup signal handlers (only works in main thread)"""
        def signal_handler(signum, frame):
            print(f"\n🛑 Received signal {signum}, stopping WebDAV server...")
            self.is_stopping = True
            self.is_running = False
            config_service.clear_webdav_pid()
            os._exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
    def _run_server_thread(self):
        """Run server in thread"""
        try:
            self.is_running = True
            server_url = self._get_server_url()
            
            print(f"\n🌐 WebDAV server starting on {server_url}")
            print(f"📁 Mount point: {server_url}")
            print("👤 Username: internxt")
            print("🔑 Password: internxt-webdav")
            print()
            print("🌐 Web interface available at:", server_url)
            print()
            print("💡 Connection Instructions:")
            print("   1. Open Finder")
            print("   2. Press Cmd+K")
            print("   3. Enter server address:", server_url)
            print("   4. Username: internxt")
            print("   5. Password: internxt-webdav")
            print()
            
            if WSGI_SERVER == 'cheroot':
                print("⚠️  Note: You may see 'NoneType has no len()' errors with macOS Finder")
                print("   This is a known issue. Try using a WebDAV client like Cyberduck instead.")
                print()
            
            print("Press Ctrl+C to stop the server")
            
            # Start the appropriate server
            print("🔄 Starting server...")
            
            if WSGI_SERVER == 'waitress':
                # Use Waitress (more stable with various clients)
                serve(
                    self.app,
                    host=self.config['host'],
                    port=self.config['port'],
                    threads=10,
                    connection_limit=1000,
                    cleanup_interval=10,
                    channel_timeout=120,
                    ident='Internxt WebDAV Server',
                )
            else:
                # Use Cheroot (original)
                self.server = wsgi.Server(
                    bind_addr=(self.config['host'], self.config['port']),
                    wsgi_app=self.app,
                    timeout=self.config['timeout_minutes'] * 60,
                    server_name="Internxt WebDAV Server",
                    numthreads=10,
                    max=-1,
                    shutdown_timeout=5,
                )
                self.server.start()
            
        except KeyboardInterrupt:
            print("\n🛑 WebDAV server stopped by user")
        except Exception as e:
            if not self.is_stopping:
                print(f"❌ WebDAV server error: {e}")
                if "NoneType" not in str(e) and "signal" not in str(e):
                    import traceback
                    traceback.print_exc()
        finally:
            self.is_running = False
            config_service.clear_webdav_pid()
            print("🛑 WebDAV server stopped")
    
    def stop(self) -> Dict[str, Any]:
        """Stop WebDAV server"""
        print("🛑 Stopping WebDAV server...")
        self.is_stopping = True
        
        # Check for PID file from background process
        saved_pid = config_service.read_webdav_pid()
        if saved_pid:
            try:
                # Try to kill the background process
                import psutil
                process = psutil.Process(saved_pid)
                process.terminate()
                process.wait(timeout=5)
                config_service.clear_webdav_pid()
                print("✅ Background WebDAV server stopped successfully")
                return {
                    'success': True,
                    'message': 'Background WebDAV server stopped successfully'
                }
            except ImportError:
                print("⚠️  psutil not installed. Install with: pip install psutil")
                print(f"   To stop manually: kill {saved_pid}")
                return {
                    'success': False,
                    'message': f'Cannot stop background server. Run: kill {saved_pid}'
                }
            except Exception as e:
                print(f"⚠️  Could not stop background process: {e}")
                config_service.clear_webdav_pid()
                return {
                    'success': False,
                    'message': f'Error stopping background server: {e}'
                }
        
        if not self.is_running:
            return {
                'success': False,
                'message': 'WebDAV server is not running'
            }
        
        try:
            self.is_running = False
            
            if WSGI_SERVER == 'cheroot' and self.server:
                print("🛑 Shutting down Cheroot server...")
                try:
                    self.server.stop()
                except Exception as e:
                    print(f"⚠️  Error stopping server: {e}")
            
            if self.server_thread and self.server_thread.is_alive():
                print("🛑 Waiting for server thread to finish...")
                # For Waitress, we need to forcefully terminate
                if WSGI_SERVER == 'waitress':
                    os._exit(0)
                else:
                    self.server_thread.join(timeout=2)
            
            config_service.clear_webdav_pid()
            print("✅ WebDAV server stopped successfully")
            
            return {
                'success': True,
                'message': 'WebDAV server stopped successfully'
            }
            
        except Exception as e:
            print(f"❌ Error during server shutdown: {e}")
            config_service.clear_webdav_pid()
            return {
                'success': False,
                'message': f'Error stopping WebDAV server: {e}'
            }
    
    def _cleanup_on_exit(self):
        """Cleanup function called on exit"""
        if self.is_running:
            self.is_stopping = True
            self.is_running = False
            config_service.clear_webdav_pid()
    
    def status(self) -> Dict[str, Any]:
        """Get server status"""
        # Check for background process first
        saved_pid = config_service.read_webdav_pid()
        if saved_pid:
            try:
                import psutil
                process = psutil.Process(saved_pid)
                if process.is_running():
                    return {
                        'running': True,
                        'url': self._get_server_url(),
                        'port': self.config['port'],
                        'protocol': 'http',
                        'host': self.config['host'],
                        'server': WSGI_SERVER,
                        'pid': saved_pid,
                        'mode': 'background'
                    }
            except ImportError:
                # psutil not available, assume running if PID exists
                return {
                    'running': True,
                    'url': self._get_server_url(),
                    'port': self.config['port'],
                    'protocol': 'http',
                    'host': self.config['host'],
                    'server': WSGI_SERVER,
                    'pid': saved_pid,
                    'mode': 'background (unverified - install psutil to verify)'
                }
            except Exception:
                # Process doesn't exist, clear stale PID
                config_service.clear_webdav_pid()
        
        if self.is_running:
            return {
                'running': True,
                'url': self._get_server_url(),
                'port': self.config['port'],
                'protocol': 'http',
                'host': self.config['host'],
                'server': WSGI_SERVER,
                'mode': 'foreground'
            }
        else:
            return {
                'running': False,
                'message': 'WebDAV server is not running'
            }
    
    def _get_server_url(self) -> str:
        """Get server URL"""
        return f"http://{self.config['host']}:{self.config['port']}/"
    
    def get_mount_instructions(self) -> Dict[str, str]:
        """Get platform-specific mount instructions"""
        url = self._get_server_url()
        
        macos_extra = ""
        if WSGI_SERVER == 'cheroot':
            macos_extra = """
            
NOTE: If you see connection errors with macOS Finder, try:
- Using Cyberduck instead: https://cyberduck.io
- Or install waitress for better compatibility: pip install waitress
"""
        
        return {
            'macos': f"""
macOS Finder:
1. Open Finder
2. Press Cmd+K (Connect to Server)
3. Enter: {url}
4. Click Connect
5. Username: internxt
6. Password: internxt-webdav{macos_extra}

macOS Command Line:
mkdir -p ~/InternxtDrive
mount -t webdav {url} ~/InternxtDrive
""",
            'windows': f"""
Windows File Explorer:
1. Open File Explorer
2. Click "This PC" 
3. Click "Map network drive"
4. Enter: {url}
5. Username: internxt
6. Password: internxt-webdav
""",
            'linux': f"""
Linux (davfs2):
sudo apt install davfs2
sudo mkdir -p /mnt/internxt
sudo mount -t davfs {url} /mnt/internxt
Username: internxt
Password: internxt-webdav
"""
        }
    
    def test_connection(self) -> Dict[str, Any]:
        """Test WebDAV server connection"""
        status = self.status()
        if not status.get('running'):
            return {
                'success': False,
                'message': 'WebDAV server is not running'
            }
        
        try:
            import requests
            from requests.auth import HTTPBasicAuth
            
            url = self._get_server_url()
            auth = HTTPBasicAuth('internxt', 'internxt-webdav')
            
            # Test PROPFIND
            propfind_body = '''<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
    <D:prop>
        <D:resourcetype/>
    </D:prop>
</D:propfind>'''
            
            response = requests.request(
                'PROPFIND',
                url,
                auth=auth,
                headers={'Depth': '0', 'Content-Type': 'application/xml'},
                data=propfind_body,
                timeout=10
            )
            
            if response.status_code == 207 and '<?xml' in response.text:
                return {
                    'success': True,
                    'message': 'WebDAV server is working correctly',
                    'status_code': response.status_code,
                    'server': WSGI_SERVER
                }
            else:
                return {
                    'success': False,
                    'message': f'Server returned status {response.status_code}',
                    'status_code': response.status_code
                }
                
        except Exception as e:
            return {
                'success': False,
                'message': f'Connection test failed: {e}'
            }


# Global instance
webdav_server = WebDAVServer()