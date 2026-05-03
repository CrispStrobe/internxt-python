#!/usr/bin/env python3
"""
API Debug Test - Compare working CLI calls vs WebDAV provider calls
Save as: debug_api.py
"""

import sys
import os

# Add current directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

def test_api_calls():
    """Test the same API calls that work for CLI vs what WebDAV provider does"""
    
    print("🧪 API Debug Test")
    print("=" * 60)
    
    try:
        # Import services
        from services.auth import auth_service
        from services.drive import drive_service
        from utils.api import api_client
        
        print("✅ Successfully imported all services")
        
        # Test 1: Check authentication
        print("\n1. Testing authentication...")
        credentials = auth_service.get_auth_details()
        user_email = credentials['user'].get('email', 'unknown')
        root_folder_id = credentials['user'].get('rootFolderId', 'unknown')
        print(f"   ✅ Authenticated as: {user_email}")
        print(f"   ✅ Root folder ID: {root_folder_id}")
        print(f"   ✅ API client drive_api_url: {api_client.drive_api_url}")
        print(f"   ✅ API client session headers: {dict(api_client.session.headers)}")
        
        # Test 2: Try CLI method (this works)
        print("\n2. Testing CLI method (drive_service.get_folder_content)...")
        try:
            content = drive_service.get_folder_content(root_folder_id)
            folders = content.get('folders', [])
            files = content.get('files', [])
            print(f"   ✅ CLI method success: {len(folders)} folders, {len(files)} files")
            
            if folders:
                print(f"   📁 First folder: {folders[0].get('plainName', 'unnamed')}")
            if files:
                print(f"   📄 First file: {files[0].get('plainName', 'unnamed')}")
                
        except Exception as e:
            print(f"   ❌ CLI method failed: {e}")
            import traceback
            traceback.print_exc()
        
        # Test 3: Try direct API calls (what WebDAV provider does)
        print("\n3. Testing direct API calls...")
        
        print("   3a. Testing api_client.get_folder_folders()...")
        try:
            folders_response = api_client.get_folder_folders(root_folder_id, 0, 50)
            folders = folders_response.get('result', folders_response.get('folders', []))
            print(f"       ✅ get_folder_folders() success: {len(folders)} folders")
            print(f"       📋 Response keys: {list(folders_response.keys())}")
            print(f"       📋 Response type: {type(folders_response)}")
            if folders:
                print(f"       📁 First folder: {folders[0].get('plainName', 'unnamed')}")
        except Exception as e:
            print(f"       ❌ get_folder_folders() failed: {e}")
            import traceback
            traceback.print_exc()
        
        print("   3b. Testing api_client.get_folder_files()...")
        try:
            files_response = api_client.get_folder_files(root_folder_id, 0, 50)
            files = files_response.get('result', files_response.get('files', []))
            print(f"       ✅ get_folder_files() success: {len(files)} files")
            print(f"       📋 Response keys: {list(files_response.keys())}")
            print(f"       📋 Response type: {type(files_response)}")
            if files:
                print(f"       📄 First file: {files[0].get('plainName', 'unnamed')}")
        except Exception as e:
            print(f"       ❌ get_folder_files() failed: {e}")
            import traceback
            traceback.print_exc()
        
        # Test 4: Check API client session state
        print("\n4. Checking API client session state...")
        print(f"   📋 Session headers: {dict(api_client.session.headers)}")
        print(f"   📋 Drive API URL: {api_client.drive_api_url}")
        print(f"   📋 Network URL: {api_client.network_url}")
        
        # Test 5: Manual HTTP request to see raw response
        print("\n5. Testing manual HTTP request...")
        try:
            import requests
            
            url = f"{api_client.drive_api_url}/folders/content/{root_folder_id}/folders"
            headers = api_client.session.headers.copy()
            params = {'offset': 0, 'limit': 10, 'sort': 'plainName', 'direction': 'ASC'}
            
            print(f"   📋 URL: {url}")
            print(f"   📋 Headers: {dict(headers)}")
            print(f"   📋 Params: {params}")
            
            response = requests.get(url, headers=headers, params=params, timeout=10)
            print(f"   📋 Status code: {response.status_code}")
            print(f"   📋 Response headers: {dict(response.headers)}")
            print(f"   📋 Response length: {len(response.text)} characters")
            
            if response.status_code == 200:
                try:
                    json_data = response.json()
                    print(f"   ✅ JSON response: {type(json_data)} with keys: {list(json_data.keys())}")
                except ValueError:
                    print("   ⚠️  Response is not JSON")
                    print(f"   📋 First 200 chars: {response.text[:200]}")
            else:
                print(f"   ❌ HTTP error: {response.status_code}")
                print(f"   📋 Response text: {response.text[:500]}")
                
        except Exception as e:
            print(f"   ❌ Manual HTTP request failed: {e}")
            import traceback
            traceback.print_exc()
        
        print("\n🎯 Summary:")
        print("   - If CLI method works but direct API calls fail, there's an issue with the API client")
        print("   - If manual HTTP request fails, there's an authentication or network issue")
        print("   - Check the status codes and error messages above")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_api_calls()