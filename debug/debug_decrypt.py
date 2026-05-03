#!/usr/bin/env python3
"""
Debug script for file decryption
"""

import sys
import os

# Add current directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

def debug_file_download():
    """Debug the file download and decryption process"""
    
    print("🔍 Debug: File Download and Decryption")
    print("=" * 60)
    
    try:
        from services.auth import auth_service
        from services.drive import drive_service
        from services.crypto import crypto_service
        from utils.api import api_client
        
        # Get auth details
        credentials = auth_service.get_auth_details()
        user = credentials['user']
        
        print(f"✅ Authenticated as: {user.get('email')}")
        print(f"📋 User UUID: {user.get('uuid')}")
        print(f"📋 Bucket ID: {user.get('bucket')}")
        
        # Check mnemonic
        mnemonic = user.get('mnemonic')
        print(f"📋 Mnemonic present: {'Yes' if mnemonic else 'No'}")
        if mnemonic:
            words = mnemonic.split()
            print(f"📋 Mnemonic words: {len(words)}")
            print(f"📋 First word: {words[0] if words else 'None'}")
            print(f"📋 Mnemonic valid: {crypto_service.validate_mnemonic(mnemonic)}")
        
        # Get file metadata
        file_uuid = 'c05fdcfc-ff72-4512-a636-8d4f00436005'  # Your test file
        print(f"\n🔍 Getting metadata for file: {file_uuid}")
        
        metadata = api_client.get_file_metadata(file_uuid)
        print(f"📋 File name: {metadata.get('plainName')}")
        print(f"📋 File type: {metadata.get('type')}")
        print(f"📋 File size: {metadata.get('size')}")
        print(f"📋 Bucket ID: {metadata.get('bucket')}")
        print(f"📋 File ID: {metadata.get('fileId')}")
        
        # Get download info
        print("\n🔍 Getting download links...")
        network_auth = drive_service._get_network_auth(user)
        
        links_response = api_client.get_download_links(
            metadata['bucket'], 
            metadata['fileId'], 
            auth=network_auth
        )
        
        print(f"📋 File index (hex): {links_response.get('index')}")
        print(f"📋 Index length: {len(links_response.get('index', ''))}")
        
        # Download encrypted data
        print("\n🔍 Downloading encrypted data...")
        download_url = links_response['shards'][0]['url']
        encrypted_data = api_client.download_chunk(download_url)
        
        print(f"📋 Encrypted data size: {len(encrypted_data)} bytes")
        print(f"📋 First 16 bytes (hex): {encrypted_data[:16].hex()}")
        
        # Test decryption
        print("\n🔍 Testing decryption...")
        file_index_hex = links_response['index']
        
        # Debug the decryption inputs
        print(f"📋 Mnemonic (first 20 chars): {mnemonic[:20]}...")
        print(f"📋 Bucket ID: {metadata['bucket']}")
        print(f"📋 File index: {file_index_hex}")
        
        # Try decryption
        try:
            decrypted_data = crypto_service.decrypt_stream_internxt_protocol(
                encrypted_data, 
                mnemonic, 
                metadata['bucket'], 
                file_index_hex
            )
            
            print("✅ Decryption successful!")
            print(f"📋 Decrypted size: {len(decrypted_data)} bytes")
            print(f"📋 Decrypted content: {decrypted_data}")
            print(f"📋 Decrypted as text: {decrypted_data.decode('utf-8', errors='replace')}")
            
        except Exception as e:
            print(f"❌ Decryption failed: {e}")
            import traceback
            traceback.print_exc()
        
        # Let's also test the key generation
        print("\n🔍 Testing key generation...")
        
        # Convert index to bytes
        index_bytes = bytes.fromhex(file_index_hex)
        print(f"📋 Index bytes length: {len(index_bytes)}")
        
        # Generate file key
        file_key = crypto_service.generate_file_key(mnemonic, metadata['bucket'], index_bytes)
        print(f"📋 File key (hex): {file_key.hex()}")
        print(f"📋 File key length: {len(file_key)} bytes")
        
        # Get IV from index
        iv = index_bytes[:16]
        print(f"📋 IV (hex): {iv.hex()}")
        print(f"📋 IV length: {len(iv)} bytes")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    debug_file_download()