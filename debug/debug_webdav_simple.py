#!/usr/bin/env python3
"""
Simple WebDAV server test - run this to verify the server works
"""

import requests
import time
import sys
from requests.auth import HTTPBasicAuth

def test_webdav_simple(base_url="http://localhost:8080", username="internxt", password="internxt-webdav"):
    """Simple WebDAV test that should work"""
    
    print(f"🧪 Testing WebDAV server at: {base_url}")
    print(f"👤 Using credentials: {username}:{password}")
    print("=" * 60)
    
    auth = HTTPBasicAuth(username, password)
    
    # Test 1: Basic connectivity
    print("1. Testing basic connectivity...")
    try:
        response = requests.get(base_url, timeout=5)
        print(f"   ✅ Server is responding (Status: {response.status_code})")
        
        if response.status_code == 401:
            print("   ℹ️  Authentication required (expected)")
        elif response.status_code == 200:
            print("   ℹ️  Server accessible")
            
    except requests.exceptions.ConnectRefused:
        print("   ❌ Connection refused - is the server running?")
        return False
    except requests.exceptions.Timeout:
        print("   ❌ Connection timeout")
        return False
    except Exception as e:
        print(f"   ❌ Connection failed: {e}")
        return False
    
    # Test 2: Authentication
    print("2. Testing authentication...")
    try:
        response = requests.get(base_url, auth=auth, timeout=10)
        
        if response.status_code in [200, 207]:
            print(f"   ✅ Authentication successful (Status: {response.status_code})")
        elif response.status_code == 401:
            print("   ❌ Authentication failed - check credentials")
            return False
        else:
            print(f"   ⚠️  Unexpected status: {response.status_code}")
            print(f"       Response: {response.text[:200]}...")
            
    except requests.exceptions.Timeout:
        print("   ❌ Authentication request timed out")
        return False
    except Exception as e:
        print(f"   ❌ Authentication test failed: {e}")
        return False
    
    # Test 3: WebDAV OPTIONS
    print("3. Testing WebDAV OPTIONS...")
    try:
        response = requests.options(base_url, auth=auth, timeout=10)
        
        if response.status_code in [200, 204]:
            print("   ✅ OPTIONS request successful")
            
            # Check headers
            allow_header = response.headers.get('Allow', '')
            dav_header = response.headers.get('DAV', '') 
            server_header = response.headers.get('Server', 'Unknown')
            
            print(f"   📋 Server: {server_header}")
            if dav_header:
                print(f"   📋 DAV: {dav_header}")
            if allow_header:
                methods = allow_header.split(', ')
                webdav_methods = [m for m in methods if m in ['PROPFIND', 'PROPPATCH', 'MKCOL', 'COPY', 'MOVE', 'LOCK', 'UNLOCK']]
                if webdav_methods:
                    print(f"   📋 WebDAV Methods: {', '.join(webdav_methods)}")
                    
        else:
            print(f"   ❌ OPTIONS failed (Status: {response.status_code})")
            return False
            
    except Exception as e:
        print(f"   ❌ OPTIONS test failed: {e}")
        return False
    
    # Test 4: Simple PROPFIND
    print("4. Testing PROPFIND...")
    try:
        headers = {
            'Depth': '1',
            'Content-Type': 'text/xml; charset=utf-8'
        }
        
        propfind_body = '''<?xml version="1.0"?>
<D:propfind xmlns:D="DAV:">
    <D:prop>
        <D:resourcetype/>
        <D:displayname/>
    </D:prop>
</D:propfind>'''
        
        response = requests.request(
            'PROPFIND',
            base_url,
            auth=auth,
            headers=headers,
            data=propfind_body.encode('utf-8'),
            timeout=15
        )
        
        if response.status_code == 207:
            print("   ✅ PROPFIND successful")
            print(f"   📋 Response length: {len(response.text)} characters")
            
            # Basic XML check
            if '<?xml' in response.text and ('multistatus' in response.text.lower()):
                print("   ✅ Valid XML response")
            else:
                print("   ⚠️  Response may not be valid XML")
                
        else:
            print(f"   ❌ PROPFIND failed (Status: {response.status_code})")
            print(f"       Response: {response.text[:200]}...")
            return False
            
    except Exception as e:
        print(f"   ❌ PROPFIND test failed: {e}")
        return False
    
    print("\n🎉 All tests passed! WebDAV server is working correctly.")
    
    # Show connection info
    print("\n💡 Connection Information:")
    print(f"   Server URL: {base_url}")
    print(f"   Username: {username}")
    print(f"   Password: {password}")
    
    print("\n🖥️  macOS Finder Instructions:")
    print("   1. Press Cmd+K in Finder")
    print(f"   2. Enter: {base_url}")
    print("   3. Use credentials above")
    
    print("\n🧪 curl Test Commands:")
    print(f"   curl -u {username}:{password} {base_url}")
    print(f"   curl -u {username}:{password} -X OPTIONS {base_url}")
    
    return True

def wait_for_server(base_url="http://localhost:8080", max_wait=30):
    """Wait for server to start"""
    print(f"⏳ Waiting for server to start at {base_url}...")
    
    start_time = time.time()
    while time.time() - start_time < max_wait:
        try:
            requests.get(base_url, timeout=2)
            print("✅ Server is ready!")
            return True
        except requests.RequestException:
            print(".", end="", flush=True)
            time.sleep(1)
    
    print(f"\n❌ Server did not start within {max_wait} seconds")
    return False

if __name__ == "__main__":
    # Default test settings
    base_url = "http://localhost:8080"
    username = "internxt"
    password = "internxt-webdav"
    
    # Allow command line override
    if len(sys.argv) > 1:
        base_url = sys.argv[1]
    if len(sys.argv) > 2:
        username = sys.argv[2] 
    if len(sys.argv) > 3:
        password = sys.argv[3]
    
    print("Simple WebDAV Server Test")
    print("Usage: python test_webdav_simple.py [url] [username] [password]")
    print()
    
    # Wait for server if needed
    if not wait_for_server(base_url):
        print("💡 Make sure to start the WebDAV server first:")
        print("   python cli.py webdav-start --port 8080")
        sys.exit(1)
    
    # Run tests
    success = test_webdav_simple(base_url, username, password)
    
    if success:
        print("\n✅ WebDAV server test completed successfully!")
        sys.exit(0)
    else:
        print("\n❌ WebDAV server test failed!")
        print("💡 Try these troubleshooting steps:")
        print("   1. Restart the server: python cli.py webdav-stop && python cli.py webdav-start --port 8080")
        print("   2. Check if another service is using the port")  
        print("   3. Try a different port: python cli.py webdav-start --port 9000")
        sys.exit(1)